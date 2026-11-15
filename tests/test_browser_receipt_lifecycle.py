"""A receipt reservation is not proof of dispatch, and live writers keep ownership."""
from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import time
import weakref

import pytest

from test_edge_background_policy import BROWSER, GEN
from test_edge_dom_policy import DomRuntime
from web_tools.unified_browser import BrowserReceipts, UnifiedBrowserService


ORIGINAL = "receipt-lifecycle-original-20261111"
EVIDENCE = "Fresh exact test document was inspected; the original write must not be replayed or assumed successful."


def make_service(path, runtime):
    return UnifiedBrowserService(path, runtime.discover, runtime.dispatch,
                                 runtime.foreground_check, runtime.background_check)


def write_arguments():
    return {"execution_mode": "extension_dom", "expected_generation": GEN, "selector": "#observed"}


@pytest.fixture
def runtime():
    value = DomRuntime()
    value.browser.update(version="1.9.12", dom_policy="cooperative_v2")
    return value


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_reserved_but_never_dispatched_interruptions_finish_without_a_fence(tmp_path, runtime, interruption):
    """A download-baseline read can consume the budget after reserve, before click."""
    baseline_started = asyncio.Event()

    async def handler(method, payload):
        if method == "list_downloads":
            baseline_started.set()
            await asyncio.sleep(5)
        return {"ok": True, "verified": True}

    runtime.handler = handler
    service = make_service(tmp_path / "receipts.sqlite3", runtime)
    task = asyncio.create_task(service.control(BROWSER, "click", "7", write_arguments(),
                                              ORIGINAL, True, 500 if interruption == "timeout" else 2000))
    await asyncio.wait_for(baseline_started.wait(), 1)
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        response = await task
        assert response["evidence"]["executed"] is False
    row = service.receipts.get(ORIGINAL)
    assert row["state"] == "failed"
    assert json.loads(row["result"])["evidence"]["executed"] is False
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    assert not any(method == "click" for method, _ in runtime.calls)


def test_receipt_connections_close_and_contexts_keep_commit_and_rollback(tmp_path, monkeypatch):
    """Connection.__exit__ commits/rolls back but does not itself close SQLite."""
    connections = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        receipts = BrowserReceipts(tmp_path / "closed.sqlite3")
        with receipts.connect() as db:
            db.execute("CREATE TABLE lifecycle_marker (value TEXT)")
            db.execute("INSERT INTO lifecycle_marker VALUES ('committed')")
        with pytest.raises(RuntimeError, match="rollback"):
            with receipts.connect() as db:
                db.execute("INSERT INTO lifecycle_marker VALUES ('must-rollback')")
                raise RuntimeError("rollback")
        with receipts.connect() as db:
            assert [row[0] for row in db.execute("SELECT value FROM lifecycle_marker")] == ["committed"]
        assert receipts.get("missing-operation") is None
        assert connections and all(connection.closed for connection in connections)
    finally:
        # Keep a failing pre-fix run from leaking handles into other tests.
        for connection in connections:
            if not connection.closed:
                connection.close()


@pytest.mark.parametrize("decision", ["verified", "not_applied"])
async def test_generic_reconciliation_cannot_retire_another_live_service_dispatch(tmp_path, runtime, decision):
    """Per-instance asyncio locks cannot guard another service sharing the DB."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(method, payload):
        if method == "list_downloads":
            return {"downloads": []}
        if method == "click":
            entered.set()
            await release.wait()
        return {"ok": True, "verified": True, "text": "Fixture page", "tab_id": "7",
                "url": "https://fixture.test/form"}

    runtime.handler = handler
    path = tmp_path / "shared.sqlite3"
    first_service, observer = make_service(path, runtime), make_service(path, runtime)
    first = asyncio.create_task(first_service.control(BROWSER, "click", "7", write_arguments(),
                                                     ORIGINAL, True, 5000))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        observed = await observer.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"})
        response = await observer.control(BROWSER, "reconcile", "7", {
            "operation_id": ORIGINAL, "observation_id": observed["observation_id"],
            "decision": decision, "evidence": EVIDENCE}, confirm=True)
        assert not response["ok"]
        assert not first.done()
        assert observer.receipts.get(ORIGINAL)["state"] == "dispatching"
        assert observer.receipts.pending(observer.resource(BROWSER, "7")) == ORIGINAL
    finally:
        release.set()
        await first


@pytest.mark.parametrize("finished_state", ["completed", "failed", "reconciled_observed_page"])
@pytest.mark.parametrize("decision", ["verified", "not_applied"])
async def test_generic_reconciliation_never_rejudges_a_finished_receipt(tmp_path, runtime, finished_state, decision):
    async def handler(method, payload):
        if method == "list_downloads":
            return {"downloads": []}
        if method == "click" and finished_state != "completed":
            return {"ok": False, "verified": False,
                    "action_may_have_run": finished_state != "failed",
                    "error": {"code": "extension_dom_context_changed"}}
        return {"ok": True, "verified": True, "text": "Fixture page", "tab_id": "7",
                "url": "https://fixture.test/form"}

    runtime.handler = handler
    service = make_service(tmp_path / "finished.sqlite3", runtime)
    await service.control(BROWSER, "click", "7", write_arguments(), ORIGINAL, True, 2000)
    observed = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"})
    if finished_state == "reconciled_observed_page":
        resumed = await service.control(BROWSER, "reconcile", "7", {
            "operation_id": ORIGINAL, "observation_id": observed["observation_id"],
            "decision": "continue_from_observed_page", "acknowledge_unknown": True,
            "evidence": EVIDENCE}, confirm=True)
        assert resumed["ok"]
    before = service.receipts.get(ORIGINAL)
    assert before["state"] == finished_state
    response = await service.control(BROWSER, "reconcile", "7", {
        "operation_id": ORIGINAL, "observation_id": observed["observation_id"],
        "decision": decision, "evidence": EVIDENCE}, confirm=True)
    assert not response["ok"]
    assert service.receipts.get(ORIGINAL) == before


@pytest.mark.parametrize("duplicate_id", [False, True])
async def test_cross_service_reservation_race_exposes_owner_or_duplicate_receipt(tmp_path, runtime, duplicate_id):
    """Both callers can pass the optimistic read before BEGIN IMMEDIATE picks an owner."""
    barrier, release = asyncio.Event(), asyncio.Event()
    checks = 0

    async def checked():
        nonlocal checks
        checks += 1
        if checks == 2:
            barrier.set()
        await barrier.wait()
        return {"unlocked": True, "secure_desktop": False, "session_active": True}

    async def handler(method, payload):
        if method == "list_downloads":
            return {"downloads": []}
        if method == "click":
            await release.wait()
        return {"ok": True, "verified": True, "tab_id": "7", "url": "https://fixture.test/form"}

    runtime.handler = handler
    path = tmp_path / "reserve-race.sqlite3"
    services = [UnifiedBrowserService(path, runtime.discover, runtime.dispatch,
                                     runtime.foreground_check, checked) for _ in range(2)]
    keys = [ORIGINAL, ORIGINAL if duplicate_id else "receipt-lifecycle-second-20261111"]
    tasks = [asyncio.create_task(service.control(BROWSER, "click", "7", write_arguments(), key, True, 5000))
             for service, key in zip(services, keys)]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=1)
        assert len(done) == 1 and len(pending) == 1
        response = next(iter(done)).result()
        assert not response["ok"]
        assert response["recovery"]["blocking_operation_id"] in keys
        assert response["recovery"]["live_dispatch"] is True
        if duplicate_id:
            assert response["duplicate_call"] is True and response["redispatched"] is False
        else:
            assert response["error"]["code"] == "browser_target_fenced"
            assert response["evidence"]["executed"] is False
        assert sum(method == "click" for method, _ in runtime.calls) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("lost_proof", ["restart", "expired"])
async def test_lost_observation_returns_a_fresh_read_plan_without_replaying(tmp_path, runtime, lost_proof):
    async def handler(method, payload):
        if method == "list_downloads":
            return {"downloads": []}
        if method == "click":
            return {"ok": False, "verified": False, "action_may_have_run": True,
                    "error": {"code": "extension_dom_context_changed"}}
        return {"ok": True, "verified": True, "text": "Fixture page", "tab_id": "7",
                "url": "https://fixture.test/form"}

    runtime.handler = handler
    path = tmp_path / "lost-observation.sqlite3"
    first_service = make_service(path, runtime)
    await first_service.control(BROWSER, "click", "7", write_arguments(), ORIGINAL, True, 2000)
    observed = await first_service.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"})
    request = observed["recovery"]["reconcile_call"]
    request["arguments"]["evidence"] = EVIDENCE
    if lost_proof == "restart":
        service = make_service(path, runtime)
    else:
        service = first_service
        service._observations[observed["observation_id"]]["at"] = time.monotonic() - 301
    rejected = await service.control(**request)
    assert not rejected["ok"]
    assert rejected["error"]["code"] == "browser_reconciliation_observation_required"
    assert service.receipts.get(ORIGINAL)["state"] == "outcome_unknown"
    assert rejected["recovery"]["blocking_operation_id"] == ORIGINAL
    assert rejected["recovery"]["reconciliation_ready"] is False
    fresh = await service.control(**rejected["recovery"]["read_call"])
    new_request = fresh["recovery"]["reconcile_call"]
    assert new_request["arguments"]["observation_id"] != observed["observation_id"]
    new_request["arguments"]["evidence"] = EVIDENCE
    resumed = await service.control(**new_request)
    assert resumed["result"]["original_outcome"] == "unknown"
    assert resumed["result"]["operation_replayed"] is False
    assert sum(method == "click" for method, _ in runtime.calls) == 1


async def test_inactive_target_ids_cannot_exhaust_browser_coordination(tmp_path, runtime):
    service = make_service(tmp_path / "lock-lifetime.sqlite3", runtime)
    for index in range(2051):
        missing = await service.control(BROWSER, "read", f"missing-tab-{index}",
                                        {"execution_mode": "extension_dom"}, timeout=1000)
        assert missing["error"]["code"] == "browser_tab_not_owned"
    observed = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"})
    assert observed["ok"] and observed["evidence"]["verified"] is True
    gc.collect()
    assert not service._locks


async def test_waiting_same_tab_calls_share_a_lock_until_both_finish(tmp_path, runtime, monkeypatch):
    first_entered, release_first, second_requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read_calls = active_readers = max_active_readers = 0

    async def handler(method, payload):
        nonlocal read_calls, active_readers, max_active_readers
        assert method == "read_page"
        read_calls += 1
        active_readers += 1
        max_active_readers = max(max_active_readers, active_readers)
        try:
            if read_calls == 1:
                first_entered.set()
                await release_first.wait()
            return {"ok": True, "verified": True, "text": "Fixture page", "tab_id": "7",
                    "url": "https://fixture.test/form"}
        finally:
            active_readers -= 1

    runtime.handler = handler
    service = make_service(tmp_path / "shared-lock.sqlite3", runtime)
    original_target_lock = service.target_lock
    returned_lock_ids = []

    def observed_target_lock(browser_id, tab_id):
        lock = original_target_lock(browser_id, tab_id)
        returned_lock_ids.append(id(lock))
        if len(returned_lock_ids) == 2:
            second_requested.set()
        return lock

    monkeypatch.setattr(service, "target_lock", observed_target_lock)
    arguments = {"execution_mode": "extension_dom"}
    first = asyncio.create_task(service.control(BROWSER, "read", "7", arguments))
    await asyncio.wait_for(first_entered.wait(), 1)
    lock_reference = weakref.ref(service._locks[(BROWSER, "7")])
    second = asyncio.create_task(service.control(BROWSER, "read", "7", arguments))
    try:
        await asyncio.wait_for(second_requested.wait(), 1)
        assert returned_lock_ids[0] == returned_lock_ids[1]
        assert read_calls == 1 and not second.done()
        assert lock_reference() is not None
    finally:
        release_first.set()
        results = await asyncio.gather(first, second)
    assert all(result["ok"] for result in results)
    assert read_calls == 2 and max_active_readers == 1
    gc.collect()
    assert lock_reference() is None and not service._locks

