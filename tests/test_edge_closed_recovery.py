"""A proved absent tab retires a fence without claiming the original effect."""
from __future__ import annotations

import asyncio

import pytest

from test_edge_background_policy import BROWSER, GEN, PROTOCOL, EdgeRuntime
from test_edge_background_routes import current_bridge
from web_tools.unified_browser import UnifiedBrowserService


OP = "edge-closed-original-operation"
URL = "https://fixture.test/article"


@pytest.fixture
def setup(tmp_path):
    runtime = EdgeRuntime()
    original_dispatch = runtime.dispatch

    async def dispatch(browser_id, generation, method, payload, timeout):
        if method == "read_page" and payload.get("mode") == "tab_presence":
            assert browser_id == BROWSER and generation == GEN
            runtime.calls.append((method, dict(payload)))
            found = [t for t in runtime.tabs if t["tab_id"] == payload["tab_id"]]
            assert len(found) <= 1
            return {"tab_id": payload["tab_id"], "exists": bool(found),
                "url": found[0]["url"] if found else "", "text": "",
                "dom_read": False, "extraction": "browser_tab_presence",
                "verified": True, "background_protocol": PROTOCOL}
        return await original_dispatch(browser_id, generation, method, payload, timeout)

    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


async def request(service, action, tab, op=None, **arguments):
    return await service.control(BROWSER, action, tab,
        {"execution_mode": "extension_background", "expected_generation": GEN, **arguments},
        op, action != "read", 2000)


async def uncertain_open(runtime, service):
    async def opened(method, payload):
        assert method == "open_tab"
        runtime.tabs.append({"tab_id": "30", "window_id": "1", "active": False, "url": URL})
        return {"ok": False, "verified": False, "tab_id": "30",
            "background_protocol": PROTOCOL, "action_may_have_run": True,
            "error": {"code": "extension_background_navigation_unsettled"}}
    runtime.handler = opened
    response = await request(service, "open_tab", None, OP, url=URL)
    assert response["status"] == "outcome_unknown", response
    runtime.handler = None
    return response


async def retire(service, observation, tab="30", decision="target_closed"):
    return await service.control(BROWSER, "reconcile", tab, {
        "operation_id": OP, "observation_id": observation["observation_id"],
        "decision": decision,
        "evidence": "A fresh authenticated tab-presence observation proves the exact returned target is now absent; the earlier outcome is still unknown."}, confirm=True)


def writes(runtime):
    return [(method, payload) for method, payload in runtime.calls
        if method not in {"read_page", "list_tabs"}]


async def test_exact_created_tab_absence_retires_only_its_fence_without_replay(setup):
    runtime, service = setup
    await uncertain_open(runtime, service)
    runtime.tabs = [t for t in runtime.tabs if t["tab_id"] != "30"]
    observed = await request(service, "read", "30", mode="tab_presence")
    assert observed["ok"] and observed["result"]["exists"] is False, observed
    assert observed["result"]["dom_read"] is False and observed["result"]["text"] == ""
    result = await retire(service, observed)
    assert result["ok"] and result["status"] == "reconciled_target_closed", result
    assert result["result"]["original_outcome"] == "unknown"
    assert result["result"]["operation_replayed"] is False
    assert service.receipts.pending(service.resource(BROWSER, None)) is None
    row = service.receipts.get(OP)
    assert row["state"] == "reconciled_target_closed"
    assert len(writes(runtime)) == 1


async def test_present_target_cannot_be_retired_as_closed(setup):
    runtime, service = setup
    await uncertain_open(runtime, service)
    observed = await request(service, "read", "30", mode="tab_presence")
    assert observed["ok"] and observed["result"]["exists"] is True
    refused = await retire(service, observed)
    assert not refused["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) == OP
    assert len(writes(runtime)) == 1


async def test_other_absent_tab_observation_does_not_release_original_fence(setup):
    runtime, service = setup
    await uncertain_open(runtime, service)
    observed = await request(service, "read", "999", mode="tab_presence")
    assert observed["ok"] and observed["result"]["exists"] is False
    refused = await retire(service, observed)
    assert not refused["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) == OP
    assert len(writes(runtime)) == 1


@pytest.mark.parametrize("decision", ["verified", "not_applied"])
async def test_absence_never_proves_old_success_or_no_effect(setup, decision):
    runtime, service = setup
    await uncertain_open(runtime, service)
    runtime.tabs = [t for t in runtime.tabs if t["tab_id"] != "30"]
    observed = await request(service, "read", "30", mode="tab_presence")
    assert observed["ok"]
    refused = await retire(service, observed, decision=decision)
    assert not refused["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) == OP
    assert len(writes(runtime)) == 1


async def test_inflight_current_generation_dispatch_cannot_be_cancelled_by_absence(setup):
    runtime, service = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_open(method, payload):
        assert method == "open_tab"
        runtime.tabs.append({"tab_id": "30", "window_id": "1", "active": False, "url": URL})
        # Store only the already assigned target receipt while dispatch remains
        # live. This emulates a task receipt arriving before final completion.
        service.receipts.save(OP, {"status": "dispatching", "result": {"tab_id": "30"}})
        entered.set()
        await release.wait()
        return {"ok": False, "verified": False, "tab_id": "30", "background_protocol": PROTOCOL,
            "action_may_have_run": True, "error": {"code": "extension_background_failed"}}

    runtime.handler = slow_open
    pending = asyncio.create_task(request(service, "open_tab", None, OP, url=URL))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        runtime.tabs = [t for t in runtime.tabs if t["tab_id"] != "30"]
        observed = await request(service, "read", "30", mode="tab_presence")
        assert observed["ok"]
        refused = await retire(service, observed)
        assert not refused["ok"]
        assert service.receipts.get(OP)["state"] == "dispatching"
        assert len(writes(runtime)) == 1
    finally:
        release.set()
        await pending


@pytest.mark.parametrize("mode", [None, "tab_metadata", "fast"])
async def test_missing_target_is_not_accepted_by_other_read_modes(setup, mode):
    runtime, service = setup
    response = await request(service, "read", "999", **({} if mode is None else {"mode": mode}))
    assert not response["ok"] and not writes(runtime)


@pytest.mark.parametrize("malformed", ["missing_exists", "truthy_absence", "wrong_extraction", "claims_dom"])
async def test_forged_presence_response_cannot_authorize_closed_recovery(setup, malformed):
    runtime, service = setup
    await uncertain_open(runtime, service)
    runtime.tabs = [t for t in runtime.tabs if t["tab_id"] != "30"]
    dispatch = service.dispatch

    async def invalid(browser_id, generation, method, payload, timeout):
        value = await dispatch(browser_id, generation, method, payload, timeout)
        if method == "read_page" and payload.get("mode") == "tab_presence":
            if malformed == "missing_exists": value.pop("exists")
            elif malformed == "truthy_absence": value["exists"] = 0
            elif malformed == "wrong_extraction": value["extraction"] = "dom-text-native"
            elif malformed == "claims_dom": value["dom_read"] = True
        return value
    service.dispatch = invalid
    observed = await request(service, "read", "30", mode="tab_presence")
    if observed["ok"]:
        refused = await retire(service, observed)
        assert not refused["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) == OP
    assert len(writes(runtime)) == 1


@pytest.mark.parametrize("exists", [False, True])
async def test_authenticated_bridge_presence_uses_only_exact_connection_tab_list(current_bridge, monkeypatch, exists):
    module = current_bridge
    bridge = module.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    calls = []

    async def call(method, params=None, timeout=30, _connection_id=None):
        calls.append((method, _connection_id))
        assert method == "list_tabs" and _connection_id == GEN
        return {"tabs": [{"tab_id": "30", "url": URL, "title": "Fixture", "active": False}] if exists else [],
            "background_protocol": PROTOCOL}

    monkeypatch.setattr(bridge, "call", call)
    value = await bridge.browser_dispatch(BROWSER, GEN, "read_page", {
        "tab_id": "30", "mode": "tab_presence"}, 2)
    assert value["exists"] is exists and value["tab_id"] == "30"
    assert value["url"] == (URL if exists else "")
    assert value["text"] == "" and value["dom_read"] is False
    assert value["extraction"] == "browser_tab_presence" and value["verified"] is True
    assert calls == [("list_tabs", GEN)]


async def test_bridge_presence_rejects_duplicate_exact_tab_matches(current_bridge, monkeypatch):
    module = current_bridge
    bridge = module.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    async def call(method, params=None, timeout=30, _connection_id=None):
        assert method == "list_tabs" and _connection_id == GEN
        return {"tabs": [{"tab_id": "30", "url": URL}, {"tab_id": "30", "url": URL}],
            "background_protocol": PROTOCOL}
    monkeypatch.setattr(bridge, "call", call)
    with pytest.raises(module.BrowserBridgeError):
        await bridge.browser_dispatch(BROWSER, GEN, "read_page", {"tab_id": "30", "mode": "tab_presence"}, 2)


async def test_bridge_presence_cannot_use_a_replacement_connection(current_bridge, monkeypatch):
    module = current_bridge
    bridge = module.EdgeExtensionBridge()
    bridge._connections["new-generation"] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Stale connection must not read a replacement browser")
    monkeypatch.setattr(bridge, "call", forbidden)
    with pytest.raises(module.BrowserBridgeError):
        await bridge.browser_dispatch(BROWSER, GEN, "read_page", {"tab_id": "30", "mode": "tab_presence"}, 2)
