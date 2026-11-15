"""A newly created Chromium frame is a read-only readiness failure, not a click retry."""
import pytest

from test_edge_dom_policy import DomRuntime
from test_edge_background_policy import BROWSER, GEN
from web_tools.unified_browser import UnifiedBrowserService


@pytest.mark.parametrize("mode", ["extension_dom", "extension_background"])
@pytest.mark.parametrize("frames,stage_error,expected", [
    (0, "browser_frame_not_ready", "browser_page_not_ready"),
    (1, "browser_frame_not_ready", "browser_page_empty"),
    (0, "browser_frame_permission_denied", "browser_page_empty"),
])
async def test_only_missing_unready_frames_get_read_only_recovery(tmp_path, mode, frames, stage_error, expected):
    runtime = DomRuntime()
    async def empty(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "background_protocol": "xiaoruo1-edge-dom-background/1",
            "error": {"code": "browser_page_empty"},
            "read_diagnostics": {"frame_count": frames, "stages": [{"error": stage_error}]}}
    runtime.handler = empty
    service = UnifiedBrowserService(tmp_path / "receipt.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "read", "7", {"execution_mode": mode}, timeout=2000)
    assert value["error"]["code"] == expected
    assert value["evidence"] == {"executed": False, "verified": False}
    assert value["retryable"] is (expected == "browser_page_not_ready")
    if value["retryable"]:
        assert value["recovery"]["tab_id"] == "7"
        assert value["recovery"]["next_steps"] == ["wait", "read"]
        assert value["recovery"]["repeat_write_allowed"] is False
    assert runtime.foreground_checks == runtime.background_checks == 0
    assert [method for method, _ in runtime.effects] == ["read_page"]


async def test_readiness_metadata_cannot_authorize_replaying_a_write(tmp_path):
    runtime = DomRuntime()
    async def failure(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": True,
            "error": {"code": "browser_page_empty"},
            "read_diagnostics": {"frame_count": 0, "stages": [{"error": "browser_frame_not_ready"}]}}
    runtime.handler = failure
    service = UnifiedBrowserService(tmp_path / "receipt.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "click", "7", {"execution_mode": "extension_dom",
        "expected_generation": GEN, "selector": "#button"}, "readiness-write-0001", True, 2000)
    assert value["status"] == "outcome_unknown" and value["retryable"] is False
    assert value["error"]["code"] == "browser_page_empty"
    assert value["recovery"]["automatic_retry_allowed"] is False
    assert value["recovery"]["repeat_write_allowed"] is False
    assert "retry_read_only" not in value["recovery"]
    assert value["recovery"]["blocking_operation_id"] == "readiness-write-0001"
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "readiness-write-0001"
