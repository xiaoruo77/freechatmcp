"""Service-boundary tests for the explicit native CEF background policy."""
from __future__ import annotations

import asyncio
import json

import pytest

from web_tools.unified_browser import BrowserControlError, UnifiedBrowserService, WIRE

BROWSER = "native_background_fixture"
GEN = "native-background-generation"


class NativeRuntime:
    def __init__(self):
        self.browser = {"browser_id": BROWSER, "connection_generation": GEN,
                        "adapter": "native_ipc", "stable_identity": True,
                        "transport": "windows_named_pipe", "browser_kind": "xiaoruo_browser",
                        "native_pid": 42, "connected": True, "supported_actions": list(WIRE)}
        self.tabs = [{"tab_id": "7", "active": False, "engine": "chromium", "url": "https://fixture.test/form"},
                     {"tab_id": "8", "active": True, "engine": "chromium", "url": "https://fixture.test/user"},
                     {"tab_id": "9", "active": False, "engine": "chromium", "url": "https://fixture.test/other"}]
        self.active = "8"
        self.calls = []
        self.foreground_checks = 0
        self.background_checks = 0
        self.before_background = None
        self.handler = None
        self.list_hook = None

    def select(self, tab_id):
        self.active = tab_id
        for item in self.tabs:
            item["active"] = item["tab_id"] == tab_id

    async def discover(self):
        return {"browsers": [dict(self.browser)]}

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        assert browser_id == BROWSER and generation == GEN and timeout > 0
        self.calls.append((method, dict(payload)))
        if method == "list_tabs":
            if self.list_hook:
                self.list_hook()
            return {"tabs": [dict(item) for item in self.tabs], "active_tab_id": self.active,
                    "verified": True, "adapter": "native_ipc"}
        if self.handler:
            return await self.handler(method, payload)
        return {"ok": True, "verified": True, "tab_id": payload["tab_id"], "url": self.tabs[0]["url"]}

    async def foreground_check(self):
        self.foreground_checks += 1
        # Recent physical input still rejects every existing foreground path.
        raise BrowserControlError("desktop_user_active", "Recent user keyboard or mouse input")

    async def background_check(self):
        self.background_checks += 1
        if self.before_background:
            self.before_background(self.background_checks)
        return {"unlocked": True, "secure_desktop": False, "session_active": True,
                "source": "interactive_desktop_state", "token": "must-not-be-receipted"}

    @property
    def effects(self):
        return [(method, payload) for method, payload in self.calls if method != "list_tabs"]


@pytest.fixture
def setup(tmp_path):
    runtime = NativeRuntime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
                                    runtime.dispatch, runtime.foreground_check, runtime.background_check)
    return runtime, service


async def background(service, action="click", operation_id="background-click-0001", **arguments):
    return await service.control(BROWSER, action, "7", {"expected_generation": GEN,
        "execution_mode": "native_background", **arguments}, operation_id, True, 2000)


async def test_user_activity_allows_background_and_still_blocks_foreground(setup):
    runtime, service = setup
    allowed = await background(service, selector="#button")
    assert allowed["ok"] and allowed["evidence"] == {"executed": True, "verified": True}
    assert runtime.foreground_checks == 0 and runtime.background_checks == 2
    assert runtime.active == "8"
    payload = runtime.effects[0][1]
    assert payload["background_only"] is True and payload["expected_active_tab_id"] == "8"
    assert "execution_mode" not in payload and "expected_generation" not in payload
    evidence = allowed["background_execution"]
    assert [item["phase"] for item in evidence["snapshots"]] == ["initial", "pre_dispatch", "post_dispatch", "post_desktop_check"]
    assert all(item["active_tab_id"] == "8" and item["target_active"] is False for item in evidence["snapshots"])
    assert "must-not-be-receipted" not in json.dumps(service.receipts.get("background-click-0001"))
    denied = await service.control(BROWSER, "click", "7", {"expected_generation": GEN, "selector": "#button"}, "foreground-click-0001", True)
    assert denied["error"]["code"] == "desktop_user_active"
    assert runtime.foreground_checks == 1 and len(runtime.effects) == 1


@pytest.mark.parametrize('variant,decision,allowed', [
    ('guarded', 'verified', True), ('guarded', 'not_applied', False),
    ('cross_origin', 'verified', False), ('no_transition', 'verified', False),
    ('failed_task', 'verified', False), ('missing_receipt', 'verified', False),
])
async def test_reconcile_guarded_same_tab_route_without_replaying_click(setup, variant, decision, allowed):
    runtime, service = setup
    destination = 'https://other.test/drafts' if variant == 'cross_origin' else 'https://fixture.test/drafts'

    async def handler(method, payload):
        if method == 'read_page':
            return {'verified': True, 'url': destination}
        assert method == 'click'
        runtime.tabs[0]['url'] = destination
        return {'verified': False, 'helper': 'xiaoruo1-cef-background-interaction/1', 'action': 'click',
            'adapter': 'native_ipc', 'execution_mode': 'native_background', 'action_may_have_run': True,
            'url_transition': variant != 'no_transition', 'native_terminal': True, 'native_state': 'succeeded',
            'native_task_id': 'xr-42-99', 'native_task': {} if variant == 'missing_receipt' else {
                'taskId': 'xr-42-99', 'state': 'failed' if variant == 'failed_task' else 'succeeded'}}

    runtime.handler = handler
    first = await background(service, operation_id='route-original-click', selector='#save')
    assert first['status'] == 'dispatched_unverified'
    observed = await background(service, action='read')
    reconciled = await service.control(BROWSER, 'reconcile', '7', {
        'operation_id': 'route-original-click', 'observation_id': observed['observation_id'],
        'decision': decision, 'evidence': 'Read the saved draft in the resulting page, original native task succeeded.'}, confirm=True)
    assert reconciled['ok'] is allowed
    if allowed:
        assert reconciled['status'] == 'reconciled_verified'
    else:
        assert reconciled['error']['code'] == 'browser_reconciliation_page_changed'
    assert len([call for call in runtime.effects if call[0] == 'click']) == 1


@pytest.mark.parametrize("action,arguments", [
    ("read", {}), ("screenshot", {}), ("screenshot", {"mode": "page"}),
])
async def test_safe_page_observation_needs_identity_but_no_desktop_input_check(setup, action, arguments):
    runtime, service = setup
    service.background_check = None
    response = await background(service, action, **arguments)
    assert response["ok"] and runtime.background_checks == runtime.foreground_checks == 0
    assert len(response["background_execution"]["snapshots"]) == 3
    assert runtime.effects[0][1]["background_only"] is True


@pytest.mark.parametrize("change", [
    {"adapter": "extension", "browser_kind": "edge"}, {"stable_identity": False},
    {"transport": "websocket"}, {"native_pid": 0}, {"browser_kind": "edge"},
])
async def test_background_refuses_unverified_or_edge_identity(setup, change):
    runtime, service = setup
    runtime.browser.update(change)
    response = await background(service, "read")
    assert response["error"]["code"] == "native_background_identity_required"
    assert not runtime.calls and not runtime.effects


@pytest.mark.parametrize("engine", ["lightpanda", "unknown", None])
async def test_background_never_switches_or_infers_engine(setup, engine):
    runtime, service = setup
    runtime.tabs[0]["engine"] = engine
    response = await background(service)
    assert response["error"]["code"] == "native_background_cef_required"
    assert not runtime.effects and runtime.background_checks == 0


async def test_active_target_refused_with_observed_evidence(setup):
    runtime, service = setup
    runtime.select("7")
    response = await background(service)
    assert response["error"]["code"] == "foreground_required"
    assert response["evidence"]["executed"] is False and not runtime.effects
    assert response["background_execution"]["snapshots"][0]["target_active"] is True


@pytest.mark.parametrize("action,arguments", [
    ("open_tab", {"url": "https://fixture.test/new"}), ("activate_tab", {}), ("close_tab", {}),
    ("navigate", {"url": "https://fixture.test/new"}), ("debug_attach", {}),
    ("debug_status", {}), ("debug_snapshot", {}), ("debug_screenshot", {}),
    ("debug_detach", {}), ("screenshot", {"mode": "window"}),
])
async def test_foreground_actions_are_never_dispatched_in_background(setup, action, arguments):
    runtime, service = setup
    response = await background(service, action, **arguments)
    assert response["error"]["code"] == "foreground_required"
    assert not runtime.calls and runtime.foreground_checks == runtime.background_checks == 0


@pytest.mark.parametrize("new_active", ["7", "9"])
async def test_user_tab_switch_during_preflight_refuses_without_restoring_focus(setup, new_active):
    runtime, service = setup
    runtime.before_background = lambda count: runtime.select(new_active)
    response = await background(service)
    assert not response["ok"] and response["evidence"]["executed"] is False
    assert response["error"]["code"] in {"foreground_required", "native_background_active_changed"}
    assert runtime.active == new_active and not runtime.effects
    assert service.receipts.get("background-click-0001") is None


@pytest.mark.parametrize("new_active", ["7", "9"])
async def test_user_tab_switch_after_dispatch_creates_durable_nonretry_fence(setup, tmp_path, new_active):
    runtime, service = setup
    async def handler(method, payload):
        runtime.select(new_active)
        return {"ok": True, "verified": True}
    runtime.handler = handler
    response = await background(service)
    assert response["status"] == "outcome_unknown" and response["retryable"] is False
    assert response["evidence"] == {"executed": None, "verified": False}
    assert runtime.active == new_active and len(runtime.effects) == 1
    assert response["background_execution"]["snapshots"][-1]["active_tab_id"] == new_active
    # A bridge restart must retain both duplicate-call and resource fences.
    restarted = UnifiedBrowserService(service.receipts.path, runtime.discover, runtime.dispatch,
                                      runtime.foreground_check, runtime.background_check)
    duplicate = await background(restarted)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    runtime.select("8")
    fenced = await background(restarted, operation_id="background-click-0002")
    assert fenced["error"]["code"] == "browser_target_fenced" and len(runtime.effects) == 1


@pytest.mark.parametrize("state", ["accepted", "succeeded", "failed"])
@pytest.mark.parametrize("probe_failure", ["active_changed", "error", "timeout"])
async def test_post_dispatch_probe_failure_retains_bounded_native_diagnostics(setup, state, probe_failure):
    runtime, service = setup
    task_id = "xr-42-17"
    native_task = {"taskId": task_id, "state": state, "action": "upload_file", "tabId": 7,
                   "result": {"html": "raw-dom-must-not-leak"}, "authToken": "auth-must-not-leak"}
    if state == "failed":
        native_task["errorCode"] = "operation_failed"
    stages = {"file_selected": True if state == "succeeded" else None,
              "upload_started": None, "server_received": None, "platform_processed": None}

    async def handler(method, payload):
        def fail_probe():
            if probe_failure == "active_changed":
                runtime.select("9")
            elif probe_failure == "timeout":
                raise TimeoutError()
            else:
                raise RuntimeError("private-error-must-not-leak")
        runtime.list_hook = fail_probe
        return {"ok": state == "succeeded", "verified": state == "succeeded", "adapter": "native_ipc",
                "native_task_id": task_id, "native_state": state, "native_task": native_task,
                "action_may_have_run": True, "stages": stages,
                "error": {"code": "operation_failed", "message": "native-error-must-not-leak"} if state == "failed" else None,
                "text": "raw-text-must-not-leak", "dom": {"selector": "raw-dom-must-not-leak"},
                "file_data_base64": "media-must-not-leak", "authToken": "auth-must-not-leak"}

    runtime.handler = handler
    fields = {"file_path": "D:/fixture/video.mp4", "selector": "#video"}
    response = await background(service, "upload_file", **fields)
    assert response["status"] == "outcome_unknown" and response["retryable"] is False
    assert response["evidence"] == {"executed": None, "verified": False}
    assert response["native_task_id"] == task_id
    summary = response["result"]
    assert summary["native_task_id"] == task_id and summary["native_state"] == state
    assert summary["native_terminal"] is (state != "accepted")
    expected_task = {key: native_task[key] for key in ("taskId", "state", "action", "tabId", "errorCode") if key in native_task}
    assert summary["native_task"] == expected_task and summary["stages"] == stages
    if state == "failed":
        assert summary["error"] == {"code": "operation_failed"}
    assert all(marker not in json.dumps(response) for marker in
               ("raw-dom-must-not-leak", "raw-text-must-not-leak", "media-must-not-leak",
                "auth-must-not-leak", "native-error-must-not-leak", "private-error-must-not-leak"))
    saved = json.loads(service.receipts.get("background-click-0001")["result"])
    assert saved["native_task_id"] == task_id and saved["result"] == summary
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "background-click-0001"
    runtime.list_hook = None
    runtime.select("8")
    duplicate = await background(service, "upload_file", **fields)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    fenced = await background(service, "upload_file", operation_id="background-upload-0002", **fields)
    assert fenced["error"]["code"] == "browser_target_fenced" and len(runtime.effects) == 1


async def test_post_dispatch_probe_diagnostics_drop_oversized_native_identifiers(setup):
    runtime, service = setup

    async def handler(method, payload):
        runtime.select("9")
        return {"ok": False, "native_task_id": "x" * 10000, "native_state": "failed",
                "native_task": {"taskId": "x" * 10000, "state": "failed", "action": "upload_file",
                                "errorCode": "y" * 10000, "result": "private-must-not-leak"},
                "error": {"code": "y" * 10000, "message": "private-must-not-leak"}}

    runtime.handler = handler
    response = await background(service)
    assert response["status"] == "outcome_unknown"
    assert "native_task_id" not in response and "native_task_id" not in response["result"]
    assert response["result"]["native_task"] == {"state": "failed", "action": "upload_file"}
    assert "error" not in response["result"] and len(json.dumps(response["result"])) < 300


async def test_predispatch_native_refusal_still_proves_no_effect(setup):
    runtime, service = setup

    async def handler(method, payload):
        return {"ok": False, "action_may_have_run": False,
                "error": {"code": "upload_selector_unobserved", "message": "Read this input first"}}

    runtime.handler = handler
    response = await background(service, "upload_file", file_path="D:/fixture/video.mp4", selector="#video")
    assert response["status"] == "failed" and response["evidence"]["executed"] is False
    assert response["result"]["action_may_have_run"] is False
    assert response["error"]["code"] == "upload_selector_unobserved"
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None


async def test_desktop_lock_or_secure_desktop_cannot_bypass_background_check(setup):
    runtime, service = setup
    async def locked():
        raise BrowserControlError("foreground_required", "Secure desktop or a locked session requires explicit foreground interaction")
    service.background_check = locked
    response = await background(service)
    assert response["error"]["code"] == "foreground_required" and not runtime.effects
    service.background_check = None
    unavailable = await background(service)
    assert unavailable["error"]["code"] == "native_background_check_unavailable" and not runtime.effects


async def test_lock_after_dispatch_is_uncertain_and_fenced(setup):
    runtime, service = setup
    async def check():
        runtime.background_checks += 1
        if runtime.background_checks > 1:
            raise BrowserControlError("foreground_required", "Session became locked")
    service.background_check = check
    response = await background(service)
    assert response["status"] == "outcome_unknown" and not response["retryable"]
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "background-click-0001"


async def test_background_preserves_confirmation_generation_and_operation_identity(setup):
    runtime, service = setup
    response = await service.control(BROWSER, "click", "7", {"execution_mode": "native_background"}, "background-confirm-0001", False)
    assert response["error"]["code"] == "browser_confirmation_required"
    missing = await service.control(BROWSER, "click", "7", {"execution_mode": "native_background"}, "background-confirm-0001", True)
    assert missing["error"]["code"] == "browser_generation_required"
    stale = await background(service, expected_generation="old-generation")
    assert stale["error"]["code"] == "browser_generation_changed"
    assert not runtime.effects
    success = await background(service)
    duplicate = await background(service)
    conflict = await background(service, selector="#different")
    assert success["ok"] and duplicate["duplicate_call"] and len(runtime.effects) == 1
    assert conflict["error"]["code"] == "browser_operation_conflict"


@pytest.mark.parametrize("mode", ["auto", "", None, True, {}, []])
async def test_invalid_execution_mode_fails_before_discovery_or_dispatch(setup, mode):
    runtime, service = setup
    response = await background(service, execution_mode=mode)
    assert response["error"]["code"] == "browser_execution_mode_invalid" and not runtime.calls


@pytest.mark.parametrize("action,arguments,wire", [
    ("type", {"selector": "#title", "text": "Draft title"}, "type_text"),
    ("select", {"selector": "#category", "option": "draft"}, "select_option"),
    ("scroll", {"direction": "down", "pixels": 200}, "scroll"),
])
async def test_allowed_input_actions_use_internal_background_guards(setup, action, arguments, wire):
    runtime, service = setup
    response = await background(service, action, **arguments)
    assert response["ok"] and len(runtime.effects) == 1 and runtime.effects[0][0] == wire
    assert runtime.effects[0][1]["background_only"] is True and runtime.foreground_checks == 0


async def test_native_foreground_required_is_not_replayed_or_reclassified_as_success(setup):
    runtime, service = setup
    async def modal(method, payload):
        return {"ok": False, "native_dispatched": False, "action_may_have_run": False,
                "error": {"code": "foreground_required", "message": "A native dialog requires foreground interaction"}}
    runtime.handler = modal
    response = await background(service)
    assert response["status"] == "failed" and response["error"]["code"] == "foreground_required"
    assert response["evidence"]["executed"] is False and response["retryable"] is False
    replay = await background(service)
    assert replay["duplicate_call"] and len(runtime.effects) == 1


async def test_adapter_observed_transient_focus_race_keeps_fence_when_active_returns(setup):
    runtime, service = setup
    async def race(method, payload):
        return {"ok": False, "native_terminal": True, "native_state": "failed", "action_may_have_run": True,
                "error": {"code": "native_background_active_changed", "message": "User changed tabs during the native task"}}
    runtime.handler = race
    response = await background(service)
    assert runtime.active == "8" and response["status"] == "outcome_unknown"
    assert response["evidence"]["executed"] is None and not response["retryable"]
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "background-click-0001"


async def test_background_upload_retains_same_input_duplicate_prevention(setup):
    runtime, service = setup
    fields = {"file_path": "D:/fixture/cover.jpg", "selector": "input[type=file]"}
    response = await background(service, "upload_file", **fields)
    assert response["ok"] and runtime.foreground_checks == 0
    assert (await background(service, "upload_file", **fields))["duplicate_call"]
    denied = await background(service, "upload_file", operation_id="background-upload-0002", **fields)
    assert denied["error"]["code"] == "upload_duplicate_prevented"
    assert len(runtime.effects) == 1


async def test_background_press_still_requires_observed_reconciliation(setup):
    runtime, service = setup
    response = await background(service, "press", key="ENTER")
    assert response["status"] == "dispatched_unverified" and not response["evidence"]["verified"]
    denied = await background(service, operation_id="background-click-0002")
    assert denied["error"]["code"] == "browser_target_fenced"


def press_proof():
    return {"ok": True, "verified": True, "adapter": "native_ipc",
            "helper": "xiaoruo1-cef-background-interaction/1",
            "input_semantics": "synthetic_dom_events_untrusted",
            "before": {"body_hash": 101, "value_length": 5},
            "after": {"body_hash": 202, "value_length": 5}}


async def test_background_press_completes_only_with_fixed_helper_and_changed_dom_state(setup):
    runtime, service = setup
    async def observed(method, payload):
        assert method == "press_key" and payload["background_only"] is True
        return press_proof()
    runtime.handler = observed
    response = await background(service, "press", selector="#press", key="ENTER")
    assert response["ok"] and response["status"] == "completed"
    assert response["evidence"] == {"executed": True, "verified": True}
    assert service.receipts.pending(service.resource(BROWSER, "7")) is None
    duplicate = await background(service, "press", selector="#press", key="ENTER")
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False and len(runtime.effects) == 1


@pytest.mark.parametrize("change", [
    {"helper": "unrelated-script"}, {"helper": None},
    {"input_semantics": "trusted_native_input"}, {"input_semantics": None},
    {"before": None}, {"after": []},
    {"after": {"body_hash": 101, "value_length": 5}},
    {"verified": "true"}, {"verified": False},
])
async def test_background_press_cannot_verify_forged_marker_or_missing_dom_effect(setup, change):
    runtime, service = setup
    async def unproven(method, payload):
        return {**press_proof(), **change}
    runtime.handler = unproven
    response = await background(service, "press", selector="#press", key="ENTER")
    assert response["status"] == "dispatched_unverified" and not response["evidence"]["verified"]
    assert response["retryable"] is False and service.receipts.pending(service.resource(BROWSER, "7")) == "background-click-0001"


async def test_foreground_press_still_unverified_even_with_background_helper_marker(setup):
    runtime, service = setup
    service.desktop_check = None
    async def observed(method, payload):
        assert "background_only" not in payload
        return press_proof()
    runtime.handler = observed
    response = await service.control(BROWSER, "press", "7", {"expected_generation": GEN, "selector": "#press", "key": "ENTER"}, "foreground-press-0001", True)
    assert response["status"] == "dispatched_unverified" and not response["evidence"]["verified"]


async def test_public_caller_cannot_supply_internal_adapter_guards(setup):
    runtime, service = setup
    response = await background(service, background_only=True, expected_active_tab_id="7")
    assert response["error"]["code"] == "browser_arguments_invalid" and not runtime.calls


async def test_cancellation_after_dispatch_preserves_original_fence(setup):
    runtime, service = setup
    started = asyncio.Event()
    async def handler(method, payload):
        started.set()
        await asyncio.Event().wait()
    runtime.handler = handler
    task = asyncio.create_task(background(service))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = service.receipts.get("background-click-0001")
    assert saved["state"] == "outcome_unknown"
    assert json.loads(saved["result"])["background_execution"]["expected_active_tab_id"] == "8"
    assert len(runtime.effects) == 1


async def test_cancellation_during_post_dispatch_probe_retains_returned_task_receipt(setup):
    runtime, service = setup
    probing = asyncio.Event()
    dispatch = service.dispatch

    async def handler(method, payload):
        return {"ok": True, "verified": True, "native_task_id": "xr-42-18", "native_state": "succeeded",
                "native_task": {"taskId": "xr-42-18", "state": "succeeded", "action": "upload_file"}}

    async def paused_probe(browser_id, generation, method, payload, timeout):
        if method == "list_tabs" and runtime.effects:
            probing.set()
            await asyncio.Event().wait()
        return await dispatch(browser_id, generation, method, payload, timeout)

    runtime.handler = handler
    service.dispatch = paused_probe
    task = asyncio.create_task(background(service))
    await asyncio.wait_for(probing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = json.loads(service.receipts.get("background-click-0001")["result"])
    assert saved["status"] == "outcome_unknown" and saved["retryable"] is False
    assert saved["native_task_id"] == "xr-42-18" and saved["result"]["native_state"] == "succeeded"
    assert saved["evidence"] == {"executed": None, "verified": False}
    assert service.receipts.pending(service.resource(BROWSER, "7")) == "background-click-0001"
    assert len(runtime.effects) == 1
