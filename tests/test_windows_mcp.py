from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, ImageContent, TextContent, Tool
import pytest

from web_tools.windows_mcp import ALLOWED_ACTIONS, WindowsMCPBridge, WindowsMCPError


@pytest.fixture(autouse=True)
def unlocked_test_desktop(monkeypatch):
    monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", lambda: {"available": True, "session_id": 7})


def settings(tmp_path, **overrides):
    values = dict(data_root=tmp_path, windows_mcp_enabled=True, windows_mcp_control_enabled=True, windows_mcp_timeout=1, windows_mcp_transport="stdio")
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.initializations = 0
        self.response = CallToolResult(content=[TextContent(type="text", text="Observed test desktop")])

    async def initialize(self):
        self.initializations += 1

    async def list_tools(self):
        return SimpleNamespace(tools=[Tool(name=name, inputSchema={"type": "object", "properties": {"text": {"type": "string"}, "label": {"type": "integer"}, "mode": {"type": "string"}}, "additionalProperties": True}) for name in ALLOWED_ACTIONS])

    async def call_tool(self, action, arguments):
        self.calls.append((action, arguments))
        return self.response


def fake_bridge(tmp_path, monkeypatch, **overrides):
    bridge = WindowsMCPBridge(settings(tmp_path, **overrides))
    session = FakeSession()

    @asynccontextmanager
    async def connect():
        yield session

    monkeypatch.setattr(bridge, "_session", connect)
    return bridge, session


@pytest.mark.asyncio
async def test_default_disabled_and_control_requires_separate_gate(tmp_path):
    bridge = WindowsMCPBridge(SimpleNamespace(data_root=tmp_path))
    assert (await bridge.call("Snapshot"))["error_code"] == "desktop_disabled"
    bridge = WindowsMCPBridge(settings(tmp_path, windows_mcp_control_enabled=False))
    assert (await bridge.call("Click", {"loc": [1, 2]}))["error_code"] == "desktop_control_disabled"
    assert bridge._worker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PowerShell", "FileSystem", "Registry", "Scrape", "Clipboard", "Multi", "Move", "unknown"])
async def test_dangerous_or_unmapped_tools_never_dispatch(tmp_path, monkeypatch, action):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    result = await bridge.call(action)
    assert result["error_code"] == "desktop_action_denied"
    assert not session.calls
    assert bridge._worker is None


@pytest.mark.asyncio
async def test_snapshot_labels_persist_in_single_owned_session(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    try:
        first = await bridge.call("Snapshot")
        second = await bridge.call("Type", {"text": "reversible smoke test", "label": 3})
        assert first["ok"] and second["ok"]
        assert session.initializations == 1
        assert session.calls == [("Snapshot", {}), ("Type", {"text": "reversible smoke test", "label": 3})]
        assert second["evidence"]["verification_required"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_lock_after_snapshot_blocks_next_control(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    try:
        assert (await bridge.call("Snapshot"))["ok"]
        monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", lambda: {"available": False, "error_code": "desktop_locked_or_secure"})
        result = await bridge.call("Click", {"label": 3})
        assert result["error_code"] == "desktop_locked_or_secure"
        assert session.calls == [("Snapshot", {})]
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"mode": "launch_executable", "executable": "powershell.exe"}, {"mode": "launch", "args": ["-Command", "whoami"]}, {"mode": "launch_executable"}])
async def test_app_executable_bypass_rejected(tmp_path, monkeypatch, args):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    result = await bridge.call("App", args)
    assert not result["ok"]
    assert not session.calls


@pytest.mark.asyncio
async def test_live_schema_validation_precedes_dispatch(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    try:
        result = await bridge.call("Type", {"text": "safe", "label": "wrong type"})
        assert result["error_code"] == "invalid_arguments"
        assert not session.calls
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_upstream_error_and_false_success_capture_text(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    try:
        session.response = CallToolResult(content=[TextContent(type="text", text="Error capturing desktop state: desktop locked")])
        result = await bridge.call("Snapshot")
        assert not result["ok"] and result["status"] == "failed"
        session.response = CallToolResult(isError=True, content=[TextContent(type="text", text="User takeover active")])
        result = await bridge.call("Click", {"label": 3})
        assert not result["ok"] and not result["retryable"]
        session.response = CallToolResult(content=[TextContent(type="text", text="MissingTestApp not found in start menu.")])
        assert not (await bridge.call("App", {"name": "MissingTestApp"}))["ok"]
        session.response = CallToolResult(content=[TextContent(type="text", text="Switched to a different window.")])
        assert (await bridge.call("App", {"mode": "switch", "name": "exact-observed-test-title"}))["error_code"] == "desktop_target_mismatch"
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["CONTROL_UNAVAILABLE", "USER_CONTROL", "CONTROL_PREEMPTED"])
async def test_app_preserves_upstream_control_gate_error_instead_of_target_mismatch(tmp_path, monkeypatch, code):
    import json
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    message = json.dumps({"code": code, "status": {"state": "unavailable"}})
    session.response = CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
    try:
        result = await bridge.call("App", {"mode": "switch", "name": "exact-observed-owned-window-title"})
        assert not result["ok"]
        assert result["error_code"] == "desktop_action_failed"
        assert code in result["error"]["message"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_fastmcp4_capture_json_text_is_decoded_and_errors_remain_failures(tmp_path, monkeypatch):
    import json
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    try:
        session.response = CallToolResult(content=[TextContent(type="text", text=json.dumps(["Focused Window:\nOwned Notepad\nUI Tree:\n(120,240) 文档 \"文本编辑器\""]))])
        result = await bridge.call("Snapshot")
        assert result["ok"]
        assert "\nUI Tree:\n" in result["result"]["summary"]
        assert not result["result"]["summary"].startswith("[")
        session.response = CallToolResult(content=[TextContent(type="text", text=json.dumps(["Error capturing desktop state: desktop locked"]))])
        result = await bridge.call("Snapshot")
        assert not result["ok"] and result["error_code"] == "desktop_action_failed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["Snapshot", "Screenshot"])
@pytest.mark.parametrize("content", [[], [TextContent(type="text", text="[]")], [TextContent(type="text", text='["", " "]')]])
async def test_empty_capture_never_reports_completed(tmp_path, monkeypatch, action, content):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    session.response = CallToolResult(content=content)
    try:
        result = await bridge.call(action)
        assert not result["ok"] and result["status"] == "failed"
        assert result["error_code"] == "desktop_capture_empty"
        assert result["error"]["code"] == "desktop_capture_empty"
        assert not result["evidence"]["upstream_reported_success"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_screenshot_requires_image_while_text_snapshot_remains_valid(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    session.response = CallToolResult(content=[TextContent(type="text", text="Desktop observed; no image attached")])
    try:
        assert (await bridge.call("Snapshot"))["ok"]
        result = await bridge.call("Screenshot")
        assert not result["ok"] and result["error_code"] == "desktop_capture_empty"
        assert result["artifacts"] == []
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["Snapshot", "Screenshot"])
@pytest.mark.parametrize("content,is_error", [([], True), ([TextContent(type="text", text='["Error capturing screenshot: desktop locked"]')], False)])
async def test_upstream_capture_errors_take_precedence_over_empty_guard(tmp_path, monkeypatch, action, content, is_error):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    session.response = CallToolResult(content=content, isError=is_error)
    try:
        result = await bridge.call(action)
        assert not result["ok"] and result["error_code"] == "desktop_action_failed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_image_stays_private_and_has_real_content(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    session.response = CallToolResult(content=[ImageContent(type="image", mimeType="image/png", data=base64.b64encode(png).decode())])
    try:
        result = await bridge.call("Screenshot")
        artifact = result["artifacts"][0]
        assert result["ok"] and artifact["private"]
        assert Path(artifact["path"]).read_bytes() == png
        assert Path(artifact["path"]).parent == tmp_path / "windows_mcp"
        assert "data" not in artifact
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_malformed_image_rejected(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)
    session.response = CallToolResult(content=[ImageContent(type="image", mimeType="image/png", data="not-base64")])
    try:
        assert (await bridge.call("Screenshot"))["error_code"] == "desktop_image_invalid"
        assert not (tmp_path / "windows_mcp").exists()
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_timeout_does_not_retry_control(tmp_path, monkeypatch):
    bridge, session = fake_bridge(tmp_path, monkeypatch)

    async def slow(action, arguments):
        session.calls.append((action, arguments))
        await asyncio.sleep(10)

    monkeypatch.setattr(session, "call_tool", slow)
    result = await bridge.call("Click", {"label": 3})
    assert result["error_code"] == "desktop_timeout" and not result["retryable"]
    assert len(session.calls) == 1
    assert bridge._worker is None


@pytest.mark.asyncio
async def test_preflight_rejects_session_zero_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", lambda: {"available": False, "error_code": "interactive_session_required"})
    bridge = WindowsMCPBridge(settings(tmp_path))
    result = await bridge.call("Snapshot")
    assert result["error_code"] == "interactive_session_required"
    await bridge.close()


@pytest.mark.parametrize("url", ["http://0.0.0.0:8888/mcp", "http://example.com/mcp", "http://localhost:8888/mcp", "http://127.0.0.1:8888/mcp?token=secret", "http://user:pass@127.0.0.1/mcp", "file:///mcp"])
def test_non_loopback_and_credential_urls_rejected(tmp_path, url):
    bridge = WindowsMCPBridge(settings(tmp_path, windows_mcp_url=url))
    with pytest.raises(WindowsMCPError) as exc:
        bridge._http_config()
    assert exc.value.code == "desktop_endpoint_denied"


def test_http_requires_private_token_file_and_verifies_user_session(tmp_path, monkeypatch):
    token = tmp_path / "private.token"
    token.write_text("test-secret-token-more-than-20", encoding="utf-8")
    bridge = WindowsMCPBridge(settings(tmp_path, windows_mcp_url="http://127.0.0.1:48888/mcp", windows_mcp_token_file=token))
    seen = []
    monkeypatch.setattr(bridge, "_verify_http_session", lambda port: seen.append(port))
    url, headers = bridge._http_config()
    assert seen == [48888] and headers["Authorization"].startswith("Bearer ")
    assert url == "http://127.0.0.1:48888/mcp"
    token.write_text("short", encoding="utf-8")
    with pytest.raises(WindowsMCPError) as exc:
        bridge._http_config()
    assert exc.value.code == "desktop_auth_required"


@pytest.mark.asyncio
async def test_raw_connection_error_does_not_leak_secret(tmp_path, monkeypatch):
    bridge = WindowsMCPBridge(settings(tmp_path))

    @asynccontextmanager
    async def broken():
        raise OSError("Bearer secret-value must not appear")
        yield

    monkeypatch.setattr(bridge, "_session", broken)
    result = await bridge.call("Snapshot")
    assert result["error_code"] == "desktop_bridge_unavailable"
    assert "secret-value" not in str(result)
    await bridge.close()


@pytest.fixture
def notepad_probe():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "10_web_deploy" / "windows" / "windows_mcp_notepad_smoke.py"
    spec = importlib.util.spec_from_file_location("windows_mcp_notepad_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notepad_probe_selects_only_own_observed_edit_target(notepad_probe):
    snapshot = '''Focused Window:
    smoke-owned.txt - Notepad
    Opened Windows:
    Other App
    UI Tree:
    desktop
    ├── window "Other App"
    │   └── (10,20) edit "unrelated"
    ├── window "smoke-owned.txt - Notepad"
    │   └── (120,240) document "Text editor" [value:"test input"]
    └── window "Another App"
        └── (999,999) edit "not ours"
    '''
    assert notepad_probe.own_edit_points(snapshot, "smoke-owned") == [[120, 240]]
    assert notepad_probe.own_text_readback(snapshot, "smoke-owned", "test input")
    assert not notepad_probe.own_text_readback(snapshot, "smoke-owned", "unrelated")


def test_notepad_probe_title_and_foreign_text_are_not_readback(notepad_probe):
    snapshot = '''UI Tree:
    ├── window "smoke-owned test input.txt - Notepad"
    │   └── (120,240) document "Text editor"
    └── window "Other"
        └── (999,999) edit "test input"
    '''
    assert not notepad_probe.own_text_readback(snapshot, "smoke-owned", "test input")
    assert notepad_probe.own_edit_points("missing UI Tree", "smoke-owned") == []


def test_notepad_probe_chinese_uia_control_type(notepad_probe):
    snapshot = '''UI Tree:
    └── window "smoke-owned.txt - Notepad"
        └── (1538,1063) 文档 "文本编辑器" [value:"独立测试 readback"]
    '''
    assert notepad_probe.own_edit_points(snapshot, "smoke-owned") == [[1538, 1063]]
    assert notepad_probe.own_text_readback(snapshot, "smoke-owned", "独立测试 readback")


@pytest.mark.parametrize("state,expected", [("ready", True), ("ai", True), ("user", False), ("unavailable", False), ("takeover_pending", False), (None, False)])
def test_production_probe_waits_for_actual_control_readiness(state, expected):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "10_web_deploy" / "verify-desktop-production.py"
    spec = importlib.util.spec_from_file_location("desktop_production_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.control_state_ready({"state": state}) is expected
    assert not module.control_state_ready(None)
    assert module.initial_idle_ready({"state": state}, 10_000) is expected
    assert not module.initial_idle_ready({"state": state}, 9_999)
    assert not module.initial_idle_ready({"state": state}, None)