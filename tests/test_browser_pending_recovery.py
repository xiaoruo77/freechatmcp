"""A recoverable receipt must carry the exact next calls, including old rows."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from test_edge_dom_policy import DomRuntime
from test_edge_background_policy import BROWSER, GEN
from web_tools.unified_browser import UnifiedBrowserService
from web_mcp.result_contract import normalize_result


ORIGINAL = "pending-recovery-original-20261111"
EVIDENCE = "Fresh exact target was inspected. The original result remains unknown; resume from the observed current state without replaying it."


@pytest.fixture
def setup(tmp_path):
    runtime = DomRuntime()
    runtime.browser["dom_policy"] = "cooperative_v2"
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


async def call(service, action, tab="7", key=None, **args):
    return await service.control(BROWSER, action, tab,
        {"execution_mode": "extension_dom", "expected_generation": GEN, **args},
        key, action in {"click", "type", "open_tab"}, 2000)


async def make_pending(runtime, service, failure="unverified", action="click", tab="7"):
    async def handler(method, payload):
        if failure == "timeout":
            raise TimeoutError()
        if failure == "cancel":
            raise asyncio.CancelledError()
        return {"ok": True, "verified": False, "dispatched": True,
            "input_semantics": "synthetic_dom_events_untrusted"}
    runtime.handler = handler
    args = {"url": "https://fixture.test/new"} if action == "open_tab" else {"selector": "#observed"}
    try:
        return await call(service, action, tab, ORIGINAL, **args)
    finally:
        runtime.handler = None


def effects(runtime, method):
    return sum(name == method for name, _ in runtime.calls)


async def follow(service, payload):
    return await service.control(**payload, timeout=2000)


@pytest.mark.parametrize("failure", ["unverified", "timeout", "cancel"])
async def test_uncertain_and_historical_receipts_expose_real_status_and_read_calls(setup, failure):
    runtime, service = setup
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await make_pending(runtime, service, failure)
    else:
        result = await make_pending(runtime, service, failure)
        assert result["recovery"]["blocking_operation_id"] == ORIGINAL
    # A persisted pre-upgrade receipt has no recovery plan at all.
    with service.receipts.connect() as db:
        row = service.receipts.get(ORIGINAL)
        raw = json.loads(row["result"])
        raw.pop("recovery", None)
        db.execute("UPDATE unified_browser_operations SET result=? WHERE operation_id=?", (json.dumps(raw), ORIGINAL))
    status = await service.control(BROWSER, "status", operation_id=ORIGINAL, timeout=2000)
    plan = status["recovery"]
    assert plan["status_call"]["action"] == "status"
    assert plan["status_call"]["operation_id"] == ORIGINAL
    assert plan["read_call"]["tab_id"] == "7"
    assert plan["read_call"]["arguments"]["execution_mode"] == "extension_dom"
    assert plan["automatic_retry_allowed"] is plan["permission_denial"] is False
    assert "reconcile_call" not in plan
    assert (await follow(service, plan["status_call"]))["operation_id"] == ORIGINAL
    assert effects(runtime, "click") == 1


async def test_fresh_read_supplies_executable_reconciliation_then_new_write_without_replay(setup):
    runtime, service = setup
    pending = await make_pending(runtime, service)
    blocked = await call(service, "click", key="pending-recovery-new-click", selector="#next")
    assert blocked["error"]["code"] == "browser_target_fenced"
    status = await follow(service, blocked["recovery"]["status_call"])
    observed = await follow(service, status["recovery"]["read_call"])
    assert observed["ok"] and observed["evidence"]["verified"] is True
    assert observed["pending_operation"]["operation_id"] == ORIGINAL
    assert observed["recovery"]["reconciliation_ready"] is True
    normalized = normalize_result("browser_control", observed)
    assert normalized["ok"] and normalized["continuation_required"] is True
    assert normalize_result("browser_control", normalized) == normalized
    request = observed["recovery"]["reconcile_call"]
    assert request["arguments"]["evidence"] == ""
    assert not (await follow(service, request))["ok"]  # Evidence cannot be skipped.
    request["arguments"]["evidence"] = EVIDENCE
    resumed = await follow(service, request)
    assert resumed["result"]["original_outcome"] == "unknown"
    assert resumed["result"]["operation_replayed"] is False
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    duplicate = await call(service, "click", key=ORIGINAL, selector="#observed")
    assert duplicate["redispatched"] is False and duplicate["ok"] is False
    assert effects(runtime, "click") == 1
    assert (await call(service, "click", key="pending-recovery-new-click", selector="#next"))["ok"]
    assert effects(runtime, "click") == 2


async def test_status_of_exact_tab_surfaces_checkpoint_without_making_a_write(setup):
    runtime, service = setup
    await make_pending(runtime, service)
    status = await service.control(BROWSER, "status", "7")
    assert status["ok"] and status["recovery"]["blocking_operation_id"] == ORIGINAL
    assert effects(runtime, "click") == 1
    assert "pending_operation" not in await service.control(BROWSER, "status", "9")


@pytest.mark.parametrize("variant", ["wrong_tab", "wrong_generation", "stale", "active_dispatch"])
async def test_recovery_never_abandons_wrong_or_live_observation(setup, variant):
    runtime, service = setup
    await make_pending(runtime, service)
    if variant == "active_dispatch":
        with service.receipts.connect() as db:
            db.execute("UPDATE unified_browser_operations SET state='dispatching' WHERE operation_id=?", (ORIGINAL,))
    observed = await call(service, "read", tab="9" if variant == "wrong_tab" else "7")
    if variant in {"wrong_tab", "active_dispatch"}:
        assert not observed.get("recovery", {}).get("reconciliation_ready")
    if variant == "wrong_generation":
        service._observations[observed["observation_id"]]["generation"] = "different-generation"
    elif variant == "stale":
        service._observations[observed["observation_id"]]["at"] = time.monotonic() - 301
    resumed = await service.control(BROWSER, "reconcile", "7", {
        "operation_id": ORIGINAL, "observation_id": observed["observation_id"],
        "decision": "continue_from_observed_page", "acknowledge_unknown": True, "evidence": EVIDENCE}, confirm=True)
    assert not resumed["ok"]
    assert service.receipts.pending(service.resource(BROWSER, "7")) == ORIGINAL
    assert effects(runtime, "click") == 1


async def test_unknown_open_without_returned_tab_recovers_from_inventory_only(setup):
    runtime, service = setup
    result = await make_pending(runtime, service, "timeout", "open_tab", None)
    assert result["status"] == "outcome_unknown"
    status = await service.control(BROWSER, "status")
    assert status["pending_operation"]["operation_id"] == ORIGINAL
    observed = await follow(service, status["recovery"]["read_call"])
    request = observed["recovery"]["reconcile_call"]
    assert request["arguments"]["decision"] == "cancel_tracking" and "tab_id" not in request
    request["arguments"]["evidence"] = EVIDENCE
    resumed = await follow(service, request)
    assert resumed["status"] == "reconciled_cancelled_tracking"
    assert resumed["result"]["created_tab_identified"] is False
    assert resumed["result"]["original_outcome"] == "unknown"
    assert effects(runtime, "open_tab") == 1
    duplicate = await call(service, "open_tab", None, ORIGINAL, url="https://fixture.test/new")
    assert duplicate["ok"] is False and duplicate["redispatched"] is False
    assert (await call(service, "open_tab", None, "pending-recovery-distinct-tab", url="https://fixture.test/new"))["ok"]
    assert effects(runtime, "open_tab") == 2


@pytest.mark.parametrize("variant", ["no_ack", "wrong_generation", "wrong_tab_observation", "verified", "active_dispatch", "wrong_browser", "stale"])
async def test_browser_inventory_recovery_rejects_missing_or_mismatched_proof(setup, variant):
    runtime, service = setup
    await make_pending(runtime, service, "timeout", "open_tab", None)
    if variant == "active_dispatch":
        with service.receipts.connect() as db:
            db.execute("UPDATE unified_browser_operations SET state='dispatching' WHERE operation_id=?", (ORIGINAL,))
    observed = await call(service, "read") if variant == "wrong_tab_observation" else await service.control(BROWSER, "list_tabs")
    observation = service._observations[observed["observation_id"]]
    if variant == "wrong_generation": observation["generation"] = "different-generation"
    elif variant == "wrong_browser": observation["browser_id"] = "browser_other-generation"
    elif variant == "stale": observation["at"] = time.monotonic() - 301
    resumed = await service.control(BROWSER, "reconcile", arguments={"operation_id": ORIGINAL,
        "observation_id": observed["observation_id"], "decision": "verified" if variant == "verified" else "cancel_tracking",
        "acknowledge_unknown": variant != "no_ack", "evidence": EVIDENCE}, confirm=True)
    assert not resumed["ok"]
    assert service.receipts.pending(service.resource(BROWSER, None)) == ORIGINAL
    assert effects(runtime, "open_tab") == 1


async def test_failed_inventory_never_creates_reconciliation_proof(setup):
    runtime, service = setup
    await make_pending(runtime, service, "timeout", "open_tab", None)
    dispatch = runtime.dispatch
    async def failed_inventory(browser_id, generation, method, payload, timeout):
        if method == "list_tabs": return {"ok": False, "tabs": [], "error": {"code": "unavailable"}}
        return await dispatch(browser_id, generation, method, payload, timeout)
    service.dispatch = failed_inventory
    response = await service.control(BROWSER, "list_tabs")
    assert not response["ok"] and "observation_id" not in response
    assert service.receipts.pending(service.resource(BROWSER, None)) == ORIGINAL


async def test_old_background_receipt_refreshes_dom_capability_for_an_active_tab(setup):
    runtime, service = setup
    await make_pending(runtime, service)
    with service.receipts.connect() as db:
        row = service.receipts.get(ORIGINAL)
        raw = json.loads(row["result"])
        raw["result"] = {"background_protocol": "xiaoruo1-edge-dom-background/1"}
        db.execute("UPDATE unified_browser_operations SET result=? WHERE operation_id=?", (json.dumps(raw), ORIGINAL))
    runtime.select("7")
    status = await service.control(BROWSER, "status", operation_id=ORIGINAL)
    assert status["recovery"]["read_call"]["arguments"]["execution_mode"] == "extension_dom"
    observed = await follow(service, status["recovery"]["read_call"])
    assert observed["ok"] and observed["recovery"]["reconciliation_ready"]
    assert runtime.active_tabs["1"] == "7" and runtime.foreground_checks == 0


async def test_closed_tab_failure_exposes_presence_then_target_closed_reconciliation(setup):
    runtime, service = setup
    await make_pending(runtime, service)
    runtime.tabs = [tab for tab in runtime.tabs if tab["tab_id"] != "7"]
    original_dispatch = service.dispatch
    async def dispatch(browser_id, generation, method, payload, timeout):
        if method == "read_page" and payload.get("mode") == "tab_presence":
            runtime.calls.append((method, dict(payload)))
            return {"ok": True, "verified": True, "tab_id": "7", "exists": False, "text": "",
                "dom_read": False, "extraction": "browser_tab_presence", "dom_protocol": runtime.browser["dom_protocol"]}
        return await original_dispatch(browser_id, generation, method, payload, timeout)
    service.dispatch = dispatch
    missing = await call(service, "read")
    assert missing["error"]["code"] == "browser_tab_not_owned"
    observed = await follow(service, missing["recovery"]["presence_read_call"])
    request = observed["recovery"]["reconcile_call"]
    assert request["arguments"]["decision"] == "target_closed"
    request["arguments"]["evidence"] = "The exact original tab is absent in a fresh browser presence read. No old write is replayed and its result remains unknown."
    resumed = await follow(service, request)
    assert resumed["status"] == "reconciled_target_closed"
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    assert effects(runtime, "click") == 1


async def test_recovery_plan_never_copies_original_field_values_or_urls(setup):
    runtime, service = setup
    await make_pending(runtime, service)
    with service.receipts.connect() as db:
        row = service.receipts.get(ORIGINAL)
        raw = json.loads(row["result"])
        raw["result"].update(text="PRIVATE_FIELD_VALUE", url="https://fixture.test/_t/PRIVATE_URL_TOKEN")
        db.execute("UPDATE unified_browser_operations SET result=? WHERE operation_id=?", (json.dumps(raw), ORIGINAL))
    status = await service.control(BROWSER, "status", operation_id=ORIGINAL)
    plan = json.dumps(status["recovery"])
    assert "PRIVATE_FIELD_VALUE" not in plan and "PRIVATE_URL_TOKEN" not in plan
