"""Content read from a replaced document never becomes a click observation."""
import pytest

from test_edge_dom_policy import DomRuntime
from test_edge_background_policy import BROWSER
from web_tools.unified_browser import UnifiedBrowserService


@pytest.mark.parametrize("changed", ["top_url", "frame_url"])
async def test_navigation_during_read_requests_only_another_read(tmp_path, changed):
    runtime = DomRuntime()
    original = runtime.tabs[0]["url"]
    async def read(method, payload):
        if changed == "top_url":
            runtime.tabs[0]["url"] = "https://fixture.test/new-page"
        return {"ok": True, "verified": True, "tab_id": "7", "url": original, "text": "Old page text",
            "extraction": "dom-text-fast", "frames": [{"frame_id": 0,
                "url": "https://fixture.test/different-document" if changed == "frame_url" else original}]}
    runtime.handler = read
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover, runtime.dispatch,
        runtime.foreground_check, runtime.background_check)
    result = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"}, timeout=2000)
    assert result["error"]["code"] == "browser_page_changed_during_read" and result["retryable"]
    assert result["recovery"]["retry_read_only"] and result["recovery"]["repeat_write_allowed"] is False
    assert "observation_id" not in result and not service._observations
    assert all(method in {"list_tabs", "read_page"} for method, _ in runtime.calls)


async def test_script_timeout_has_browser_connected_read_only_recovery(tmp_path):
    runtime = DomRuntime()
    async def timeout(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "error": {"code": "browser_page_empty"},
            "read_diagnostics": {"frame_count": 0, "stages": [{"error": "browser_frame_read_timeout"}]}}
    runtime.handler = timeout
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover, runtime.dispatch,
        runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_dom"}, timeout=2000)
    assert value["error"]["code"] == "browser_page_execution_timeout" and value["retryable"]
    assert value["recovery"]["original_tab_preserved"] is True
    assert value["recovery"]["fallback_action"] == "open_tab"
    assert value["recovery"]["fallback_scope"] == "fresh_url_document_not_original_live_dom"
    assert value["recovery"]["repeat_write_allowed"] is False
    assert not service._observations
