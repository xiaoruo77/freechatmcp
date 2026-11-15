"""MCP compatibility routes must share the guarded Edge background policy."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest


BROWSER = "browser_12345678-1234-1234-1234-123456789abc"
GEN = "edge-background-generation-1"
PROTOCOL = "xiaoruo1-edge-dom-background/1"
DOM_PROTOCOL = "xiaoruo1-edge-dom/1"


@pytest.fixture
def current_bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    token = tmp_path / "test-bridge.token"
    token.write_text("test-background-bridge-token", encoding="utf-8")
    monkeypatch.setenv("WEB_AGENT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_AGENT_BROWSER_TOKEN_FILE", str(token))
    monkeypatch.setenv("WEB_AGENT_BROWSER_LOG", str(tmp_path / "bridge.log"))
    name = "web_tools._edge_background_routes_test"
    spec = importlib.util.spec_from_file_location(name, root / "06_web_tools/web_tools/browser_bridge.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.bridge, "native_browser_adapter", lambda: None)
    return module


@pytest.fixture
def routes(current_bridge, monkeypatch):
    module = current_bridge
    calls = []
    browsers = [{"browser_id": BROWSER, "connection_generation": GEN, "connected": True,
                 "browser_kind": "edge", "adapter": "authenticated_extension"}]
    async def instances():
        return {"browsers": [dict(b) for b in browsers]}
    async def dispatch(browser_id, generation, method, payload, timeout):
        assert method == "list_tabs" and browser_id == BROWSER and generation == GEN
        return {"tabs": [{"tab_id": "7", "active": False, "window_id": "1"}]}
    async def control(*args, **kwargs):
        if args:
            kwargs = dict(zip(("browser_id", "action", "tab_id", "arguments", "operation_id", "confirm"), args))
        calls.append(kwargs)
        return {"ok": True, "result": {"tab_id": "7", "guarded": True},
                "operation_id": kwargs.get("operation_id"), "evidence": {"executed": True}}
    async def direct_forbidden(*args, **kwargs):
        raise AssertionError("Legacy direct browser entry point must not bypass background policy")
    monkeypatch.setattr(module, "_isolated_browser_policy", lambda: True)
    monkeypatch.setattr(module.bridge, "browser_instances", instances)
    monkeypatch.setattr(module.bridge, "browser_dispatch", dispatch)
    monkeypatch.setattr(module.unified, "control", control)
    for method in ("open_tab", "navigate", "click", "type_text", "press_key", "select_option", "screenshot", "close_tab"):
        monkeypatch.setattr(module.bridge, method, direct_forbidden)
    return module, calls, browsers


@pytest.mark.parametrize("action,arguments,auto", [
    ("click", {"selector": "#button"}, True), ("open_tab", {"url": "https://fixture.test/"}, True),
    ("read", {}, True), ("screenshot", {}, True), ("wait", {}, True),
    ("status", {}, False), ("list_tabs", {}, False), ("downloads", {}, False),
    ("task_status", {"task_id": "task-1"}, False),
])
async def test_isolated_edge_dispatch_selects_guarded_mode_for_browser_work(routes, action, arguments, auto):
    module, calls, _ = routes
    body = module.BrowserControlRequest(browser_id=BROWSER, action=action, tab_id="7",
                                        arguments=arguments, confirm=True)
    await module.browser_dispatch(body)
    forwarded = calls[0]["arguments"]
    assert (forwarded.get("execution_mode") == "extension_background") is auto
    assert calls[0]["confirm"] is True and calls[0]["browser_id"] == BROWSER


@pytest.mark.parametrize("mode", ["foreground", "native_background", "extension_background"])
async def test_explicit_execution_policy_is_never_rewritten(routes, mode):
    module, calls, _ = routes
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="click",
        tab_id="7", arguments={"execution_mode": mode}, confirm=True))
    assert calls[0]["arguments"]["execution_mode"] == mode


async def test_interactive_policy_does_not_silently_choose_background(routes, monkeypatch):
    module, calls, _ = routes
    monkeypatch.setattr(module, "_isolated_browser_policy", lambda: False)
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="click",
                                                               tab_id="7", arguments={}))
    assert "execution_mode" not in calls[0]["arguments"]


@pytest.mark.parametrize("kind", ["xiaoruo_browser", "chromium_other"])
async def test_other_browser_adapters_do_not_inherit_edge_mode(routes, kind):
    module, calls, browsers = routes
    browsers[0]["browser_kind"] = kind
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="click",
                                                               tab_id="7", arguments={}))
    assert "execution_mode" not in calls[0]["arguments"]


@pytest.mark.parametrize("action,arguments,expected_mode", [
    ("read", {}, "extension_dom"), ("screenshot", {}, "extension_dom"),
    ("click", {"selector": "#button"}, "extension_background"),
    ("open_tab", {"url": "https://fixture.test/new"}, "extension_background"),
    ("click", {"execution_mode": "foreground", "selector": "#button"}, "extension_dom"),
    ("read", {"execution_mode": "foreground"}, "extension_dom"),
    ("click", {"execution_mode": "extension_background"}, "extension_background"),
    ("read", {"execution_mode": "native_background"}, "native_background"),
])
async def test_modern_edge_uses_dom_for_observation_and_explicit_browser_foreground(routes, action, arguments, expected_mode):
    module, calls, browsers = routes
    browsers[0].update(dom_protocol=DOM_PROTOCOL, dom_actions=["read", "screenshot", "click", "open_tab"])
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action=action,
        tab_id="7", arguments=arguments, confirm=True))
    assert calls[0]["arguments"]["execution_mode"] == expected_mode


@pytest.mark.parametrize("protocol", [None, "other-protocol"])
async def test_old_or_unknown_edge_capability_does_not_rewrite_explicit_foreground(routes, protocol):
    module, calls, browsers = routes
    browsers[0]["dom_protocol"] = protocol
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="read",
        tab_id="7", arguments={"execution_mode": "foreground"}))
    assert calls[0]["arguments"]["execution_mode"] == "foreground"


async def test_modern_edge_capability_does_not_change_non_edge_routing(routes):
    module, calls, browsers = routes
    browsers[0].update(browser_kind="xiaoruo_browser", dom_protocol=DOM_PROTOCOL)
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="read", tab_id="7", arguments={}))
    assert "execution_mode" not in calls[0]["arguments"]


async def test_modern_legacy_screenshot_uses_dom_policy_without_direct_fallback(routes):
    module, calls, browsers = routes
    browsers[0]["dom_protocol"] = DOM_PROTOCOL
    response = await module.screenshot(module.ScreenshotRequest(tab_id="7"))
    assert response["ok"] and calls[0]["action"] == "screenshot"
    assert calls[0]["arguments"]["execution_mode"] == "extension_dom"


@pytest.mark.parametrize("route,request_model,action,expected", [
    ("click", "LocatorRequest", "click", {"by": "css", "selector": "#button"}),
    ("type_text", "TypeRequest", "type", {"by": "css", "selector": "#button", "text": "text"}),
    ("press_key", "PressRequest", "press", {"by": "css", "selector": "#button", "key": "Enter"}),
    ("select_option", "SelectRequest", "select", {"by": "css", "selector": "#button", "option": "one"}),
    ("navigate", "NavigateRequest", "navigate", {"url": "https://fixture.test/next"}),
    ("close_tab", "TabRequest", "close_tab", {}),
    ("screenshot", "ScreenshotRequest", "screenshot", {}),
])
async def test_legacy_tab_routes_share_unified_guard_and_filtered_arguments(routes, route, request_model, action, expected):
    module, calls, _ = routes
    values = {"tab_id": "7", **expected}
    response = await getattr(module, route)(getattr(module, request_model)(**values))
    assert response["ok"] and response["guarded"]
    forwarded = calls[0]
    assert forwarded["action"] == action and forwarded["tab_id"] == "7"
    assert forwarded["confirm"] is True
    assert forwarded["operation_id"].startswith("legacy-background-")
    assert forwarded["arguments"]["execution_mode"] == "extension_background"
    assert forwarded["arguments"]["expected_generation"] == GEN
    for key, value in expected.items():
        assert forwarded["arguments"][key] == value
    assert "tab_id" not in forwarded["arguments"] and "timeout" not in forwarded["arguments"]
    assert None not in forwarded["arguments"].values()


async def test_legacy_open_uses_guarded_unified_path(routes):
    module, calls, _ = routes
    response = await module.open_tab(module.OpenTabRequest(url="https://fixture.test/new"))
    assert response["ok"] and calls[0]["action"] == "open_tab"
    assert calls[0]["tab_id"] is None
    assert calls[0]["arguments"] == {"url": "https://fixture.test/new",
        "execution_mode": "extension_background", "expected_generation": GEN}


async def test_legacy_guard_failure_never_falls_back_to_direct_browser(routes, monkeypatch):
    module, _, _ = routes
    refusal = {"ok": False, "status": "failed", "error": {"code": "foreground_required"}}
    async def denied(*args, **kwargs):
        return refusal
    monkeypatch.setattr(module.unified, "control", denied)
    response = await module.click(module.LocatorRequest(tab_id="7", selector="#button"))
    assert response == refusal


async def test_ambiguous_legacy_browser_is_refused_without_dispatch(routes):
    module, calls, browsers = routes
    browsers.append(dict(browsers[0]))
    response = await module.open_tab(module.OpenTabRequest(url="https://fixture.test/new"))
    assert response["error"]["code"] == "browser_disconnected_or_ambiguous" and not calls


def test_updated_routes_still_require_local_bridge_authentication(current_bridge):
    with TestClient(current_bridge.app) as client:
        for path, payload in [
            ("/v1/browser/dispatch", {"browser_id": BROWSER, "action": "click", "tab_id": "7"}),
            ("/v1/tabs/click", {"tab_id": "7", "selector": "#button"}),
            ("/v1/tabs/open", {"url": "https://fixture.test/new"}),
        ]:
            assert client.post(path, json=payload).status_code == 401
            assert client.post(path, json=payload,
                headers={"X-Xiaoruo1-Browser-Token": "invalid-token"}).status_code == 401


@pytest.mark.parametrize("protocol", [PROTOCOL, "unknown-protocol", None])
async def test_hello_discovery_advertises_only_recognized_background_protocol(current_bridge, protocol):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object()}
    await bridge._receive({"type": "hello_ack", "browser_family": "edge", "browser_kind": "edge",
        "instance_id": BROWSER.removeprefix("browser_"), "version": "1.9.1",
        "supported_actions": ["click", "read"], "background_protocol": protocol,
        "background_actions": ["click", "read", "screenshot", "arbitrary-command"]}, GEN)
    browser = (await bridge.browser_instances())["browsers"][0]
    assert browser["background_protocol"] == (PROTOCOL if protocol == PROTOCOL else None)
    assert browser["background_actions"] == ["click", "read"]
    assert browser["adapter"] == "authenticated_extension" and browser["stable_identity"] is True


@pytest.mark.parametrize("action", ["open_tab", "navigate", "click", "type", "press", "select", "scroll", "close_tab", "read", "screenshot"])
async def test_advertised_cooperative_edge_routes_omitted_modes_to_dom(routes, action):
    module, calls, browsers = routes
    browsers[0].update(dom_protocol=DOM_PROTOCOL, dom_policy="cooperative_v2")
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action=action,
        tab_id="7", arguments={}, confirm=True))
    assert calls[0]["arguments"]["execution_mode"] == "extension_dom"


@pytest.mark.parametrize("action", ["open_tab", "navigate", "click", "type", "press", "select", "scroll", "close_tab"])
async def test_dom_capable_edge_without_cooperative_policy_keeps_default_background_writes(routes, action):
    module, calls, browsers = routes
    browsers[0].update(dom_protocol=DOM_PROTOCOL)
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action=action,
        tab_id="7", arguments={}, confirm=True))
    assert calls[0]["arguments"]["execution_mode"] == "extension_background"


@pytest.mark.parametrize("mode", ["extension_background", "native_background"])
async def test_cooperative_capability_does_not_override_explicit_background_policy(routes, mode):
    module, calls, browsers = routes
    browsers[0].update(dom_protocol=DOM_PROTOCOL, dom_policy="cooperative_v2")
    await module.browser_dispatch(module.BrowserControlRequest(browser_id=BROWSER, action="click",
        tab_id="7", arguments={"execution_mode": mode}, confirm=True))
    assert calls[0]["arguments"]["execution_mode"] == mode


async def test_legacy_click_uses_advertised_cooperative_dom_route(routes):
    module, calls, browsers = routes
    browsers[0].update(dom_protocol=DOM_PROTOCOL, dom_policy="cooperative_v2")
    await module.click(module.LocatorRequest(tab_id="7", selector="#observed"))
    assert calls[0]["arguments"]["execution_mode"] == "extension_dom"


@pytest.mark.parametrize("policy", ["cooperative_v2", "unknown_policy", None])
async def test_hello_discovery_accepts_only_known_cooperative_policy(current_bridge, policy):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object()}
    await bridge._receive({"type": "hello_ack", "browser_family": "edge", "browser_kind": "edge",
        "instance_id": BROWSER.removeprefix("browser_"), "version": "1.9.8",
        "supported_actions": ["click", "read"], "dom_protocol": DOM_PROTOCOL,
        "dom_actions": ["click", "read"], "dom_policy": policy}, GEN)
    item = (await bridge.browser_instances())["browsers"][0]
    assert item["dom_policy"] == ("cooperative_v2" if policy == "cooperative_v2" else None)


async def test_tab_metadata_reads_exact_authenticated_connection_without_dom_injection(current_bridge, monkeypatch):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    bridge._connections["other-generation"] = {"websocket": object(),
        "instance_id": "abcdefab-1234-1234-1234-123456789abc"}
    calls = []
    async def metadata_only(method, payload, timeout, _connection_id=None):
        assert method == "list_tabs", "Metadata fallback must never inject read_page into a renderer"
        assert payload == {} and _connection_id == GEN
        calls.append((method, _connection_id))
        return {"background_protocol": PROTOCOL, "tabs": [
            {"tab_id": "7", "title": "T" * 700, "url": "https://fixture.test/new", "text": "not-dom-evidence"},
            {"tab_id": "8", "title": "Another tab", "url": "https://fixture.test/user"},
        ]}
    monkeypatch.setattr(bridge, "call", metadata_only)
    response = await bridge.browser_dispatch(BROWSER, GEN, "read_page", {
        "tab_id": "7", "mode": "tab_metadata", "selector": "#not-used",
    }, 2)
    assert calls == [("list_tabs", GEN)]
    assert response["tab_id"] == "7" and response["url"] == "https://fixture.test/new"
    assert response["dom_read"] is False and response["text"] == ""
    assert response["extraction"] == "browser_tab_metadata" and response["verified"] is True
    assert len(response["title"]) == 500 and response["background_protocol"] == PROTOCOL


async def test_tab_metadata_does_not_fall_back_after_selected_connection_disappears(current_bridge, monkeypatch):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections["replacement-generation"] = {
        "websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    async def forbidden_call(*args, **kwargs):
        raise AssertionError("Disconnected exact owner must not fall back to another connection")
    monkeypatch.setattr(bridge, "call", forbidden_call)
    with pytest.raises(current_bridge.BrowserBridgeError, match="absent or ambiguous"):
        await bridge.browser_dispatch(BROWSER, GEN, "read_page", {
            "tab_id": "7", "mode": "tab_metadata",
        }, 2)


@pytest.mark.parametrize("tabs", [[], [{"tab_id": "7"}, {"tab_id": "7"}], [{"tab_id": "8"}]])
async def test_tab_metadata_requires_one_exact_tab_owner(current_bridge, monkeypatch, tabs):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    calls = []
    async def listed(method, payload, timeout, _connection_id=None):
        calls.append((method, _connection_id))
        return {"tabs": tabs, "background_protocol": PROTOCOL}
    monkeypatch.setattr(bridge, "call", listed)
    with pytest.raises(current_bridge.BrowserBridgeError, match="absent or ambiguous"):
        await bridge.browser_dispatch(BROWSER, GEN, "read_page", {
            "tab_id": "7", "mode": "tab_metadata",
        }, 2)
    assert calls == [("list_tabs", GEN)]
