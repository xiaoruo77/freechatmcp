from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from web_mcp.server import create_app
from web_mcp.tool_registry import ToolRegistry


def direct_settings(base, tmp_path, **kwargs):
    return replace(base, orchestration_mode="chatgpt_direct", workspace_root=tmp_path,
                   allowed_workspaces=[tmp_path], **kwargs)


class FakeChatGPTHost:
    """A test client, not an LLM or proof of ChatGPT web product behavior."""
    def __init__(self, client):
        self.client = client
        self.trace = []

    def rpc(self, method, params=None):
        reply = self.client.post("/mcp", headers={"Authorization": "Bearer pytest-token", "Host": "127.0.0.1:48271",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
            json={"jsonrpc": "2.0", "id": len(self.trace) + 1, "method": method, "params": params or {}})
        assert reply.status_code == 200, reply.text
        if reply.headers.get("content-type", "").startswith("text/event-stream"):
            packet = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")][-1]
        else:
            packet = reply.json()
        assert "error" not in packet, packet
        return packet["result"]

    def call(self, name, arguments=None):
        raw = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        value = raw["structuredContent"]
        assert {"ok", "status", "terminal", "task_id", "operation", "result", "artifacts", "error", "retryable", "evidence"} <= value.keys()
        assert value["operation"] == name
        assert raw.get("isError", False) == (not value["ok"])
        self.trace.append({"name": name, "status": value["status"]})
        return value


def test_fake_host_dependent_calls_failure_repair_and_checkpoint(isolated_settings, tmp_path, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Sampling or implicit handoff was called")
    for name in ("_autonomous_sample", "_maybe_handoff_runner_goal", "_maybe_handoff_browser_goal", "_infer_latest_chat_goal"):
        monkeypatch.setattr(ToolRegistry, name, forbidden)
    settings = direct_settings(isolated_settings, tmp_path)
    with TestClient(create_app(settings)) as client:
        host = FakeChatGPTHost(client)
        listed = host.rpc("tools/list")["tools"]
        assert {"goal_start", "goal_checkpoint", "task_status", "windows_snapshot"} <= {t["name"] for t in listed}
        assert "terminal" in next(t for t in listed if t["name"] == "list_tasks")["outputSchema"]["properties"]
        goal = host.call("goal_start", {"goal": "Repair three test files", "acceptance": ["three files contain valid"]})
        goal_id = goal["goal_id"]
        host.call("create_directory", {"path": "probe"})
        for name, text in (("one.txt", "valid"), ("two.txt", "WRONG"), ("three.txt", "valid")):
            host.call("write_file", {"path": "probe/" + name, "content": text})
        directory = host.call("list_directory", {"path": "probe"})
        observed = {}
        for entry in directory["result"]["data"]["entries"]:
            read = host.call("read_file", {"path": entry["path"]})
            observed[entry["path"]] = read["result"]["data"]["content"]
        wrong = next(path for path, content in observed.items() if content != "valid")
        host.call("patch_file", {"path": wrong, "find": observed[wrong], "replace": "valid"})
        checked = host.call("read_file", {"path": wrong})
        assert checked["result"]["data"]["content"] == "valid"
        failure = host.call("read_file", {"path": "missing.txt"})
        assert failure["error_code"] == "not_found" and failure["terminal"]
        repaired = host.call("read_file", {"path": wrong})
        assert repaired["ok"]
        args = {"goal_id": goal_id, "step_id": "verified", "observation": {"path": wrong, "content": "valid"}, "idempotency_key": "verified-v1"}
        first = host.call("goal_checkpoint", args)
        duplicate = host.call("goal_checkpoint", args)
        assert first["result"]["data"]["sequence"] == duplicate["result"]["data"]["sequence"]
        finished = host.call("goal_finish", {"goal_id": goal_id, "outcome": "completed", "evidence": {"checks": [
            {"criterion": "three files contain valid", "passed": True, "evidence": {"directory": str(tmp_path / "probe"), "verified": True}}]}})
        assert finished["status"] == "completed"
        assert len(host.trace) >= 8
    # Fresh application and DB connection: checkpoint survives, no step is replayed.
    with TestClient(create_app(settings)) as client:
        resumed = FakeChatGPTHost(client).call("goal_status", {"goal_id": goal_id})
        assert len(resumed["result"]["data"]["checkpoints"]) == 1
        assert (tmp_path / wrong).read_text() == "valid"


def test_direct_mcp_browser_runner_no_handoff_and_permissions(isolated_settings, tmp_path, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("implicit supervisor entered")
    for name in ("_autonomous_sample", "_maybe_handoff_runner_goal", "_maybe_handoff_browser_goal"):
        monkeypatch.setattr(ToolRegistry, name, forbidden)
    async def read_page(self, **kwargs):
        return {"tab_id": kwargs["tab_id"], "text": "observed-page"}
    monkeypatch.setattr(ToolRegistry, "_browser_read_page", read_page)
    with TestClient(create_app(direct_settings(isolated_settings, tmp_path))) as client:
        host = FakeChatGPTHost(client)
        assert client.post("/mcp", json={}).status_code == 401
        page = host.call("browser_read_page", {"tab_id": "explicit-test-tab"})
        assert page["result"]["data"]["text"] == "observed-page"
        queue = host.call("run_powershell_workflow", {"workflow": "custom", "commands": ["Write-Output test"], "cwd": str(tmp_path)})
        assert queue["status"] == "queued" and not queue["terminal"] and queue["task_id"]
        assert host.call("task_status", {"task_id": queue["task_id"]})["status"] == "queued"
        host.call("cancel_task", {"task_id": queue["task_id"]})
        planning = host.call("run_autonomous_workflow", {"goal": "A natural-language goal needs host planning"})
        assert planning["status"] == "needs_chatgpt_planning" and not planning["terminal"]
        assert host.call("read_file", {"path": str(tmp_path.parent / "outside.txt")})["error_code"] == "permission_denied"
        assert host.call("windows_type", {"text": "test", "confirm": True})["error_code"] == "permission_denied"
        assert host.call("windows_snapshot")["error_code"] == "desktop_disabled"
        assert host.call("list_tasks")["result"]["data"]["items"]


@pytest.mark.skipif(os.getenv("WEB_AGENT_RUN_LONG_SMOKE") != "1", reason="Opt-in real 65-second Worker test")
def test_real_long_worker_keeps_health_and_mcp_responsive(isolated_settings, tmp_path):
    settings = direct_settings(isolated_settings, tmp_path, embedded_worker=True, task_poll_interval=0.2)
    with TestClient(create_app(settings)) as client:
        host = FakeChatGPTHost(client)
        start = time.monotonic()
        queued = host.call("run_python_script", {"code": "import time\nprint('started', flush=True)\ntime.sleep(65)\nprint('long-worker-finished', flush=True)", "cwd": str(tmp_path), "timeout": 90})
        assert time.monotonic() - start < 3
        seen_running = False
        probes = 0
        while time.monotonic() - start < 85:
            began = time.monotonic()
            assert client.get("/health").status_code == 200
            state = host.call("task_status", {"task_id": queued["task_id"]})
            assert time.monotonic() - began < 3
            probes += 1
            seen_running |= state["status"] == "running"
            if state["terminal"]:
                break
            time.sleep(1)
        assert seen_running and state["status"] == "completed", state
        assert state["result"]["data"]["result"]["return_code"] == 0
        output = host.call("read_task_output", {"task_id": queued["task_id"], "max_chars": 2000})
        assert "long-worker-finished" in output["result"]["data"]["content"]
        logs = host.call("task_log", {"task_id": queued["task_id"], "limit": 1})
        assert logs["next_cursor"] is not None
        assert host.call("task_log", {"task_id": queued["task_id"], "cursor": logs["next_cursor"]})["entries"]
        assert probes >= 50


def test_result_redaction_and_nonzero_returncode():
    from web_mcp.result_contract import error_result, normalize_result
    failed = error_result("example", PermissionError("token=do-not-print Authorization=Bearer abcdefgh"))
    assert "do-not-print" not in json.dumps(failed)
    assert "abcdefgh" not in json.dumps(failed)
    assert normalize_result("runner", {"return_code": 3})["ok"] is False
    for status in ("queued", "running"):
        result = normalize_result("runner", {"status": status, "task_id": "task-test"})
        assert result["ok"] and not result["terminal"]