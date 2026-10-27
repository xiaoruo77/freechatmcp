from __future__ import annotations

import base64
from dataclasses import replace
import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/mcp"),
    ("GET", "/sse"),
    ("GET", "/sse/"),
    ("POST", "/messages/?session_id=00000000-0000-0000-0000-000000000000"),
    ("POST", "/messages"),
])
def test_every_mcp_transport_requires_token_when_oauth_disabled(isolated_settings, tmp_path, method, path):
    from web_mcp.server import create_app

    settings = replace(isolated_settings, orchestration_mode="chatgpt_direct", oauth_enabled=False,
                       workspace_root=tmp_path, allowed_workspaces=[tmp_path])
    with TestClient(create_app(settings)) as client:
        # In particular, SSE must reject before opening an indefinite stream,
        # and the message endpoint must reject before looking up session IDs.
        missing = client.request(method, path)
        wrong = client.request(method, path, headers={"Authorization": "Bearer invalid-test-token"})
        assert missing.status_code == wrong.status_code == 401


@pytest.fixture
async def security_registry(isolated_settings, tmp_path):
    from web_canvas.service import CanvasService
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    settings = replace(isolated_settings, orchestration_mode="chatgpt_direct", workspace_root=tmp_path,
                       allowed_workspaces=[tmp_path])
    db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    await db.init()
    registry = ToolRegistry(ToolContext(settings, db, CanvasService(db), logging.getLogger("direct-security-tests")))
    try:
        yield registry
    finally:
        await db.close()


async def test_task_status_and_monitor_both_reject_unverified_production_and_persist_failure(security_registry, tmp_path):
    from web_canvas.service import CanvasService
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    registry = security_registry
    task = await registry.tasks.create("production_md", {"system_dir": str(tmp_path), "duration_seconds": 300})
    # A zero process exit is deliberately insufficient: no fresh production
    # log, trajectory or elapsed-time evidence exists in this test directory.
    await registry.tasks.finish(task["id"], "success", result={"return_code": 0})
    status = await registry.invoke("task_status", {"task_id": task["id"]})
    monitor = await registry.invoke("monitor_task", {"task_id": task["id"]})
    for result in (status, monitor):
        assert result["status"] == "failed" and result["terminal"] is True
        assert result["completed"] is False
        assert result["error_code"] == "production_md_output_not_verified"
        assert result["result"]["return_code"] == 0
    assert (await registry.tasks.get(task["id"]))["status"] == "failed"

    # A separate connection and registry must see the corrected durable state.
    # Reading it does not enqueue a replacement scientific run.
    settings = registry.ctx.settings
    reopened = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    await reopened.init()
    try:
        restored = ToolRegistry(ToolContext(settings, reopened, CanvasService(reopened), registry.ctx.logger))
        assert (await restored.invoke("task_status", {"task_id": task["id"]}))["status"] == "failed"
        assert len(await restored.tasks.list()) == 1
    finally:
        await reopened.close()


@pytest.mark.parametrize(("recorded_result", "expected"), [
    (None, "failed"),
    ({"return_code": 9}, "failed"),
    ({"return_code": 0}, "success"),
])
async def test_task_status_requires_real_zero_return_code(security_registry, recorded_result, expected):
    task = await security_registry.tasks.create("python_script", {"code": "Explicit test payload; never executed"})
    await security_registry.tasks.finish(task["id"], "success", result=recorded_result)
    result = await security_registry.invoke("task_status", {"task_id": task["id"]})
    assert result["status"] == expected and result["terminal"] is True
    assert (await security_registry.tasks.get(task["id"]))["status"] == expected


@pytest.mark.parametrize("already_normalized", [False, True])
def test_diagnostic_secrets_are_redacted_in_every_result_copy(already_normalized):
    from web_mcp.result_contract import normalize_result

    diagnostics = {
        "status": "failed",
        "error": "password=error-only-test-value",
        "logs": [{"message": "token=log-only-test-value"}],
        "diagnostic_evidence": {"nested": [{"message": "Authorization=Bearer bearer-only-test-value"}]},
        "payload": {"api_key": "key-only-test-value"},
    }
    if already_normalized:
        value = {"ok": False, "status": "failed", "terminal": True, "task_id": None,
                 "operation": "example", "result": {"summary": "failed", "data": diagnostics},
                 "artifacts": [], "error": {"code": "test_failure", "message": diagnostics["error"]},
                 "retryable": False, "evidence": {}}
    else:
        value = diagnostics
    output = normalize_result("example", value)
    serialized = json.dumps(output)
    for secret in ("error-only-test-value", "log-only-test-value", "bearer-only-test-value", "key-only-test-value"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert diagnostics["error"] == "password=error-only-test-value"


def test_explicit_file_content_survives_diagnostic_redaction():
    from web_mcp.result_contract import normalize_result

    text = "password=intentionally-visible-file-data\nRead this user-requested fixture exactly."
    result = normalize_result("read_file", {"path": "fixture.txt", "content": text})
    assert result["result"]["data"]["content"] == text


async def test_direct_output_schemas_cover_lists_errors_and_private_images(tmp_path, monkeypatch):
    from web_mcp import mcp_server
    from web_mcp.result_contract import normalize_result

    root = tmp_path / "data"
    private = root / "windows_mcp"
    private.mkdir(parents=True)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2BsAAAAASUVORK5CYII=")
    allowed = private / "allowed.png"
    outside = root / "outside.png"
    allowed.write_bytes(png)
    outside.write_bytes(png)
    oversized = private / "oversized.png"
    with oversized.open("wb") as handle:
        handle.truncate(12 * 1024 * 1024 + 1)

    class FakeRegistry:
        chatgpt_direct = True
        ctx = SimpleNamespace(settings=SimpleNamespace(data_root=root))

        async def invoke(self, name, arguments=None):
            if name == "list_tasks":
                return []
            if name == "read_file":
                raise FileNotFoundError("Missing test fixture")
            assert name == "windows_screenshot"
            return normalize_result(name, {"artifacts": [
                {"path": str(allowed), "mime_type": "image/png"},
                {"path": str(outside), "mime_type": "image/png"},
                {"path": str(oversized), "mime_type": "image/png"},
                {"path": str(private / "missing.png"), "mime_type": "image/png"},
                {"path": str(allowed), "mime_type": "text/plain"},
            ]})

    monkeypatch.setattr(mcp_server, "_registry", FakeRegistry())
    server = mcp_server.build_mcp(orchestration_mode="chatgpt_direct")
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name, args in (("list_tasks", {}), ("read_file", {"path": "missing.txt"}), ("windows_screenshot", {})):
        result = await server.call_tool(name, args)
        Draft202012Validator(tools[name].outputSchema).validate(result.structuredContent)
        assert result.structuredContent["operation"] == name
        assert bool(result.isError) == (not result.structuredContent["ok"])
        images = [block for block in result.content if block.type == "image"]
        if name == "windows_screenshot":
            assert len(images) == 1
            assert images[0].mimeType == "image/png"
            assert base64.b64decode(images[0].data) == png
        elif name == "list_tasks":
            assert result.structuredContent["result"]["data"]["items"] == []
            assert images == []
        else:
            assert result.structuredContent["error_code"] == "not_found"
            assert images == []