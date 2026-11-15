"""A late browser fact can confirm an action; the original action is never repeated."""
from datetime import datetime, timezone

import pytest

from test_edge_recovery_receipts import Runtime, BROWSER, GEN, START, FINISH
from web_tools.unified_browser import UnifiedBrowserService


class LateRuntime(Runtime):
    def __init__(self, *, navigate=True, download=False, unrelated=False):
        super().__init__()
        self.navigate = navigate
        self.download = download
        self.unrelated = unrelated
        self.clicked = 0
        self.started = None

    async def discover(self):
        value = await super().discover()
        value["browsers"][0]["version"] = "1.9.8"
        return value

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        if method == "list_downloads":
            records = [{"id": 1, "url": FINISH, "state": "complete", "filename": "old.txt"}]
            if self.clicked and self.download:
                records.append({"id": 42, "url": "https://unrelated.test/file" if self.unrelated else FINISH,
                    "final_url": "https://unrelated.test/file" if self.unrelated else FINISH,
                    "state": "in_progress", "filename": "D:/Downloads/new.txt", "exists": False,
                    "start_time": self.started, "bytes_received": 1, "total_bytes": 10})
            return {"downloads": records}
        if method == "click":
            self.calls.append((method, dict(payload)))
            self.clicked += 1
            self.started = datetime.now(timezone.utc).isoformat()
            if self.download:
                return {"ok": False, "verified": False, "action_may_have_run": True,
                    "background_protocol": "xiaoruo1-edge-dom-background/1", "operation_no_replay": True,
                    "download_protocol": "xiaoruo1-edge-download/1",
                    "download_source": {"source_tab_id": "7", "source_url": START, "expected_url": FINISH},
                    "error": {"code": "browser_click_result_unknown"}}
            if self.navigate:
                self.tabs[0].update(url=FINISH, status="complete")
            return {"ok": False, "verified": False, "action_may_have_run": True,
                "background_protocol": "xiaoruo1-edge-dom-background/1", "operation_no_replay": True,
                "navigation_protocol": "xiaoruo1-edge-navigation/1",
                "navigation_source": {"source_tab_id": "7", "source_url": START, "expected_url": FINISH,
                    "target": "_self", "frame_id": 0},
                "navigation_target": {"tab_id": "7", "url": "", "created": False,
                    "status": "unknown", "navigation_verified": False},
                "error": {"code": "browser_click_result_unknown"}}
        return await super().dispatch(browser_id, generation, method, payload, timeout)


async def click(tmp_path, runtime):
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover, runtime.dispatch,
        background_check=runtime.session)
    result = await service.control(BROWSER, "click", "7", {"execution_mode": "extension_background",
        "expected_generation": GEN, "selector": "#anchor"}, "late-browser-proof-0001", True, 3000)
    return service, result


async def test_context_lost_navigation_is_verified_from_later_exact_tab_without_reclick(tmp_path):
    runtime = LateRuntime()
    _, result = await click(tmp_path, runtime)
    assert result["ok"] and result["evidence"] == {"executed": True, "verified": True}
    assert result["next_target"]["tab_id"] == "7" and result["result"]["recovered_after_navigation"]
    assert result["result"]["navigation_target"]["url"] == FINISH and runtime.clicked == 1


async def test_original_unchanged_url_cannot_verify_a_failed_navigation(tmp_path):
    runtime = LateRuntime(navigate=False)
    service, result = await click(tmp_path, runtime)
    assert not result["ok"] and result["status"] == "outcome_unknown" and runtime.clicked == 1
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "late-browser-proof-0001"


async def test_late_new_download_record_confirms_only_start_and_never_reclicks(tmp_path):
    runtime = LateRuntime(download=True)
    _, result = await click(tmp_path, runtime)
    assert result["ok"] and result["download_verification"]["download_id"] == 42
    assert result["download_verification"]["complete"] is False and result["result"]["goal_download_complete"] is False
    assert result["result"]["download_started"] is True and runtime.clicked == 1


async def test_simultaneous_unrelated_download_does_not_release_unknown_click(tmp_path):
    runtime = LateRuntime(download=True, unrelated=True)
    service, result = await click(tmp_path, runtime)
    assert result["status"] == "outcome_unknown" and runtime.clicked == 1
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "late-browser-proof-0001"
