"""A chat's explicit foreground authorization selects only that call's route."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent, Tool
import pytest

from web_tools.desktop_policy import load_desktop_policy
from web_tools.windows_mcp import ALLOWED_ACTIONS, WindowsMCPBridge


def configured(tmp_path, mode="background_preferred", **overrides):
    config = tmp_path / "07_web_config" / "desktop_policy.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    values = dict(project_root=tmp_path, data_root=tmp_path / "data", extra={},
        windows_mcp_enabled=True, windows_mcp_control_enabled=True,
        windows_mcp_transport="streamable-http", windows_mcp_timeout=1)
    values.update(overrides)
    return SimpleNamespace(**values)


def foreground_bridge(tmp_path, monkeypatch, *, mode="background_preferred", **overrides):
    bridge = WindowsMCPBridge(configured(tmp_path, mode, **overrides))
    calls = []
    response = {"value": CallToolResult(content=[TextContent(type="text", text='{"state":"ready"}')])}
    http_checks = []
    monkeypatch.setattr(bridge, "_http_config", lambda: http_checks.append("checked"))

    class Session:
        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(name=action, inputSchema={"type": "object",
                "properties": {"text": {"type": "string"}, "label": {"type": "integer"},
                    "mode": {"type": "string"}, "name": {"type": "string"}},
                "additionalProperties": True}) for action in ALLOWED_ACTIONS])

        async def call_tool(self, action, arguments):
            calls.append((action, arguments))
            return response["value"]

    @asynccontextmanager
    async def connect():
        yield Session()

    monkeypatch.setattr(bridge, "_session", connect)
    return bridge, calls, response, http_checks


def test_background_preferred_policy_advertises_conditional_foreground(tmp_path):
    policy = load_desktop_policy(configured(tmp_path))
    assert policy["mode"] == "background_preferred"
    assert policy["host_input_allowed"] is False
    assert policy["foreground_available_on_request"] is True


@pytest.mark.asyncio
async def test_default_route_stays_background_even_with_consent_flag(tmp_path, monkeypatch):
    bridge, foreground, _, checks = foreground_bridge(tmp_path, monkeypatch)
    background = []
    monkeypatch.setattr("web_tools.background_desktop_client.configured", lambda settings: True)
    async def background_call(settings, action, arguments):
        background.append((action, arguments))
        return {"ok": True, "result": {"data": {"state": "background"}}, "evidence": {}}
    monkeypatch.setattr("web_tools.background_desktop_client.windows_action", background_call)
    value = await bridge.call("Snapshot", {}, foreground_authorized=True)
    assert value["ok"] and background == [("Snapshot", {})]
    assert not foreground and not checks and bridge._worker is None
    assert value["evidence"]["effective_execution_mode"] == "background"
    assert value["evidence"]["foreground_authorized"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action, arguments", [("Snapshot", {}), ("ControlStatus", {}), ("Click", {"label": 2})])
@pytest.mark.parametrize("consent", [False, None, "true", 1])
async def test_explicit_foreground_requires_real_authorization_before_transport(tmp_path, monkeypatch, action, arguments, consent):
    bridge, calls, _, checks = foreground_bridge(tmp_path, monkeypatch)
    value = await bridge.call(action, arguments, execution_mode="foreground", foreground_authorized=consent)
    assert value["error_code"] == "foreground_authorization_required"
    assert value["evidence"]["action_may_have_run"] is False
    assert not calls and not checks and bridge._worker is None


@pytest.mark.asyncio
async def test_authorized_foreground_uses_existing_transport_and_is_not_persistent(tmp_path, monkeypatch):
    bridge, calls, _, checks = foreground_bridge(tmp_path, monkeypatch)
    background = []
    monkeypatch.setattr("web_tools.background_desktop_client.configured", lambda settings: True)
    async def background_call(settings, action, arguments):
        background.append(action)
        return {"ok": True, "evidence": {}}
    monkeypatch.setattr("web_tools.background_desktop_client.windows_action", background_call)
    try:
        first = await bridge.call("Type", {"text": "authorized test", "label": 2},
            execution_mode="foreground", foreground_authorized=True)
        second = await bridge.call("Snapshot", {})
        assert first["ok"] and first["evidence"]["effective_execution_mode"] == "foreground"
        assert first["evidence"]["foreground_authorized"] is True
        assert calls == [("Type", {"text": "authorized test", "label": 2})]
        assert checks == ["checked"] and background == ["Snapshot"] and second["ok"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_strict_isolation_cannot_be_overridden_per_call(tmp_path, monkeypatch):
    bridge, calls, _, checks = foreground_bridge(tmp_path, monkeypatch, mode="isolated_only")
    value = await bridge.call("Snapshot", {}, execution_mode="foreground", foreground_authorized=True)
    assert value["error_code"] == "foreground_policy_denied"
    assert value["desktop_policy"]["foreground_available_on_request"] is False
    assert not calls and not checks


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides, code", [
    ({"windows_mcp_enabled": False}, "desktop_disabled"),
    ({"windows_mcp_control_enabled": False}, "desktop_control_disabled"),
])
async def test_foreground_consent_keeps_backend_enablement_gates(tmp_path, monkeypatch, overrides, code):
    bridge, calls, _, checks = foreground_bridge(tmp_path, monkeypatch, **overrides)
    value = await bridge.call("Click", {"label": 1}, execution_mode="foreground", foreground_authorized=True)
    assert value["error_code"] == code and not calls and not checks


@pytest.mark.asyncio
async def test_foreground_preserves_upstream_user_takeover_refusal(tmp_path, monkeypatch):
    bridge, calls, response, _ = foreground_bridge(tmp_path, monkeypatch)
    response["value"] = CallToolResult(isError=True, content=[TextContent(type="text", text='{"code":"USER_CONTROL","status":{"state":"user"}}')])
    try:
        value = await bridge.call("Click", {"label": 1}, execution_mode="foreground", foreground_authorized=True)
        assert not value["ok"] and value["error_code"] == "desktop_action_failed"
        assert "USER_CONTROL" in value["error"]["message"] and len(calls) == 1
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_foreground_keeps_lock_screen_preflight(tmp_path, monkeypatch):
    bridge, calls, _, _ = foreground_bridge(tmp_path, monkeypatch, windows_mcp_transport="stdio")
    monkeypatch.setattr("web_tools.windows_mcp.interactive_desktop_state", lambda: {"available": False,
        "error_code": "desktop_locked_or_secure"})
    value = await bridge.call("Snapshot", {}, execution_mode="foreground", foreground_authorized=True)
    assert value["error_code"] == "desktop_locked_or_secure" and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "", None, [], {}])
async def test_invalid_execution_mode_never_selects_a_transport(tmp_path, monkeypatch, mode):
    bridge, calls, _, checks = foreground_bridge(tmp_path, monkeypatch)
    value = await bridge.call("Snapshot", {}, execution_mode=mode, foreground_authorized=True)
    assert value["error_code"] == "invalid_arguments" and not calls and not checks


@pytest.mark.asyncio
async def test_interactive_policy_keeps_legacy_callers_working(tmp_path, monkeypatch):
    bridge, calls, _, _ = foreground_bridge(tmp_path, monkeypatch, mode="interactive")
    try:
        value = await bridge.call("ControlStatus", {})
        assert value["ok"] and calls == [("ControlStatus", {})]
        assert value["evidence"]["authorization_source"] == "interactive_policy"
    finally:
        await bridge.close()
