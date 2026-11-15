"""Keep authenticated Edge DOM control separate from physical desktop input."""
from __future__ import annotations

import json

import pytest

from web_tools.unified_browser import BrowserControlError, UnifiedBrowserService, WIRE


BROWSER = "browser_12345678-1234-1234-1234-123456789abc"
GEN = "edge-background-generation-1"
PROTOCOL = "xiaoruo1-edge-dom-background/1"
BACKGROUND_ACTIONS = ["open_tab", "close_tab", "navigate", "read", "click", "type",
                      "press", "select", "scroll", "wait"]


class EdgeRuntime:
    def __init__(self):
        self.browser = {
            "browser_id": BROWSER, "connection_generation": GEN,
            "adapter": "authenticated_extension", "browser_kind": "edge",
            "browser_family": "edge", "stable_identity": True,
            "connected": True, "supported_actions": list(WIRE),
            "background_protocol": PROTOCOL,
            "background_actions": list(BACKGROUND_ACTIONS),
        }
        self.tabs = [
            {"tab_id": "7", "window_id": "1", "active": False,
             "url": "https://fixture.test/form"},
            {"tab_id": "8", "window_id": "1", "active": True,
             "url": "https://fixture.test/user"},
            {"tab_id": "9", "window_id": "1", "active": False,
             "url": "https://fixture.test/other"},
            {"tab_id": "28", "window_id": "2", "active": True,
             "url": "https://fixture.test/second-window"},
        ]
        self.active_tabs = {"1": "8", "2": "28"}
        self.focused_window = "1"
        self.calls = []
        self.foreground_checks = 0
        self.background_checks = 0
        self.before_check = None
        self.handler = None

    def select(self, tab_id):
        target = next(t for t in self.tabs if t["tab_id"] == tab_id)
        window_id = target["window_id"]
        self.active_tabs[window_id] = tab_id
        for item in self.tabs:
            if item["window_id"] == window_id:
                item["active"] = item["tab_id"] == tab_id

    async def discover(self):
        return {"ok": True, "browsers": [dict(self.browser)]}

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        assert browser_id == BROWSER and generation == GEN and timeout > 0
        self.calls.append((method, dict(payload)))
        if method == "list_tabs":
            return {
                "tabs": [dict(t) for t in self.tabs], "verified": True,
                "adapter": "authenticated_extension",
                "background_protocol": PROTOCOL,
                "background_state": {
                    "active_tabs": dict(self.active_tabs),
                    "focused_window_id": self.focused_window,
                },
            }
        if self.handler:
            return await self.handler(method, payload)
        target_id = str(payload.get("tab_id") or "")
        if method == "open_tab":
            target_id = "30"
            self.tabs.append({"tab_id": target_id, "window_id": "1", "active": False,
                              "url": payload["url"]})
        elif method == "close_tab":
            self.tabs = [t for t in self.tabs if t["tab_id"] != target_id]
        elif method == "navigate":
            next(t for t in self.tabs if t["tab_id"] == target_id)["url"] = payload["url"]
        target = next((t for t in self.tabs if t["tab_id"] == target_id), {})
        return {"ok": True, "verified": True, "tab_id": target_id,
                "url": target.get("url", "https://fixture.test/form"),
                "background_protocol": PROTOCOL,
                "text": "Fixture page", "milliseconds": payload.get("milliseconds", 0)}

    async def foreground_check(self):
        self.foreground_checks += 1
        raise BrowserControlError("desktop_user_active", "Physical desktop input belongs to the user")

    async def background_check(self):
        self.background_checks += 1
        if self.before_check:
            self.before_check(self.background_checks)
        return {"available": True, "unlocked": True, "secure_desktop": False,
                "session_active": True, "interactive": True,
                "source": "readonly_native_session_preflight", "token": "secret-not-for-receipts"}

    @property
    def effects(self):
        return [(method, payload) for method, payload in self.calls if method != "list_tabs"]


@pytest.fixture
def setup(tmp_path):
    runtime = EdgeRuntime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
                                    runtime.dispatch, runtime.foreground_check,
                                    runtime.background_check)
    return runtime, service


async def background(service, action="click", tab="7", operation_id="edge-background-operation-1",
                     confirm=True, **arguments):
    return await service.control(BROWSER, action, tab, {
        "expected_generation": GEN, "execution_mode": "extension_background", **arguments,
    }, operation_id, confirm, 2000)


@pytest.mark.parametrize("action,arguments", [
    ("click", {"selector": "#button"}),
    ("type", {"selector": "#text", "text": "local test"}),
    ("select", {"selector": "#select", "option": "one"}),
    ("scroll", {"pixels": 100}),
    ("navigate", {"url": "https://fixture.test/next"}),
    ("close_tab", {}),
])
async def test_background_writes_use_session_check_without_claiming_desktop(setup, action, arguments):
    runtime, service = setup
    response = await background(service, action, **arguments)
    assert response["ok"], response
    assert response["evidence"] == {"executed": True, "verified": True}
    assert runtime.foreground_checks == 0 and runtime.background_checks >= 1
    assert runtime.active_tabs == {"1": "8", "2": "28"} and runtime.focused_window == "1"
    payload = runtime.effects[0][1]
    assert payload["background_only"] is True
    assert payload["expected_active_tabs"] == {"1": "8", "2": "28"}
    assert payload["expected_focused_window_id"] == "1"
    assert "execution_mode" not in payload and "expected_generation" not in payload
    saved = json.dumps(service.receipts.get("edge-background-operation-1"))
    assert "secret-not-for-receipts" not in saved


async def test_background_press_is_dispatched_without_global_keyboard_input(setup):
    runtime, service = setup
    response = await background(service, "press", selector="#text", key="Enter")
    assert response["evidence"]["executed"] is True
    assert response["status"] in {"completed", "dispatched_unverified"}
    assert runtime.foreground_checks == 0 and runtime.background_checks >= 1
    assert runtime.effects[0][0] == "press_key"
    assert runtime.effects[0][1]["background_only"] is True


async def test_open_creates_inactive_tab_without_switching_any_window(setup):
    runtime, service = setup
    response = await background(service, "open_tab", tab=None, url="https://fixture.test/new")
    assert response["ok"], response
    payload = runtime.effects[0][1]
    assert payload["active"] is False
    assert next(t for t in runtime.tabs if t["tab_id"] == "30")["active"] is False
    assert runtime.active_tabs == {"1": "8", "2": "28"} and runtime.focused_window == "1"
    assert runtime.foreground_checks == 0


async def test_background_can_run_when_no_edge_window_is_focused(setup):
    runtime, service = setup
    runtime.focused_window = None
    response = await background(service, selector="#button")
    assert response["ok"], response
    assert runtime.effects[0][1]["expected_focused_window_id"] is None
    assert runtime.focused_window is None
    assert runtime.foreground_checks == 0


@pytest.mark.parametrize("malformed", [
    "missing_state", "empty_active_map", "missing_window", "wrong_active_tab",
    "unknown_focused_window", "duplicate_active_tab", "wrong_protocol",
])
async def test_malformed_window_state_never_dispatches_a_write(setup, malformed):
    runtime, service = setup
    dispatch = runtime.dispatch
    async def invalid_state(browser_id, generation, method, payload, timeout):
        value = await dispatch(browser_id, generation, method, payload, timeout)
        if method == "list_tabs":
            if malformed == "missing_state":
                value.pop("background_state")
            elif malformed == "empty_active_map":
                value["background_state"]["active_tabs"] = {}
            elif malformed == "missing_window":
                value["background_state"]["active_tabs"].pop("2")
            elif malformed == "wrong_active_tab":
                value["background_state"]["active_tabs"]["1"] = "9"
            elif malformed == "unknown_focused_window":
                value["background_state"]["focused_window_id"] = "999"
            elif malformed == "duplicate_active_tab":
                value["tabs"][0]["active"] = True
            else:
                value["background_protocol"] = "unverified-protocol"
        return value
    service.dispatch = invalid_state
    response = await background(service)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects
    assert service.receipts.get("edge-background-operation-1") is None


@pytest.mark.parametrize("action,arguments", [("read", {}), ("wait", {"milliseconds": 1})])
async def test_background_observation_does_not_require_input_lease(setup, action, arguments):
    runtime, service = setup
    service.background_check = None
    response = await background(service, action, **arguments)
    assert response["ok"], response
    assert runtime.foreground_checks == runtime.background_checks == 0


@pytest.mark.parametrize("change", [
    {"adapter": "unverified_extension"}, {"browser_kind": "chromium_other"},
    {"stable_identity": False}, {"background_protocol": None},
    {"background_protocol": "unknown-protocol"}, {"background_actions": ["read"]},
])
async def test_background_requires_exact_edge_capability_before_write(setup, change):
    runtime, service = setup
    runtime.browser.update(change)
    response = await background(service)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects and runtime.foreground_checks == runtime.background_checks == 0
    assert service.receipts.get("edge-background-operation-1") is None


@pytest.mark.parametrize("tab", ["8", "28"])
async def test_active_tab_in_any_window_cannot_receive_background_writes(setup, tab):
    runtime, service = setup
    response = await background(service, tab=tab)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects


async def test_default_foreground_path_still_obeys_desktop_control_check(setup):
    runtime, service = setup
    response = await service.control(BROWSER, "click", "7", {
        "expected_generation": GEN, "selector": "#button",
    }, "edge-foreground-operation-1", True)
    assert response["error"]["code"] == "desktop_user_active"
    assert runtime.foreground_checks == 1 and runtime.background_checks == 0
    assert not runtime.effects


async def test_foreground_screenshot_cannot_bypass_desktop_focus_check(setup):
    runtime, service = setup
    response = await service.control(BROWSER, "screenshot", "7", {})
    assert response["error"]["code"] == "desktop_user_active"
    assert runtime.foreground_checks == 1 and runtime.background_checks == 0
    assert not runtime.effects


@pytest.mark.parametrize("action", ["screenshot", "activate_tab", "debug_attach", "debug_snapshot"])
async def test_focus_or_unimplemented_capture_actions_are_refused(setup, action):
    runtime, service = setup
    response = await background(service, action)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects and runtime.foreground_checks == runtime.background_checks == 0


async def test_background_writes_require_independent_session_check(setup):
    runtime, service = setup
    service.background_check = None
    response = await background(service)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects and runtime.foreground_checks == 0


async def test_locked_or_secure_session_refuses_before_dispatch(setup):
    runtime, service = setup
    async def locked():
        raise BrowserControlError("foreground_required", "Locked session or secure desktop")
    service.background_check = locked
    response = await background(service)
    assert response["error"]["code"] == "foreground_required"
    assert not runtime.effects and runtime.foreground_checks == 0


@pytest.mark.parametrize("checked", [
    None, {}, {"unlocked": False, "secure_desktop": False, "session_active": True},
    {"unlocked": True, "secure_desktop": True, "session_active": True},
    {"unlocked": True, "secure_desktop": False, "session_active": False},
    {"unlocked": 1, "secure_desktop": 0, "session_active": 1},
])
async def test_background_check_must_prove_unlocked_session_not_merely_return(setup, checked):
    runtime, service = setup
    async def incomplete_check():
        return checked
    service.background_check = incomplete_check
    response = await background(service)
    assert response["error"]["code"] == "foreground_required"
    assert response["evidence"]["executed"] is False
    assert not runtime.effects


@pytest.mark.parametrize("change", ["target_selected", "other_tab_selected", "window_focused"])
async def test_tab_or_window_race_during_session_check_is_not_dispatched(setup, change):
    runtime, service = setup
    def switch(_):
        if change == "window_focused":
            runtime.focused_window = "2"
        else:
            runtime.select("7" if change == "target_selected" else "9")
    runtime.before_check = switch
    response = await background(service)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert not runtime.effects
    assert service.receipts.get("edge-background-operation-1") is None


@pytest.mark.parametrize("change", ["active_tab", "focused_window"])
async def test_postdispatch_race_is_uncertain_durable_and_never_replayed(setup, change):
    runtime, service = setup
    async def changed(method, payload):
        if change == "active_tab":
            runtime.select("9")
        else:
            runtime.focused_window = "2"
        return {"ok": True, "verified": True}
    runtime.handler = changed
    response = await background(service)
    assert response["status"] == "outcome_unknown" and response["retryable"] is False
    assert response["evidence"] == {"executed": None, "verified": False}
    assert len(runtime.effects) == 1
    restarted = UnifiedBrowserService(service.receipts.path, runtime.discover, runtime.dispatch,
                                      runtime.foreground_check, runtime.background_check)
    duplicate = await background(restarted)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    runtime.select("8")
    runtime.focused_window = "1"
    fenced = await background(restarted, operation_id="edge-background-operation-2")
    assert fenced["error"]["code"] == "browser_target_fenced"
    assert len(runtime.effects) == 1


async def test_transient_open_can_reconcile_only_its_returned_created_tab(setup):
    runtime, service = setup
    created = []
    async def transient_open(method, payload):
        if method == "open_tab":
            tab_id = str(30 + len(created))
            created.append(tab_id)
            runtime.tabs.append({"tab_id": tab_id, "window_id": "1", "active": False,
                "url": "about:blank" if len(created) == 1 else payload["url"]})
            return {"ok": True, "verified": True, "tab_id": tab_id,
                    "background_protocol": PROTOCOL}
        target = next(t for t in runtime.tabs if t["tab_id"] == payload["tab_id"])
        return {"ok": True, "verified": True, "tab_id": target["tab_id"],
                "url": target["url"], "text": "Fresh created tab observation",
                "background_protocol": PROTOCOL}
    runtime.handler = transient_open
    operation_id = "edge-transient-open-operation"
    original = await background(service, "open_tab", tab=None, operation_id=operation_id,
                                url="https://fixture.test/new")
    assert original["status"] == "dispatched_unverified"
    assert original["result"]["tab_id"] == "30"
    assert service.receipts.pending(service.resource(BROWSER, None)) == operation_id
    blocked = await background(service, "open_tab", tab=None,
                               operation_id="edge-open-before-reconcile", url="https://fixture.test/other")
    assert blocked["error"]["code"] == "browser_target_fenced" and created == ["30"]
    wrong_read = await background(service, "read", tab="7")
    wrong = await service.control(BROWSER, "reconcile", "7", {
        "operation_id": operation_id, "observation_id": wrong_read["observation_id"],
        "decision": "verified", "evidence": "This is a different tab and must not release the pending open.",
    }, confirm=True)
    assert wrong["error"]["code"] == "browser_operation_not_found"
    next(t for t in runtime.tabs if t["tab_id"] == "30")["url"] = "https://fixture.test/new"
    observed = await background(service, "read", tab="30")
    reconciled = await service.control(BROWSER, "reconcile", "30", {
        "operation_id": operation_id, "observation_id": observed["observation_id"],
        "decision": "verified", "evidence": "Read the exact returned newly created tab after its navigation committed.",
    }, confirm=True)
    assert reconciled["status"] == "reconciled_verified" and reconciled["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) is None
    next_open = await background(service, "open_tab", tab=None,
        operation_id="edge-open-after-reconcile", url="https://fixture.test/other")
    assert next_open["ok"] and created == ["30", "31"]


async def test_open_postdispatch_race_retains_created_identity_for_exact_reconciliation(setup):
    runtime, service = setup
    async def opened_before_race(method, payload):
        assert method == "open_tab"
        runtime.tabs.append({"tab_id": "30", "window_id": "1", "active": False,
                             "url": payload["url"]})
        runtime.focused_window = "2"
        return {"ok": True, "verified": True, "tab_id": "30", "url": payload["url"],
                "background_protocol": PROTOCOL, "action_may_have_run": True,
                "text": "private-page-content-must-not-be-receipted",
                "token": "private-token-must-not-be-receipted"}
    runtime.handler = opened_before_race
    operation_id = "edge-open-raced-after-created"
    response = await background(service, "open_tab", tab=None, operation_id=operation_id,
                                url="https://fixture.test/new")
    assert response["status"] == "outcome_unknown" and response["retryable"] is False
    assert response["result"]["tab_id"] == "30"
    assert service.receipts.pending(service.resource(BROWSER, None)) == operation_id
    saved = json.dumps(service.receipts.get(operation_id))
    assert "private-page-content-must-not-be-receipted" not in saved
    assert "private-token-must-not-be-receipted" not in saved
    runtime.focused_window = "1"
    runtime.handler = None
    observed = await background(service, "read", tab="30")
    reconciled = await service.control(BROWSER, "reconcile", "30", {
        "operation_id": operation_id, "observation_id": observed["observation_id"],
        "decision": "verified", "evidence": "Read the exact returned created tab after restoring a stable observed focus state.",
    }, confirm=True)
    assert reconciled["ok"] and reconciled["status"] == "reconciled_verified"
    assert service.receipts.pending(service.resource(BROWSER, None)) is None
    assert sum(method == "open_tab" for method, _ in runtime.effects) == 1


@pytest.mark.parametrize("may_have_run", [False, True])
async def test_adapter_refusal_retains_no_effect_or_uncertain_fence(setup, may_have_run):
    runtime, service = setup
    async def refusal(method, payload):
        return {"ok": False, "background_protocol": PROTOCOL,
                "action_may_have_run": may_have_run,
                "error": {"code": "edge_guard_refused", "message": "Adapter guard refused"}}
    runtime.handler = refusal
    response = await background(service)
    assert response["error"]["code"] == "edge_guard_refused"
    assert response["status"] == ("outcome_unknown" if may_have_run else "failed")
    assert response["evidence"]["executed"] is (None if may_have_run else False)
    assert response["retryable"] is False
    pending = service.receipts.pending(service.resource(BROWSER, "7"))
    assert pending == ("edge-background-operation-1" if may_have_run else None)


@pytest.mark.parametrize("arguments,confirm,operation_id,code", [
    ({"expected_generation": GEN}, False, "edge-requirements-1", "browser_confirmation_required"),
    ({}, True, "edge-requirements-1", "browser_generation_required"),
    ({"expected_generation": "stale-generation"}, True, "edge-requirements-1", "browser_generation_changed"),
    ({"expected_generation": GEN}, True, None, "browser_operation_id_required"),
])
async def test_background_preserves_confirm_generation_and_receipt_requirements(
        setup, arguments, confirm, operation_id, code):
    runtime, service = setup
    response = await service.control(BROWSER, "click", "7", {
        "execution_mode": "extension_background", **arguments,
    }, operation_id, confirm, 2000)
    assert response["error"]["code"] == code
    assert not runtime.effects


@pytest.mark.parametrize("field,value", [
    ("background_only", True), ("active", False),
    ("expected_active_tabs", {"1": "7"}), ("expected_focused_window_id", "2"),
])
async def test_callers_cannot_forge_internal_background_guards(setup, field, value):
    runtime, service = setup
    response = await background(service, **{field: value})
    assert response["error"]["code"] == "browser_arguments_invalid"
    assert not runtime.effects
