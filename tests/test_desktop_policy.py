"""Desktop policy decisions must happen without contacting a GUI transport."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent, Tool
import pytest

from web_tools.desktop_policy import DesktopPolicyError, load_desktop_policy
from web_tools.windows_mcp import WindowsMCPBridge


def configured(tmp_path, mode="isolated_only", **overrides):
    config = tmp_path / "07_web_config" / "desktop_policy.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    values = {
        "project_root": tmp_path,
        "data_root": tmp_path / "data",
        "extra": {},
        "windows_mcp_enabled": True,
        "windows_mcp_control_enabled": True,
        "windows_mcp_timeout": 1,
        "windows_mcp_transport": "streamable-http",
    }
    values.update(overrides)
    return SimpleNamespace(**values), config


def block_transport(bridge, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("transport_or_desktop")
        raise AssertionError("Isolation policy must reject before transport, token reads or input")

    monkeypatch.setattr(bridge, "_http_config", forbidden)
    monkeypatch.setattr(bridge, "_session", forbidden)
    monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", forbidden)
    return attempts


def test_missing_configuration_is_isolated(tmp_path):
    settings = SimpleNamespace(project_root=tmp_path)
    policy = load_desktop_policy(settings)
    assert policy["mode"] == "isolated_only"
    assert policy["host_input_allowed"] is False
    assert policy["isolated_desktop_ready"] is False
    assert load_desktop_policy(SimpleNamespace())["mode"] == "isolated_only"


def test_trusted_missing_override_isolates_production_policy(tmp_path):
    settings, _ = configured(tmp_path)
    settings.extra["desktop_policy_config"] = tmp_path / "missing-test-policy.json"
    assert load_desktop_policy(settings)["mode"] == "isolated_only"


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize("action, arguments", [
    ("Snapshot", {}),
    ("Screenshot", {}),
    ("Click", {"loc": [1, 2]}),
    ("Type", {"text": "literal", "loc": [1, 2]}),
    ("Shortcut", {"shortcut": "ctrl+s"}),
    ("Scroll", {"loc": [1, 2], "direction": "down"}),
    ("App", {"mode": "launch", "name": "notepad"}),
])
@pytest.mark.asyncio
async def test_isolated_mode_blocks_every_gui_operation_before_transport(tmp_path, monkeypatch, transport, action, arguments):
    settings, _ = configured(tmp_path, windows_mcp_transport=transport)
    bridge = WindowsMCPBridge(settings)
    attempts = block_transport(bridge, monkeypatch)
    result = await bridge.call(action, arguments)
    assert result["ok"] is False
    assert result["error_code"] == "isolated_desktop_unavailable"
    assert result["retryable"] is False
    assert result["evidence"]["action_may_have_run"] is False
    assert result["desktop_policy"]["host_input_allowed"] is False
    assert result["desktop_policy"]["isolated_desktop_ready"] is False
    assert "不会自动回退" in result["error"]["message"]
    assert attempts == []
    assert bridge._worker is None


@pytest.mark.asyncio
async def test_isolated_control_status_is_honest_local_and_does_not_connect(tmp_path, monkeypatch):
    settings, _ = configured(tmp_path, windows_mcp_enabled=False, windows_mcp_control_enabled=False)
    bridge = WindowsMCPBridge(settings)
    attempts = block_transport(bridge, monkeypatch)
    result = await bridge.call("ControlStatus", {})
    assert result["ok"] is True and result["status"] == "completed"
    assert result["error"] is None
    data = result["result"]["data"]
    assert data["state"] == "isolated_unavailable"
    assert data["host_input_allowed"] is False
    assert data["isolated_desktop_ready"] is False
    assert data["backend"] == "unconfigured"
    assert set(data["background_alternatives"]) == {
        "file", "python", "notebook", "native_background_browser", "vscode_api", "word_com",
    }
    assert data["pending_application_backends"] == {
        "vscode_api": {"available": False, "status": "unconfigured"},
        "word_com": {"available": False, "status": "unconfigured"},
    }
    assert "虚拟机" not in result["result"]["summary"]
    assert result["evidence"]["transport"] == "local_policy"
    assert attempts == []
    assert bridge._worker is None


@pytest.mark.parametrize("body", [
    '{"mode": "unknown"}', '{}', '[]', '{"mode": null}', '{"mode": []}',
    '{broken', '{"mode": "isolated_only"' + " " * 17000,
])
@pytest.mark.parametrize("action", ["ControlStatus", "Click"])
@pytest.mark.asyncio
async def test_invalid_configuration_fails_closed_before_transport(tmp_path, monkeypatch, body, action):
    settings, config = configured(tmp_path)
    config.write_text(body, encoding="utf-8")
    bridge = WindowsMCPBridge(settings)
    attempts = block_transport(bridge, monkeypatch)
    result = await bridge.call(action, {})
    assert result["ok"] is False
    assert result["error_code"] == "desktop_policy_invalid"
    assert result["retryable"] is False
    assert result["evidence"]["action_may_have_run"] is False
    assert attempts == []
    assert bridge._worker is None


def test_unreadable_policy_fails_closed(tmp_path):
    settings, config = configured(tmp_path)
    config.unlink()
    config.mkdir()
    with pytest.raises(DesktopPolicyError):
        load_desktop_policy(settings)


def test_backend_declaration_cannot_pretend_an_isolated_desktop_is_ready(tmp_path):
    settings, config = configured(tmp_path)
    config.write_text(json.dumps({"mode": "isolated_only", "backend": "vm", "ready": True}), encoding="utf-8")
    policy = load_desktop_policy(settings)
    assert policy["backend"] == "unconfigured"
    assert policy["host_input_allowed"] is False
    assert policy["isolated_desktop_ready"] is False


@pytest.mark.asyncio
async def test_interactive_control_status_keeps_original_gate_and_adds_policy(tmp_path, monkeypatch):
    settings, _ = configured(tmp_path, mode="interactive", windows_mcp_transport="stdio")
    bridge = WindowsMCPBridge(settings)
    calls = []
    monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", lambda: {"available": True, "session_id": 7})

    class Session:
        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(name="ControlStatus", inputSchema={"type": "object"})])

        async def call_tool(self, action, arguments):
            calls.append(action)
            return CallToolResult(content=[TextContent(type="text", text='{"state":"user"}')])

    @asynccontextmanager
    async def connect():
        yield Session()

    monkeypatch.setattr(bridge, "_session", connect)
    try:
        result = await bridge.call("ControlStatus", {})
    finally:
        await bridge.close()
    assert result["ok"] is True
    assert '"state":"user"' in result["result"]["summary"]
    assert result["desktop_policy"]["mode"] == "interactive"
    assert calls == ["ControlStatus"]


@pytest.mark.asyncio
async def test_policy_reloaded_for_each_call_blocks_existing_bridge_after_mode_change(tmp_path, monkeypatch):
    settings, config = configured(tmp_path, mode="interactive")
    bridge = WindowsMCPBridge(settings)
    config.write_text('{"mode":"isolated_only"}', encoding="utf-8")
    attempts = block_transport(bridge, monkeypatch)
    assert (await bridge.call("Snapshot"))["error_code"] == "isolated_desktop_unavailable"
    assert attempts == []
