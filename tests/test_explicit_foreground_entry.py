"""Foreground consent survives public entry points without changing background defaults."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import logging
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest


CASES = [
    ("windows_snapshot", "Snapshot", {"use_vision": False, "use_dom": False}),
    ("windows_screenshot", "Screenshot", {}),
    ("windows_control_status", "ControlStatus", {}),
    ("windows_click", "Click", {"label": 2, "button": "left", "clicks": 1}),
    ("windows_type", "Type", {"text": "test", "label": 2, "clear": False, "press_enter": False}),
    ("windows_shortcut", "Shortcut", {"shortcut": "ctrl+a"}),
    ("windows_scroll", "Scroll", {"direction": "down", "wheel_times": 1, "type": "vertical"}),
    ("windows_app", "App", {"mode": "launch", "name": "notepad"}),
]
READ_TOOLS = {"windows_snapshot", "windows_screenshot", "windows_control_status"}


@pytest.fixture
async def foreground_registry(isolated_settings, tmp_path):
    from web_canvas.service import CanvasService
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    Path(isolated_settings.extra["desktop_policy_config"]).write_text(
        json.dumps({"mode": "background_preferred"}), encoding="utf-8")
    settings = replace(isolated_settings, orchestration_mode="chatgpt_direct", admin_mode=True,
        windows_mcp_control_enabled=True, workspace_root=tmp_path, allowed_workspaces=[tmp_path])
    db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    await db.init()
    registry = ToolRegistry(ToolContext(settings, db, CanvasService(db), logging.getLogger("foreground-entry-tests")))
    registry.direct_tools.desktop.call = AsyncMock(return_value={"ok": True, "status": "completed",
        "terminal": True, "result": {"summary": "Fixture observation", "data": {"observed": True}}})
    try:
        yield registry
    finally:
        await db.close()


@pytest.mark.parametrize("name,action,native", CASES)
@pytest.mark.parametrize("foreground", [False, True])
async def test_public_mcp_tools_preserve_explicit_mode_and_strip_wrappers(
    foreground_registry, monkeypatch, name, action, native, foreground
):
    from web_mcp import mcp_server

    monkeypatch.setattr(mcp_server, "_registry", foreground_registry)
    server = mcp_server.build_mcp(orchestration_mode="chatgpt_direct")
    request = {**native, "confirm": name not in READ_TOOLS or foreground}
    if foreground:
        request["execution_mode"] = "foreground"
    result = await server.call_tool(name, request)
    assert result.structuredContent["ok"] is True
    call = foreground_registry.direct_tools.desktop.call
    if foreground:
        call.assert_awaited_once_with(action, native, execution_mode="foreground", foreground_authorized=True)
    else:
        call.assert_awaited_once_with(action, native)


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
@pytest.mark.parametrize("confirm", [False, None, "true", 1])
async def test_foreground_reads_require_exact_consent_before_bridge(foreground_registry, name, confirm):
    with pytest.raises(PermissionError, match="confirm=true"):
        await foreground_registry.invoke(name, {"execution_mode": "foreground", "confirm": confirm})
    foreground_registry.direct_tools.desktop.call.assert_not_awaited()


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
@pytest.mark.parametrize("disabled", ["admin_mode", "windows_mcp_control_enabled"])
async def test_foreground_reads_keep_control_gates(foreground_registry, name, disabled):
    foreground_registry.ctx.settings = replace(foreground_registry.ctx.settings, **{disabled: False})
    with pytest.raises(PermissionError, match="disabled"):
        await foreground_registry.invoke(name, {"execution_mode": "foreground", "confirm": True})
    foreground_registry.direct_tools.desktop.call.assert_not_awaited()


async def test_confirmation_alone_does_not_select_foreground(foreground_registry):
    await foreground_registry.invoke("windows_control_status", {"confirm": True})
    foreground_registry.direct_tools.desktop.call.assert_awaited_once_with("ControlStatus", {})


@pytest.mark.parametrize("mode", [None, "", "auto", "Foreground", 1, [], {}])
async def test_invalid_modes_fail_before_bridge(foreground_registry, mode):
    with pytest.raises(ValueError, match="execution_mode"):
        await foreground_registry.invoke("windows_snapshot", {"execution_mode": mode, "confirm": True})
    foreground_registry.direct_tools.desktop.call.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"text": "outer field"}, {"foreground_authorized": True}])
async def test_ambiguous_argument_wrappers_fail_before_bridge(foreground_registry, extra):
    with pytest.raises(ValueError, match="additional outer fields"):
        await foreground_registry.invoke("windows_type", {"arguments": {"text": "nested"}, **extra,
            "execution_mode": "foreground", "confirm": True})
    foreground_registry.direct_tools.desktop.call.assert_not_awaited()


@pytest.mark.parametrize("orchestration_mode", ["legacy_sampling", "chatgpt_direct"])
async def test_all_public_windows_schemas_offer_explicit_foreground(orchestration_mode):
    from web_mcp import mcp_server

    server = mcp_server.build_mcp(orchestration_mode=orchestration_mode)
    schemas = {tool.name: tool.inputSchema for tool in await server.list_tools()}
    for name, _, _ in CASES:
        properties = schemas[name]["properties"]
        assert properties["execution_mode"]["enum"] == ["background", "foreground"]
        assert properties["execution_mode"]["default"] == "background"
        assert properties["confirm"]["type"] == "boolean"
        assert properties["confirm"]["default"] is False


@pytest.mark.parametrize("name,action,native", CASES)
async def test_generic_batches_require_dedicated_windows_calls(foreground_registry, tmp_path, name, action, native):
    with pytest.raises(ValueError, match="dedicated tool explicitly"):
        await foreground_registry.invoke("run_workflow", {"workflow": {"steps": [
            {"action": "write_file", "args": {"path": "must-not-be-created.txt", "content": "no"}},
            {"tool": name, "arguments": {**native, "execution_mode": "foreground", "confirm": True}},
        ]}})
    assert not (tmp_path / "must-not-be-created.txt").exists()
    foreground_registry.direct_tools.desktop.call.assert_not_awaited()


async def test_background_control_status_reports_real_conditional_policy(foreground_registry, monkeypatch):
    from web_tools import background_desktop_client
    from web_tools.windows_mcp import WindowsMCPBridge

    request = []
    def background_status(settings, action, arguments, *, vscode=False):
        request.append((action, arguments, vscode))
        return {"ok": True, "result": {"created": True}}
    monkeypatch.setattr(background_desktop_client, "configured", lambda settings: True)
    monkeypatch.setattr(background_desktop_client, "_request", background_status)
    result = await WindowsMCPBridge(foreground_registry.ctx.settings).call("ControlStatus")
    assert result["ok"] is True and request == [("status", {}, False)]
    data = result["result"]["data"]
    assert data["mode"] == "background_preferred"
    assert data["foreground_available_on_request"] is True
    assert data["host_input_allowed"] is False
    assert data["effective_execution_mode"] == "background"
    assert result["evidence"]["effective_execution_mode"] == "background"
    assert result["evidence"]["foreground_authorized"] is False


@pytest.fixture
def foreground_browser_bridge(foreground_registry, tmp_path, monkeypatch):
    """Import routes with a private fixture token and no real native IPC."""
    from web_config.settings import Settings

    token = tmp_path / "browser-test.token"
    token.write_text("fixture-browser-bridge-token", encoding="utf-8")
    monkeypatch.setenv("WEB_AGENT_DATA", str(tmp_path / "browser-data"))
    monkeypatch.setenv("WEB_AGENT_BROWSER_TOKEN_FILE", str(token))
    monkeypatch.setenv("WEB_AGENT_BROWSER_LOG", str(tmp_path / "browser.log"))
    monkeypatch.setattr(Settings, "load", lambda: foreground_registry.ctx.settings)
    name = "web_tools._explicit_foreground_entry_bridge_test"
    path = Path(__file__).resolve().parents[1] / "06_web_tools/web_tools/browser_bridge.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("action,mode", [("click", "extension_background"), ("read", "extension_dom")])
async def test_background_preferred_preserves_guarded_browser_routing(foreground_browser_bridge, monkeypatch, action, mode):
    module = foreground_browser_bridge
    assert module._isolated_browser_policy() is True
    monkeypatch.setattr(module.bridge, "browser_instances", AsyncMock(return_value={"browsers": [{
        "browser_id": "edge-fixture", "browser_kind": "edge", "connected": True,
        "dom_protocol": "xiaoruo1-edge-dom/1"}]}))
    control = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(module, "_browser_control_with_read_retries", control)
    await module.browser_dispatch(module.BrowserControlRequest(browser_id="edge-fixture", action=action,
        tab_id="7", arguments={}, confirm=True))
    forwarded = control.call_args.args[0]
    assert forwarded["arguments"]["execution_mode"] == mode
    assert forwarded["confirm"] is True
