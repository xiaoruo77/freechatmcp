"""Recovery describes uncertain Edge effects without replay or private payloads."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from web_tools.unified_browser import UnifiedBrowserService, WIRE


BROWSER = "browser_12345678-1234-1234-1234-123456789abc"
GEN = "edge-recovery-generation-1"
PROTOCOL = "xiaoruo1-edge-dom-background/1"
START = "https://fixture.test/article"
FINISH = "https://fixture.test/article/fulltext"
PRIVATE = "PRIVATE-PAGE-BODY-MUST-NOT-ENTER-RECOVERY"


class Runtime:
    def __init__(self):
        self.generation = GEN
        self.tabs = [
            {"tab_id": "7", "window_id": "1", "active": False, "url": START},
            {"tab_id": "8", "window_id": "1", "active": True, "url": "https://fixture.test/user"},
        ]
        self.calls = []
        self.handler = None

    async def discover(self):
        return {"browsers": [{"browser_id": BROWSER,
            "connection_generation": self.generation, "connected": True,
            "adapter": "authenticated_extension", "browser_kind": "edge",
            "stable_identity": True, "supported_actions": list(WIRE),
            "background_protocol": PROTOCOL,
            "background_actions": ["open_tab", "close_tab", "navigate", "read", "click", "type", "press", "select", "scroll", "wait"]}]}

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        assert browser_id == BROWSER and generation == self.generation and timeout > 0
        self.calls.append((method, deepcopy(payload)))
        if method == "list_tabs":
            return {"tabs": deepcopy(self.tabs), "background_protocol": PROTOCOL,
                "background_state": {"active_tabs": {"1": "8"}, "focused_window_id": None}}
        if self.handler:
            return await self.handler(method, payload)
        target = next(t for t in self.tabs if t["tab_id"] == payload["tab_id"])
        return {"verified": True, "tab_id": target["tab_id"], "url": target["url"],
            "text": PRIVATE, "background_protocol": PROTOCOL}

    async def session(self):
        return {"unlocked": True, "secure_desktop": False, "session_active": True}

    @property
    def writes(self):
        return [(method, payload) for method, payload in self.calls
            if method not in {"list_tabs", "read_page"}]


@pytest.fixture
def setup(tmp_path):
    runtime = Runtime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, background_check=runtime.session)
    return runtime, service


async def command(service, action="click", tab="7", op="edge-recovery-original", **arguments):
    return await service.control(BROWSER, action, tab,
        {"execution_mode": "extension_background", "expected_generation": GEN, **arguments},
        op, True, 2000)


def guarded_transition(**overrides):
    state = {"active_tabs": {"1": "8"}, "focused_window_id": None}
    return {"ok": True, "verified": False, "tab_id": "7",
        "background_protocol": PROTOCOL, "action_may_have_run": True,
        "input_semantics": "synthetic_dom_events_untrusted", "url_transition": True,
        "before_url": START, "after_url": FINISH,
        "background_execution": {"protocol": PROTOCOL, "before": deepcopy(state),
            "after": deepcopy(state), "target_inactive": True, "no_system_input": True},
        **overrides}


async def create_uncertain_click(runtime, service, receipt=None):
    async def click(method, payload):
        assert method == "click"
        return receipt if receipt is not None else {"ok": True, "verified": False,
            "tab_id": "7", "background_protocol": PROTOCOL, "text": PRIVATE,
            "token": "PRIVATE-TOKEN", "html": "PRIVATE-HTML"}
    runtime.handler = click
    original = await command(service, selector="#fulltext")
    assert original["status"] == "dispatched_unverified", original
    runtime.handler = None
    return original


async def reconcile(service, observation, decision="verified"):
    return await service.control(BROWSER, "reconcile", "7", {
        "operation_id": "edge-recovery-original", "observation_id": observation["observation_id"],
        "decision": decision, "evidence": "The exact current tab displays the resulting full text page after the original single click."}, confirm=True)


async def test_fenced_click_returns_bounded_actionable_recovery_without_replay(setup):
    runtime, service = setup
    await create_uncertain_click(runtime, service)
    refused = await command(service, op="edge-recovery-next", selector="#another")
    assert refused["error"]["code"] == "browser_target_fenced"
    recovery = refused["recovery"]
    assert recovery["blocking_operation_id"] == "edge-recovery-original"
    assert recovery["action"] == "click" and recovery["browser_id"] == BROWSER
    assert recovery["tab_id"] == "7" and recovery["expected_generation"] == GEN
    steps = json.dumps(recovery["next_steps"])
    assert all(action in steps for action in ("status", "read", "reconcile"))
    assert len(json.dumps(recovery)) < 4000
    assert all(marker not in json.dumps(recovery) for marker in (PRIVATE, "PRIVATE-TOKEN", "PRIVATE-HTML"))
    assert len(runtime.writes) == 1


async def test_fenced_new_tab_identifies_exact_created_target_and_original_operation(setup):
    runtime, service = setup
    async def open_unsettled(method, payload):
        assert method == "open_tab"
        runtime.tabs.append({"tab_id": "30", "window_id": "1", "active": False, "url": START})
        return {"ok": False, "verified": False, "action_may_have_run": True,
            "error": {"code": "extension_background_navigation_unsettled"},
            "tab_id": "30", "background_protocol": PROTOCOL}
    runtime.handler = open_unsettled
    first = await command(service, "open_tab", tab=None, url=START)
    assert first["status"] == "outcome_unknown"
    refused = await command(service, "open_tab", tab=None, op="edge-recovery-new-open", url=FINISH)
    recovery = refused["recovery"]
    assert recovery["blocking_operation_id"] == "edge-recovery-original"
    assert recovery["action"] == "open_tab" and recovery["tab_id"] == "30"
    assert len(runtime.writes) == 1 and len([t for t in runtime.tabs if t["tab_id"] == "30"]) == 1


@pytest.mark.parametrize("action", ["open_tab", "navigate"])
async def test_navigation_accepts_explicit_redirect_proof_only_with_fresh_exact_final_url(setup, action):
    runtime, service = setup
    async def redirected(method, payload):
        assert method == action
        tab_id = "30" if action == "open_tab" else "7"
        if action == "open_tab":
            runtime.tabs.append({"tab_id": tab_id, "window_id": "1", "active": False, "url": FINISH})
        else:
            runtime.tabs[0]["url"] = FINISH
        return {"ok": True, "verified": True, "tab_id": tab_id, "url": FINISH,
            "background_protocol": PROTOCOL, "navigation_verified": True,
            "requested_url": START, "final_url": FINISH, "created": action == "open_tab"}
    runtime.handler = redirected
    response = await command(service, action, tab=None if action == "open_tab" else "7", url=START)
    assert response["ok"] and response["evidence"]["verified"] is True, response
    assert len(runtime.writes) == 1


@pytest.mark.parametrize("created", [None, False, 1])
async def test_redirected_open_requires_explicit_created_receipt(setup, created):
    runtime, service = setup
    async def redirected(method, payload):
        runtime.tabs.append({"tab_id": "30", "window_id": "1", "active": False, "url": FINISH})
        return {"ok": True, "verified": True, "tab_id": "30", "url": FINISH,
            "background_protocol": PROTOCOL, "navigation_verified": True,
            "requested_url": START, "final_url": FINISH, "created": created}
    runtime.handler = redirected
    response = await command(service, "open_tab", tab=None, url=START)
    assert not response["ok"] and response["evidence"]["verified"] is False
    assert len(runtime.writes) == 1


@pytest.mark.parametrize("malformed", [
    "missing_proof", "false_proof", "truthy_proof", "wrong_requested", "wrong_final",
    "credential_final", "non_http_final", "missing_final", "wrong_protocol",
])
async def test_navigation_redirect_evidence_cannot_forge_success(setup, malformed):
    runtime, service = setup
    final_url = "https://user:secret@fixture.test/fulltext" if malformed == "credential_final" else "file:///private.txt" if malformed == "non_http_final" else FINISH
    async def invalid(method, payload):
        assert method == "navigate"
        runtime.tabs[0]["url"] = final_url
        value = {"ok": True, "verified": True, "tab_id": "7", "url": final_url,
            "background_protocol": PROTOCOL, "navigation_verified": True,
            "requested_url": START, "final_url": final_url}
        if malformed == "missing_proof": value.pop("navigation_verified")
        elif malformed == "false_proof": value["navigation_verified"] = False
        elif malformed == "truthy_proof": value["navigation_verified"] = 1
        elif malformed == "wrong_requested": value["requested_url"] = "https://fixture.test/other"
        elif malformed == "wrong_final": value["final_url"] = "https://fixture.test/other"
        elif malformed == "missing_final": value.pop("final_url")
        elif malformed == "wrong_protocol": value["background_protocol"] = "unverified-protocol"
        return value
    runtime.handler = invalid
    response = await command(service, "navigate", url=START)
    assert not response["ok"] and response["evidence"]["verified"] is False
    assert len(runtime.writes) == 1


async def test_guarded_same_origin_click_can_reconcile_a_recorded_transition(setup):
    runtime, service = setup
    await create_uncertain_click(runtime, service, guarded_transition())
    runtime.tabs[0]["url"] = FINISH
    observed = await command(service, "read", op=None)
    result = await reconcile(service, observed)
    assert result["ok"] and result["status"] == "reconciled_verified", result
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    assert len(runtime.writes) == 1


@pytest.mark.parametrize("variant", [
    "legacy_no_evidence", "wrong_protocol", "missing_no_input", "target_active",
    "active_map_changed", "focused_changed", "missing_before", "wrong_before_url",
    "wrong_after_url", "cross_origin", "credential_url", "missing_semantics",
    "missing_transition", "not_applied", "new_generation",
])
async def test_changed_page_reconciliation_requires_every_recorded_edge_guard(setup, variant):
    runtime, service = setup
    receipt = guarded_transition()
    if variant == "legacy_no_evidence": receipt = {"ok": True, "verified": False, "background_protocol": PROTOCOL}
    elif variant == "wrong_protocol": receipt["background_protocol"] = "unverified-protocol"
    elif variant == "missing_no_input": receipt["background_execution"].pop("no_system_input")
    elif variant == "target_active": receipt["background_execution"]["target_inactive"] = False
    elif variant == "active_map_changed": receipt["background_execution"]["after"]["active_tabs"]["1"] = "7"
    elif variant == "focused_changed": receipt["background_execution"]["after"]["focused_window_id"] = "1"
    elif variant == "missing_before": receipt["background_execution"].pop("before")
    elif variant == "wrong_before_url": receipt["before_url"] = "https://fixture.test/different"
    elif variant == "wrong_after_url": receipt["after_url"] = "https://fixture.test/different"
    elif variant == "cross_origin": receipt["after_url"] = "https://other.test/fulltext"
    elif variant == "credential_url": receipt["after_url"] = "https://user:secret@fixture.test/fulltext"
    elif variant == "missing_semantics": receipt.pop("input_semantics")
    elif variant == "missing_transition": receipt.pop("url_transition")
    # An invalid protocol must not make the initial action successful, but its
    # uncertain receipt can still be inspected; it cannot authorize recovery.
    original = await create_uncertain_click(runtime, service, receipt) if variant != "wrong_protocol" else None
    if variant == "wrong_protocol":
        async def invalid_protocol(method, payload): return receipt
        runtime.handler = invalid_protocol
        original = await command(service, selector="#fulltext")
        assert not original["ok"]
        runtime.handler = None
    runtime.tabs[0]["url"] = receipt.get("after_url", FINISH) if variant in {"cross_origin", "credential_url"} else FINISH
    if variant == "new_generation":
        runtime.generation = "edge-recovery-generation-2"
        observed = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_background"})
    else:
        observed = await command(service, "read", op=None)
    result = await reconcile(service, observed, "not_applied" if variant == "not_applied" else "verified")
    assert not result["ok"] and result["error"]["code"] == "browser_reconciliation_page_changed", result
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "edge-recovery-original"
    assert len(runtime.writes) == 1
