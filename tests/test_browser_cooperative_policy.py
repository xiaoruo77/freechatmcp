"""Advertised cooperative DOM control keeps exact targets without a desktop lease."""
from __future__ import annotations

from copy import deepcopy

import pytest

from test_edge_dom_policy import DomRuntime, BROWSER, GEN
from test_browser_navigation_receipts import proof
from web_tools.browser_navigation import navigation_target
from web_tools.unified_browser import UnifiedBrowserService


@pytest.fixture
def setup(tmp_path):
    runtime = DomRuntime()
    runtime.browser["dom_policy"] = "cooperative_v2"
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


async def operate(service, action="click", tab="8", operation_id="cooperative-operation-0001", **arguments):
    return await service.control(BROWSER, action, tab, {"execution_mode": "extension_dom",
        "expected_generation": GEN, **arguments}, operation_id, True, 2000)


@pytest.mark.parametrize("action,arguments", [
    ("click", {"selector": "#observed"}),
    ("type", {"selector": "#query", "text": "test query"}),
    ("select", {"selector": "#options", "option": "one"}),
    ("scroll", {"pixels": 100}),
])
async def test_advertised_cooperative_policy_can_address_an_active_tab_without_desktop_input(setup, action, arguments):
    runtime, service = setup
    source_url = next(t for t in runtime.tabs if t["tab_id"] == "8")["url"]
    response = await operate(service, action, **arguments)
    assert response["ok"], response
    payload = next(payload for method, payload in runtime.effects if method != "list_tabs")
    assert payload["browser_dom_only"] is True and payload["cooperative_tab"] is True
    assert payload["require_inactive"] is False and payload["expected_target_url"] == source_url
    assert "background_only" not in payload and "execution_mode" not in payload
    assert runtime.foreground_checks == 0 and runtime.background_checks == 2
    assert runtime.active_tabs == {"1": "8", "2": "28"} and runtime.focused_window == "1"
    assert len(runtime.effects) == 1


@pytest.mark.parametrize("cooperative_policy", [None, "unknown_policy", "cooperative_v1"])
async def test_older_dom_capability_keeps_inactive_target_requirement(setup, cooperative_policy):
    runtime, service = setup
    runtime.browser["dom_policy"] = cooperative_policy
    response = await operate(service, tab="7", selector="#observed")
    assert response["ok"]
    payload = runtime.effects[0][1]
    assert payload["require_inactive"] is True
    assert "cooperative_tab" not in payload and "expected_target_url" not in payload
    assert runtime.foreground_checks == 0


@pytest.mark.parametrize("field,value", [
    ("cooperative_tab", True), ("require_inactive", False),
    ("expected_target_url", "https://attacker.test/replacement"),
    ("dom_policy", "cooperative_v2"), ("browser_dom_only", True),
])
async def test_public_arguments_cannot_forge_cooperative_authority_or_target_guard(setup, field, value):
    runtime, service = setup
    response = await operate(service, **{field: value})
    assert response["error"]["code"] == "browser_arguments_invalid"
    assert not runtime.effects and runtime.foreground_checks == runtime.background_checks == 0


async def test_expected_page_replacement_is_a_known_refusal_and_is_not_retried(setup):
    runtime, service = setup
    original_url = runtime.tabs[1]["url"]
    async def page_replaced(method, payload):
        assert payload["expected_target_url"] == original_url
        runtime.tabs[1]["url"] = "https://fixture.test/user-navigated"
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "error": {"code": "extension_dom_target_changed"}}
    runtime.handler = page_replaced
    response = await operate(service, selector="#observed")
    assert response["status"] == "failed" and response["error"]["code"] == "extension_dom_target_changed"
    assert response["retryable"] is False and response["evidence"]["executed"] is False
    assert service.receipts.pending(service.resource(BROWSER, "8")) is None
    assert len(runtime.effects) == 1 and runtime.foreground_checks == 0


async def test_explicit_background_mode_remains_strict_for_an_active_target(setup):
    runtime, service = setup
    runtime.browser["background_policy"] = "target_inactive_v2"
    response = await service.control(BROWSER, "click", "8", {
        "execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed"},
        "cooperative-explicit-background", True, 2000)
    assert response["error"]["code"] == "foreground_required"
    assert not runtime.effects and runtime.foreground_checks == 0


def test_active_navigation_target_requires_explicit_allow_active_and_valid_observed_proof():
    receipt = proof(expected="https://fixture.test/catalog/62")
    tabs = [{"tab_id": "7", "url": receipt["navigation_target"]["url"], "active": True}]
    base = {"source_tab_id": "7", "source_url": receipt["navigation_source"]["source_url"], "tabs": tabs}
    assert navigation_target(receipt, **base) is None
    assert navigation_target(receipt, **base, allow_active=True)["tab_id"] == "7"
    unrelated = deepcopy(receipt)
    unrelated["navigation_target"]["url"] = "https://unrelated.test/result"
    tabs[0]["url"] = unrelated["navigation_target"]["url"]
    assert navigation_target(unrelated, **base, allow_active=True) is None


@pytest.mark.parametrize("cooperative_policy,successful", [("cooperative_v2", True), (None, False)])
async def test_validated_active_click_navigation_is_a_continuation_only_for_cooperative_capability(setup, cooperative_policy, successful):
    runtime, service = setup
    runtime.browser["dom_policy"] = cooperative_policy
    source = runtime.tabs[1]["url"]
    destination = "https://publisher.test/observed-record"
    receipt = proof(expected=destination, source_id="8", source_url=source, target_id="8")
    async def navigate(method, payload):
        runtime.tabs[1]["url"] = destination
        return deepcopy(receipt)
    runtime.handler = navigate
    response = await operate(service, selector="#observed-link")
    assert response["ok"] is successful, response
    assert runtime.foreground_checks == 0 and len(runtime.effects) == 1
    if successful:
        assert response["next_target"]["tab_id"] == "8" and response["next_target"]["read_required"] is True
        assert service.receipts.pending(service.resource(BROWSER, "8")) is None
    else:
        assert response["status"] == "dispatched_unverified" and "next_target" not in response
