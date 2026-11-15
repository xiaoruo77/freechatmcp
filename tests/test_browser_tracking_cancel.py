"""Ending stale local tracking never converts an unknown write into success."""
import pytest

from test_edge_recovery_receipts import Runtime, BROWSER, GEN, START
from web_tools.unified_browser import UnifiedBrowserService


@pytest.fixture
async def pending(tmp_path):
    runtime = Runtime()
    async def handler(method, payload):
        if method == "click":
            return {"ok": False, "verified": False, "action_may_have_run": True,
                "error": {"code": "extension_background_context_changed"},
                "background_protocol": "xiaoruo1-edge-dom-background/1"}
        return {"ok": True, "verified": True, "tab_id": "7", "url": START,
            "text": "", "dom_read": False, "extraction": "browser_tab_metadata",
            "background_protocol": "xiaoruo1-edge-dom-background/1"}
    runtime.handler = handler
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, background_check=runtime.session)
    args = {"execution_mode": "extension_background", "expected_generation": GEN, "selector": "#entry"}
    first = await service.control(BROWSER, "click", "7", args, "cancel-stale-tracking-0001", True, 2000)
    assert first["status"] == "outcome_unknown"
    read = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_background", "mode": "tab_metadata"}, timeout=2000)
    return runtime, service, args, read


async def cancel(service, read, **overrides):
    arguments = {"operation_id": "cancel-stale-tracking-0001", "observation_id": read["observation_id"],
        "decision": "cancel_tracking", "acknowledge_unknown": True,
        "evidence": "Only the stale local tracking is cancelled after exact-tab metadata inspection. The original result remains unknown and the click is never repeated.", **overrides}
    return await service.control(BROWSER, "reconcile", "7", arguments, confirm=True, timeout=2000)


async def test_fresh_metadata_can_cancel_only_tracking_without_success_or_replay(pending):
    runtime, service, args, read = pending
    result = await cancel(service, read)
    assert result["status"] == "reconciled_cancelled_tracking" and result["ok"]
    assert result["result"]["original_outcome"] == "unknown" and result["result"]["goal_success_claimed"] is False
    assert result["evidence"] == {"executed": None, "verified": False}
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    saved = service.receipts.get("cancel-stale-tracking-0001")
    import json
    assert json.loads(saved["result"])["original_receipt"]["status"] == "outcome_unknown"
    duplicate = await service.control(BROWSER, "click", "7", args, "cancel-stale-tracking-0001", True, 2000)
    assert not duplicate["ok"] and duplicate["status"] == "outcome_unknown" and duplicate["redispatched"] is False
    assert sum(method == "click" for method, _ in runtime.calls) == 1


@pytest.mark.parametrize("change", [{"acknowledge_unknown": False}, {"evidence": "short"}, {"observation_id": "unknown-observation"}])
async def test_acknowledgement_and_exact_observation_are_mandatory(pending, change):
    _, service, _, read = pending
    result = await cancel(service, read, **change)
    assert not result["ok"] and service.receipts.pending(service.resource(BROWSER, "7")) is not None


async def test_current_live_dispatch_cannot_be_cancelled(pending):
    _, service, _, read = pending
    with service.receipts.connect() as db:
        db.execute("UPDATE unified_browser_operations SET state='dispatching' WHERE operation_id=?", ("cancel-stale-tracking-0001",))
    result = await cancel(service, read)
    assert not result["ok"] and result["error"]["code"] == "browser_cancel_tracking_acknowledgement_required"
