"""Form validation and lost execution results require different recovery."""
import pytest

from test_edge_dom_policy import DomRuntime
from test_edge_background_policy import BROWSER, GEN
from web_tools.unified_browser import UnifiedBrowserService


@pytest.mark.parametrize("mode", ["extension_dom", "extension_background"])
@pytest.mark.parametrize("failure", ["extension_background_target_hidden", "extension_background_target_readonly", "extension_background_target_disabled"])
async def test_proven_prewrite_validation_does_not_create_an_unknown_fence(tmp_path, mode, failure):
    runtime = DomRuntime()
    async def refused(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": False,
            "background_protocol": "xiaoruo1-edge-dom-background/1", "error": {"code": failure, "phase": "target_validation"}}
    runtime.handler = refused
    service = UnifiedBrowserService(tmp_path / "receipt.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    value = await service.control(BROWSER, "type", "7", {"execution_mode": mode,
        "expected_generation": GEN, "selector": "#form-field", "text": "allowed fixture text"},
        "form-prewrite-failure-0001", True, 2000)
    assert value["status"] == "failed" and value["evidence"] == {"executed": False, "verified": False}
    assert value["error"]["code"] == failure
    assert "refused" not in value["error"]["message"].lower()
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    assert value["recovery"]["inspect_visible_control"] is True
    assert value["recovery"]["write_dispatched"] is False
    assert sum(method == "type_text" for method, _ in runtime.calls) == 1


@pytest.mark.parametrize("failure", ["extension_background_response_unavailable", "extension_dom_script_execution_error", "extension_dom_script_result_missing"])
async def test_missing_renderer_result_keeps_unknown_without_false_permission_explanation(tmp_path, failure):
    runtime = DomRuntime()
    async def missing(method, payload):
        return {"ok": False, "verified": False, "action_may_have_run": True,
            "error": {"code": failure, "phase": "script_result"}}
    runtime.handler = missing
    service = UnifiedBrowserService(tmp_path / "receipt.sqlite3", runtime.discover,
        runtime.dispatch, runtime.foreground_check, runtime.background_check)
    args = {"execution_mode": "extension_dom", "expected_generation": GEN, "selector": "#field", "text": "fixture"}
    value = await service.control(BROWSER, "type", "7", args, "form-lost-result-0001", True, 2000)
    assert value["status"] == "outcome_unknown" and value["retryable"] is False
    assert value["evidence"] == {"executed": None, "verified": False}
    assert "refused" not in value["error"]["message"].lower()
    assert "permission denial" in value["error"]["message"].lower()
    assert value["recovery"]["blocking_operation_id"] == "form-lost-result-0001"
    assert value["recovery"]["repeat_write_allowed"] is False
    second = await service.control(BROWSER, "type", "7", args, "form-lost-result-0001", True, 2000)
    assert second["duplicate_call"] and second["redispatched"] is False
    assert sum(method == "type_text" for method, _ in runtime.calls) == 1
