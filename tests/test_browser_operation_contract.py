"""Browser checkpoints remain actionable through MCP normalization and read retries."""
from __future__ import annotations

from copy import deepcopy
import logging
from types import SimpleNamespace

import httpx
import pytest

from test_edge_background_routes import BROWSER, GEN, current_bridge
from web_mcp.result_contract import normalize_result


@pytest.mark.parametrize("status", ["outcome_unknown", "dispatched_unverified"])
def test_browser_uncertain_status_and_exact_continuation_survive_normalization_twice(status):
    raw = {"ok": False, "status": status, "browser_id": BROWSER, "tab_id": "7",
        "action": "click", "operation_id": "contract-original-click", "retryable": False,
        "error": {"code": "browser_verification_required", "message": "Inspect the exact page before another write."},
        "evidence": {"executed": None, "verified": False},
        "next_target": {"browser_id": BROWSER, "tab_id": "30", "expected_generation": GEN, "read_required": True},
        "recovery": {"blocking_operation_id": "contract-original-click", "next_steps": ["status", "read", "reconcile"]}}
    first = normalize_result("browser_control", raw)
    second = normalize_result("browser_control", first)
    for value in (first, second):
        assert value["status"] == status and value["ok"] is False and value["completed"] is False
        assert value["error"]["message"] == raw["error"]["message"]
        assert not value["error"]["message"].startswith("{")
        assert value["continuation_required"] is True and value["repeat_write_allowed"] is False
        assert value["suggested_next_tool"] == "browser_control"
        assert value["next_target"] == raw["next_target"] and value["recovery"] == raw["recovery"]
        assert value["evidence"] == raw["evidence"]
    assert first == second


def test_browser_fence_preserves_specific_blocking_operation_and_plain_error_message():
    raw = {"ok": False, "status": "failed", "error": {"code": "browser_target_fenced", "message": "Read the original receipt before any new action."},
        "recovery": {"blocking_operation_id": "blocked-original", "tab_id": "7", "expected_generation": GEN}}
    value = normalize_result("browser_control", raw)
    assert value["error"] == raw["error"]
    assert value["continuation_required"] is True and value["repeat_write_allowed"] is False
    assert value["recovery"] == raw["recovery"]


def test_generic_failure_is_not_reclassified_as_a_browser_checkpoint():
    value = normalize_result("read_file", {"ok": False, "status": "outcome_unknown", "error": {"code": "failure", "message": "File failure"}})
    assert value["status"] == "failed" and "continuation_required" not in value


def not_ready(*, code="browser_page_not_ready", recovery=True):
    return {"ok": False, "status": "failed", "error": {"code": code},
        **({"recovery": {"retry_read_only": True}} if recovery else {})}


async def run_retries(module, monkeypatch, outputs, *, action="read", timeout=3000):
    calls = []
    async def control(**payload):
        calls.append(deepcopy(payload))
        return deepcopy(outputs[min(len(calls)-1, len(outputs)-1)])
    async def no_sleep(_):
        pass
    monkeypatch.setattr(module.unified, "control", control)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    payload = {"browser_id": BROWSER, "tab_id": "7", "action": action,
        "arguments": {"execution_mode": "extension_dom", "selector": "#observed"},
        "operation_id": "retry-original-operation" if action == "click" else None,
        "confirm": action == "click", "timeout": timeout}
    return await module._browser_control_with_read_retries(payload), calls, payload


async def test_readiness_retry_uses_same_exact_target_and_stops_on_success(current_bridge, monkeypatch):
    value, calls, original = await run_retries(current_bridge, monkeypatch,
        [not_ready(), not_ready(), {"ok": True, "status": "completed", "result": {"text": "Observed page"}}])
    assert value["ok"] and len(calls) == 3
    assert len(value["read_attempts"]) == 3
    assert all({k: v for k, v in call.items() if k != "timeout"} == {k: v for k, v in original.items() if k != "timeout"} for call in calls)
    assert all(500 <= call["timeout"] <= original["timeout"] for call in calls)


async def test_readiness_retry_is_bounded_to_three_read_attempts(current_bridge, monkeypatch):
    value, calls, _ = await run_retries(current_bridge, monkeypatch, [not_ready()])
    assert not value["ok"] and len(calls) == 3 and len(value["read_attempts"]) == 3
    assert all(call["action"] == "read" for call in calls)


@pytest.mark.parametrize("output", [not_ready(recovery=False), not_ready(code="browser_frame_permission_denied"),
    not_ready(code="browser_page_empty"), {"ok": False, "error": {"code": "browser_timeout"}}, {"ok": True}])
async def test_only_proven_read_readiness_is_retried(current_bridge, monkeypatch, output):
    _, calls, _ = await run_retries(current_bridge, monkeypatch, [output])
    assert len(calls) == 1


@pytest.mark.parametrize("action", ["click", "navigate", "type", "open_tab", "close_tab"])
async def test_write_operations_are_never_replayed_by_readiness_helper(current_bridge, monkeypatch, action):
    value, calls, _ = await run_retries(current_bridge, monkeypatch, [not_ready()], action=action)
    assert len(calls) == 1 and calls[0]["action"] == action and "read_attempts" not in value


async def test_invalid_timeout_is_validated_once_without_retry(current_bridge, monkeypatch):
    _, calls, _ = await run_retries(current_bridge, monkeypatch, [not_ready()], timeout="invalid")
    assert len(calls) == 1 and calls[0]["timeout"] == "invalid"


@pytest.mark.parametrize("exception,expected", [(httpx.ReadTimeout, "browser_response_timeout"),
    (httpx.ConnectError, "browser_bridge_error")])
async def test_real_http_client_distinguishes_a_slow_response_from_a_connection_error(tmp_path, monkeypatch, exception, expected):
    from web_tools import browser_client
    token = tmp_path / "test-bridge.token"
    token.write_text("test-only-bridge-token", encoding="utf-8")
    async def handler(request):
        raise exception("Simulated network condition", request=request)
    original_client = httpx.AsyncClient
    def client_factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(browser_client.httpx, "AsyncClient", client_factory)
    client = browser_client.BrowserBridgeClient(SimpleNamespace(
        browser_bridge_url="http://test.invalid", browser_bridge_token_file=token))
    with pytest.raises(browser_client.BrowserBridgeError) as caught:
        await client.browser_control(BROWSER, "click", "7", operation_id="timeout-original-click", confirm=True)
    assert caught.value.code == expected
    if expected == "browser_response_timeout":
        assert "未启动" not in str(caught.value)
        assert "写操作不要直接重试" in str(caught.value)


async def test_structured_browser_refusal_survives_the_http_client(tmp_path, monkeypatch):
    from web_tools import browser_client
    token = tmp_path / "test-bridge.token"
    token.write_text("test-only-bridge-token", encoding="utf-8")
    refusal = {"ok": False, "status": "failed", "error": {"code": "extension_dom_capture_requires_active_tab"},
        "evidence": {"executed": False, "verified": False}, "retryable": False}
    original_client = httpx.AsyncClient
    def client_factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=refusal)))
    monkeypatch.setattr(browser_client.httpx, "AsyncClient", client_factory)
    client = browser_client.BrowserBridgeClient(SimpleNamespace(
        browser_bridge_url="http://test.invalid", browser_bridge_token_file=token))
    assert await client.browser_control(BROWSER, "screenshot", "7") == refusal


@pytest.mark.parametrize("method", ["screenshot", "debug_screenshot"])
async def test_capture_refusal_is_not_reinterpreted_as_invalid_png(current_bridge, monkeypatch, method):
    bridge = current_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"instance_id": BROWSER.removeprefix("browser_"), "websocket": object()}
    refusal = {"ok": False, "verified": False, "dom_protocol": "xiaoruo1-edge-dom/1",
        "error": {"code": "extension_dom_capture_requires_active_tab"}, "action_may_have_run": False}
    async def call(*args, **kwargs):
        return deepcopy(refusal)
    monkeypatch.setattr(bridge, "call", call)
    value = await bridge.browser_dispatch(BROWSER, GEN, method,
        {"tab_id": "7", "browser_dom_only": True}, 2)
    assert value == refusal
    assert list(current_bridge.SCREENSHOT_ROOT.glob("*.png")) == []


@pytest.mark.parametrize("action,is_write", [("click", True), ("navigate", True), ("open_tab", True),
    ("read", False), ("list_tabs", False), ("screenshot", False)])
async def test_direct_registry_preserves_unknown_write_vs_retryable_read_timeout(action, is_write):
    from web_mcp.tool_registry import ToolRegistry, ToolSpec
    from web_tools.browser_client import BrowserBridgeError
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(settings=SimpleNamespace(orchestration_mode="chatgpt_direct"),
        logger=logging.getLogger("browser-timeout-contract"))
    registry.specs = {"browser_control": ToolSpec("browser_control", "test")}
    registry._direct_tool_names = set()
    attempts = []
    async def failed(**arguments):
        attempts.append(arguments)
        raise BrowserBridgeError("The browser response timed out; inspect its original receipt.",
            code="browser_response_timeout", retryable=True)
    registry._browser_control = failed
    result = await registry.invoke("browser_control", {"browser_id": BROWSER, "tab_id": "7", "action": action,
        "operation_id": "timed-original-0001", "confirm": is_write})
    assert result["status"] == ("outcome_unknown" if is_write else "failed")
    assert result["retryable"] is (not is_write)
    assert result["evidence"] == {"executed": None if is_write else False, "verified": False}
    assert result["error_code"] == "browser_response_timeout" and len(attempts) == 1
    if is_write:
        assert result["recovery"]["operation_id"] == "timed-original-0001"
        assert result["recovery"]["tab_id"] == "7" and result["recovery"]["repeat_write_allowed"] is False
        assert result["recovery"]["next_steps"] == ["status", "read"]
    else:
        assert "recovery" not in result


@pytest.mark.parametrize("code", ["browser_bridge_error", "browser_bridge_http_error", "browser_bridge_invalid_response",
    "browser_bridge_credentials_unavailable", "browser_bridge_authentication_failed", "browser_bridge_request_rejected"])
@pytest.mark.parametrize("action", ["click", "read"])
async def test_direct_registry_never_calls_an_ambiguous_http_write_known_not_executed(code, action):
    from web_mcp.tool_registry import ToolRegistry, ToolSpec
    from web_tools.browser_client import BrowserBridgeError
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(settings=SimpleNamespace(orchestration_mode="chatgpt_direct"),
        logger=logging.getLogger("browser-http-contract"))
    registry.specs = {"browser_control": ToolSpec("browser_control", "test")}
    registry._direct_tool_names = set()
    async def failed(**arguments):
        raise BrowserBridgeError("Local bridge result unavailable", code=code, retryable=True)
    registry._browser_control = failed
    response = await registry.invoke("browser_control", {"browser_id": BROWSER, "tab_id": "7", "action": action,
        "operation_id": "http-original-0001", "confirm": action == "click"})
    known = code in {"browser_bridge_credentials_unavailable", "browser_bridge_authentication_failed", "browser_bridge_request_rejected"}
    uncertain = action == "click" and not known
    assert response["status"] == ("outcome_unknown" if uncertain else "failed")
    assert response["evidence"]["executed"] is (None if uncertain else False)
    if uncertain:
        assert response["retryable"] is False
        assert response["recovery"]["operation_id"] == "http-original-0001"
        assert response["recovery"]["repeat_write_allowed"] is False
