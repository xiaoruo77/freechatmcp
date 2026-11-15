"""Resume from proven page state without inventing an old click result."""
from __future__ import annotations

import json
import time

import pytest

from test_edge_background_policy import BROWSER, GEN, PROTOCOL, EdgeRuntime
from web_tools.unified_browser import UnifiedBrowserService


ORIGINAL = "continuation-original-click"
EXPLANATION = "The exact tab was read again. Continue from that observed page, keeping the earlier click result unknown without replaying it."


@pytest.fixture
def setup(tmp_path):
    runtime = EdgeRuntime()
    original_dispatch = runtime.dispatch
    async def dispatch(browser_id, generation, method, payload, timeout):
        if method == "read_page" and payload.get("mode") in {"tab_metadata", "tab_presence"}:
            runtime.calls.append((method, dict(payload)))
            matches = [t for t in runtime.tabs if t["tab_id"] == payload["tab_id"]]
            mode = payload["mode"]
            return {"tab_id": payload["tab_id"], "url": matches[0]["url"] if matches else "",
                "text": "", "dom_read": False, "verified": True,
                "extraction": "browser_tab_presence" if mode == "tab_presence" else "browser_tab_metadata",
                **({"exists": bool(matches)} if mode == "tab_presence" else {}),
                "background_protocol": PROTOCOL}
        return await original_dispatch(browser_id, generation, method, payload, timeout)
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


def click_args():
    return {"execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed"}


async def create_unknown(runtime, service):
    async def lost(method, payload):
        assert method == "click"
        return {"ok": False, "verified": False, "action_may_have_run": True,
            "background_protocol": PROTOCOL, "tab_id": "7",
            "error": {"code": "extension_background_context_changed"}}
    runtime.handler = lost
    value = await service.control(BROWSER, "click", "7", click_args(), ORIGINAL, True, 2000)
    runtime.handler = None
    assert value["status"] == "outcome_unknown"
    return value


async def read(service, tab="7", **arguments):
    return await service.control(BROWSER, "read", tab, {"execution_mode": "extension_background", **arguments}, timeout=2000)


async def resume(service, observation, *, tab="7", **overrides):
    args = {"operation_id": ORIGINAL, "observation_id": observation["observation_id"],
        "decision": "continue_from_observed_page", "acknowledge_unknown": True, "evidence": EXPLANATION,
        **overrides}
    return await service.control(BROWSER, "reconcile", tab, args, confirm=True, timeout=2000)


def click_count(runtime):
    return sum(method == "click" for method, _ in runtime.effects)


@pytest.mark.parametrize("changed_page", [False, True])
async def test_continuation_retires_tracking_without_claiming_success_or_replaying(setup, changed_page):
    runtime, service = setup
    await create_unknown(runtime, service)
    if changed_page:
        runtime.tabs[0]["url"] = "https://publisher.test/now-observed"
    observation = await read(service)
    value = await resume(service, observation)
    assert value["ok"] and value["status"] == "reconciled_observed_page"
    assert value["evidence"] == {"executed": None, "verified": False}
    assert value["result"]["original_outcome"] == "unknown"
    assert value["result"]["operation_replayed"] is False
    assert value["result"]["repeat_original_write_allowed"] is False
    assert value["result"]["continuation_scope"] == "freshly_observed_page_only"
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    assert click_count(runtime) == 1


async def test_same_operation_id_after_continuation_returns_original_unknown_not_a_success(setup):
    runtime, service = setup
    await create_unknown(runtime, service)
    observed = await read(service)
    assert (await resume(service, observed))["ok"]
    duplicate = await service.control(BROWSER, "click", "7", click_args(), ORIGINAL, True, 2000)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    assert duplicate["status"] == "outcome_unknown" and duplicate["ok"] is False
    assert duplicate["evidence"] == {"executed": None, "verified": False}
    assert click_count(runtime) == 1
    assert service.receipts.get(ORIGINAL)["state"] == "reconciled_observed_page"


@pytest.mark.parametrize("acknowledgement", [False, None, 1, "true"])
async def test_continuation_requires_literal_acknowledgement_of_unknown_result(setup, acknowledgement):
    runtime, service = setup
    await create_unknown(runtime, service)
    value = await resume(service, await read(service), acknowledge_unknown=acknowledgement)
    assert not value["ok"] and value["error"]["code"] == "browser_observed_state_acknowledgement_required"
    assert service.receipts.pending(service.resource(BROWSER, "7")) == ORIGINAL
    assert click_count(runtime) == 1


@pytest.mark.parametrize("mode", ["tab_metadata", "tab_presence"])
async def test_metadata_observation_is_insufficient_to_continue_unknown_write(setup, mode):
    runtime, service = setup
    await create_unknown(runtime, service)
    observed = await read(service, mode=mode)
    value = await resume(service, observed)
    assert not value["ok"] and value["error"]["code"] == "browser_observed_state_acknowledgement_required"
    assert click_count(runtime) == 1


async def test_absent_target_cannot_continue_as_a_present_observed_page(setup):
    runtime, service = setup
    await create_unknown(runtime, service)
    runtime.tabs = [t for t in runtime.tabs if t["tab_id"] != "7"]
    observed = await read(service, mode="tab_presence")
    value = await resume(service, observed)
    assert not value["ok"]
    assert service.receipts.pending(service.resource(BROWSER, "7")) == ORIGINAL
    assert click_count(runtime) == 1


@pytest.mark.parametrize("variant", ["stale", "wrong_generation", "wrong_tab", "missing", "short_evidence"])
async def test_continuation_requires_fresh_exact_content_observation(setup, variant):
    runtime, service = setup
    await create_unknown(runtime, service)
    observed = await read(service, tab="9" if variant == "wrong_tab" else "7")
    if variant == "stale": service._observations[observed["observation_id"]]["at"] = time.monotonic() - 301
    elif variant == "wrong_generation": service._observations[observed["observation_id"]]["generation"] = "old-connection"
    elif variant == "missing": service._observations.clear()
    value = await resume(service, observed, **({"evidence": "Too short"} if variant == "short_evidence" else {}))
    assert not value["ok"] and click_count(runtime) == 1
    assert service.receipts.pending(service.resource(BROWSER, "7")) == ORIGINAL


@pytest.mark.parametrize("original_state", ["dispatching", "completed", "failed"])
async def test_a_live_dispatch_or_finished_operation_is_not_abandoned(setup, original_state):
    runtime, service = setup
    await create_unknown(runtime, service)
    row = service.receipts.get(ORIGINAL)
    payload = json.loads(row["result"])
    payload["status"] = original_state
    with service.receipts.connect() as db:
        db.execute("UPDATE unified_browser_operations SET state=?,result=? WHERE operation_id=?",
            (original_state, json.dumps(payload), ORIGINAL))
    value = await resume(service, await read(service))
    assert not value["ok"] and value["error"]["code"] == "browser_observed_state_acknowledgement_required"
    assert click_count(runtime) == 1


@pytest.mark.parametrize("change", ["unrelated_tab", "focused_window", "unrelated_tab_and_focus"])
async def test_v2_background_control_allows_user_activity_on_unrelated_targets(tmp_path, change):
    runtime = EdgeRuntime()
    runtime.browser["background_policy"] = "target_inactive_v2"
    def user_activity(_):
        if change != "focused_window": runtime.select("9")
        if change != "unrelated_tab": runtime.focused_window = "2"
    runtime.before_check = user_activity
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "click", "7", click_args(), "v2-independent-click", True, 2000)
    assert value["ok"], value
    payload = next(payload for method, payload in runtime.effects if method == "click")
    assert payload["independent_target_guard"] is True and payload["expected_target_window_id"] == "1"
    assert any(frame["unrelated_user_activity_observed"] for frame in value["background_execution"]["snapshots"])
    assert next(tab for tab in runtime.tabs if tab["tab_id"] == "7")["active"] is False
    assert runtime.foreground_checks == 0 and click_count(runtime) == 1


@pytest.mark.parametrize("change", ["target_active", "target_window_moved"])
async def test_v2_target_must_remain_inactive_in_its_observed_window(tmp_path, change):
    runtime = EdgeRuntime()
    runtime.browser["background_policy"] = "target_inactive_v2"
    def changed(_):
        if change == "target_active": runtime.select("7")
        else: runtime.tabs[0]["window_id"] = "2"
    runtime.before_check = changed
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "click", "7", click_args(), "v2-target-raced-click", True, 2000)
    assert not value["ok"] and value["evidence"]["executed"] is False
    assert click_count(runtime) == 0
    assert service.receipts.get("v2-target-raced-click") is None


async def test_old_extension_capability_keeps_strict_unrelated_activity_guard(tmp_path):
    runtime = EdgeRuntime()
    runtime.before_check = lambda _: runtime.select("9")
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "click", "7", click_args(), "v1-strict-click", True, 2000)
    assert value["error"]["code"] == "extension_background_active_changed" and click_count(runtime) == 0


@pytest.mark.parametrize("field,value", [("independent_target_guard", True), ("expected_target_window_id", "2"), ("background_policy", "target_inactive_v2")])
async def test_v2_internal_guards_cannot_be_forged_by_public_arguments(tmp_path, field, value):
    runtime = EdgeRuntime()
    runtime.browser["background_policy"] = "target_inactive_v2"
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    response = await service.control(BROWSER, "click", "7", {**click_args(), field: value}, "forged-v2-click", True, 2000)
    assert response["error"]["code"] == "browser_arguments_invalid" and click_count(runtime) == 0
