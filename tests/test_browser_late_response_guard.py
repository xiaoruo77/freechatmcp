"""Late adapter results cannot replace an independently retired receipt."""
from __future__ import annotations

import asyncio

import pytest

from test_edge_background_policy import BROWSER, GEN
from test_edge_dom_policy import DomRuntime
from web_tools.unified_browser import BrowserControlError, BrowserReceipts, UnifiedBrowserService


@pytest.fixture
def setup(tmp_path):
    runtime = DomRuntime()
    runtime.browser["dom_policy"] = "cooperative_v2"
    database = tmp_path / "receipts.sqlite3"
    service = UnifiedBrowserService(database, runtime.discover, runtime.dispatch,
        runtime.foreground_check, runtime.background_check)
    return runtime, service, database


def _retire_from_other_connection(database, key):
    # Another process has inspected the new connection and retired the old
    # generation's dispatch while its HTTP adapter response is still in flight.
    # Use a distinct SQLite connection, just as that process would.
    other = BrowserReceipts(database)
    saved = {"ok": True, "status": "reconciled_observed_page", "browser_id": BROWSER,
        "tab_id": "7", "action": "click", "operation_id": key,
        "connection_generation": "fresh-observed-connection",
        "result": {"reconciliation": "continue_from_observed_page", "original_outcome": "unknown",
                   "operation_replayed": False, "repeat_original_write_allowed": False},
        "error": None, "retryable": False, "evidence": {"executed": None, "verified": False}}
    assert other.save(key, saved, expected_state="dispatching", expected_generation=GEN)


@pytest.mark.parametrize("late_outcome", ["success", "extension_error", "timeout"])
async def test_late_write_reply_returns_authoritative_unknown_receipt(setup, late_outcome):
    runtime, service, database = setup
    key = "late-response-guard-" + late_outcome
    async def late_reply(method, payload):
        assert method == "click"
        _retire_from_other_connection(database, key)
        if late_outcome == "timeout":
            raise TimeoutError()
        if late_outcome == "extension_error":
            return {"ok": False, "verified": False, "action_may_have_run": True,
                "error": {"code": "extension_dom_script_execution_error", "phase": "script_result"}}
        return {"ok": True, "verified": True, "tab_id": "7",
            "input_semantics": "synthetic_dom_events_untrusted"}
    runtime.handler = late_reply
    arguments = {"execution_mode": "extension_dom", "expected_generation": GEN, "selector": "#observed"}
    value = await service.control(BROWSER, "click", "7", arguments, key, True, 2000)
    assert value.get("late_response_ignored") is True
    assert value["status"] == "outcome_unknown" and value["ok"] is False
    assert value["tracking_state"] == "continued_from_observed_page"
    assert value["error"]["code"] == "browser_original_outcome_unknown"
    assert value["evidence"] == {"executed": None, "verified": False}
    assert value["result"]["original_outcome"] == "unknown"
    assert service.receipts.get(key)["state"] == "reconciled_observed_page"
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    runtime.handler = None
    duplicate = await service.control(BROWSER, "click", "7", arguments, key, True, 2000)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    assert duplicate["status"] == "outcome_unknown" and duplicate["ok"] is False
    assert sum(method == "click" for method, _ in runtime.calls) == 1


@pytest.mark.parametrize("failure", ["guard_error", "cancel"])
async def test_dom_open_keeps_known_created_tab_after_post_dispatch_failure(setup, failure):
    runtime, service, _ = setup
    key = "dom-open-post-dispatch-" + failure
    def post_check_fails(number):
        if number == 2:
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise BrowserControlError("desktop_state_unavailable", "Post-dispatch session facts were unavailable")
    runtime.before_check = post_check_fails
    arguments = {"execution_mode": "extension_dom", "expected_generation": GEN, "url": "https://fixture.test/new"}
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await service.control(BROWSER, "open_tab", None, arguments, key, True, 2000)
    else:
        value = await service.control(BROWSER, "open_tab", None, arguments, key, True, 2000)
        assert value["status"] == "outcome_unknown" and value["result"]["tab_id"] == "30"
    runtime.before_check = None
    status = await service.control(BROWSER, "status", operation_id=key, timeout=2000)
    assert status["result"]["tab_id"] == "30"
    read_call = status["recovery"]["read_call"]
    assert read_call["action"] == "read" and read_call["tab_id"] == "30"
    assert read_call["arguments"]["execution_mode"] == "extension_dom"
    observed = await service.control(**read_call, timeout=2000)
    assert observed["ok"] and observed["recovery"]["reconciliation_ready"]
    request = observed["recovery"]["reconcile_call"]
    assert request["tab_id"] == "30"
    request["arguments"]["evidence"] = "Fresh exact created-tab read inspected the current document. Preserve the old open result as unknown and continue only from this observed page without replay."
    resumed = await service.control(**request, timeout=2000)
    assert resumed["ok"] and resumed["result"]["original_outcome"] == "unknown"
    assert service.receipts.pending(service.resource(BROWSER, None)) is None
    assert sum(method == "open_tab" for method, _ in runtime.calls) == 1
