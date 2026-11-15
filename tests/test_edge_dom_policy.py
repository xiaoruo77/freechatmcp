"""Explicit Edge DOM work has browser authority without a Windows input lease."""
from __future__ import annotations

import json

import pytest

from test_edge_background_policy import EdgeRuntime, BROWSER, GEN
from web_tools.unified_browser import UnifiedBrowserService
from web_tools.extension_background import DOM_PROTOCOL, DOM_ACTIONS


class DomRuntime(EdgeRuntime):
    def __init__(self):
        super().__init__()
        self.browser.update(dom_protocol=DOM_PROTOCOL, dom_actions=sorted(DOM_ACTIONS))

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        if method == "list_tabs":
            return await super().dispatch(browser_id, generation, method, payload, timeout)
        value = await super().dispatch(browser_id, generation, method, payload, timeout)
        return {"dom_protocol": DOM_PROTOCOL, "input_scope": "browser_dom", "foreground_input_used": False, **value}


@pytest.fixture
def dom(tmp_path):
    runtime = DomRuntime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


async def operate(service, action="click", tab="7", operation_id="dom-test-operation-1", confirm=True, **arguments):
    return await service.control(BROWSER, action, tab, {"execution_mode": "extension_dom",
        "expected_generation": GEN, **arguments}, operation_id, confirm, 2000)


@pytest.mark.parametrize("action,args", [
    ("click", {"selector": "#button"}), ("type", {"selector": "#text", "text": "test"}),
    ("select", {"selector": "#select", "option": "one"}), ("scroll", {"pixels": 100}),
    ("navigate", {"url": "https://fixture.test/new"}), ("close_tab", {}),
])
async def test_dom_write_uses_session_facts_without_desktop_input_lease(dom, action, args):
    runtime, service = dom
    result = await operate(service, action, **args)
    assert result["ok"], result
    assert runtime.foreground_checks == 0 and runtime.background_checks == 2
    payload = runtime.effects[0][1]
    assert payload["browser_dom_only"] is True and payload["require_inactive"] is True
    assert "execution_mode" not in payload and "expected_generation" not in payload
    assert "background_only" not in payload
    assert "secret-not-for-receipts" not in json.dumps(service.receipts.get("dom-test-operation-1"))


async def test_dom_open_defaults_to_inactive_creation(dom):
    runtime, service = dom
    result = await operate(service, "open_tab", tab=None, url="https://fixture.test/new")
    assert result["ok"], result
    payload = runtime.effects[0][1]
    assert payload["activate"] is False and payload["browser_dom_only"] is True
    assert runtime.active_tabs == {"1": "8", "2": "28"}


@pytest.mark.parametrize("action", ["read", "screenshot"])
async def test_active_page_observation_never_claims_desktop_input(dom, action):
    runtime, service = dom
    service.background_check = None
    result = await operate(service, action, tab="8")
    assert result["ok"], result
    assert runtime.foreground_checks == runtime.background_checks == 0
    assert runtime.effects[0][1]["browser_dom_only"] is True


async def test_active_dom_write_receives_adapter_refusal_without_retry_or_fence(dom):
    runtime, service = dom
    async def refused(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "error": {"code": "browser_target_in_use"}}
    runtime.handler = refused
    result = await operate(service, tab="8", selector="#button")
    assert not result["ok"] and result["error"]["code"] == "browser_target_in_use"
    assert result["evidence"]["executed"] is False and result["retryable"] is False
    assert runtime.foreground_checks == 0 and len(runtime.effects) == 1
    assert service.receipts.pending(service.resource(BROWSER, "8")) is None


@pytest.mark.parametrize("change", [
    {"adapter": "native_ipc"}, {"browser_kind": "chromium_other"}, {"stable_identity": False},
    {"dom_protocol": None}, {"dom_protocol": "other"}, {"dom_actions": ["read"]},
])
async def test_dom_identity_capability_mismatch_has_no_fallback(dom, change):
    runtime, service = dom
    runtime.browser.update(change)
    result = await operate(service, selector="#button")
    assert result["error"]["code"] == "browser_dom_upgrade_required"
    assert not runtime.effects and runtime.foreground_checks == runtime.background_checks == 0


@pytest.mark.parametrize("checked", [None, {},
    {"unlocked": False, "secure_desktop": False, "session_active": True},
    {"unlocked": True, "secure_desktop": True, "session_active": True},
    {"unlocked": True, "secure_desktop": False, "session_active": False},
])
async def test_dom_mutations_require_real_unlocked_session_facts(dom, checked):
    runtime, service = dom
    async def invalid():
        return checked
    service.background_check = invalid
    result = await operate(service, selector="#button")
    assert not result["ok"] and result["error"]["code"] == "foreground_required"
    assert not runtime.effects and runtime.foreground_checks == 0


async def test_dom_mutations_without_session_callback_refuse(dom):
    runtime, service = dom
    service.background_check = None
    result = await operate(service, selector="#button")
    assert not result["ok"] and not runtime.effects and runtime.foreground_checks == 0


@pytest.mark.parametrize("arguments,confirm,operation_id,code", [
    ({}, False, "dom-req-1", "browser_confirmation_required"),
    ({"expected_generation": ""}, True, "dom-req-1", "browser_generation_required"),
    ({"expected_generation": "old"}, True, "dom-req-1", "browser_generation_changed"),
    ({}, True, None, "browser_operation_id_required"),
])
async def test_dom_preserves_write_confirm_generation_receipts(dom, arguments, confirm, operation_id, code):
    runtime, service = dom
    result = await operate(service, operation_id=operation_id, confirm=confirm, **arguments)
    assert result["error"]["code"] == code and not runtime.effects


@pytest.mark.parametrize("field,value", [("browser_dom_only", True), ("require_inactive", False),
    ("activate", True), ("window_id", "1"), ("background_only", False)])
async def test_internal_dom_authority_cannot_be_forged_in_public_arguments(dom, field, value):
    runtime, service = dom
    result = await operate(service, **{field: value})
    assert result["error"]["code"] == "browser_arguments_invalid" and not runtime.effects


async def test_dom_response_protocol_mismatch_is_unknown_after_write_and_not_replayed(dom):
    runtime, service = dom
    async def bad(method, payload):
        return {"verified": True, "dom_protocol": "wrong"}
    runtime.handler = bad
    result = await operate(service, selector="#button")
    assert result["status"] == "outcome_unknown" and result["retryable"] is False
    assert result["error"]["code"] == "browser_dom_response_unverified"
    duplicate = await operate(service, selector="#button")
    assert duplicate["duplicate_call"] is True and duplicate["redispatched"] is False
    assert len(runtime.effects) == 1 and runtime.foreground_checks == 0


async def test_empty_page_is_a_real_read_failure_without_observation(dom):
    runtime, service = dom
    async def empty(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "text": "", "error": {"code": "browser_page_empty"},
            "read_diagnostics": {"frames_seen": 1, "top_body_chars": 0}}
    runtime.handler = empty
    result = await operate(service, "read", tab="8")
    assert result["error"]["code"] == "browser_page_empty" and not result["ok"]
    assert "observation_id" not in result and runtime.foreground_checks == 0


async def test_original_foreground_path_keeps_desktop_gate(dom):
    runtime, service = dom
    result = await service.control(BROWSER, "click", "7", {"expected_generation": GEN},
        "dom-existing-foreground", True)
    assert result["error"]["code"] == "desktop_user_active"
    assert runtime.foreground_checks == 1 and runtime.background_checks == 0 and not runtime.effects
