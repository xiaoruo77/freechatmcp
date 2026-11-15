"""Transport routing tests use fakes exclusively: no bridge, GUI, or network."""
import asyncio
import copy
import json

import pytest

from web_tools.remote_control_route import PreferredRemoteController


ALIASES = {"DEMO-NODE": {"uu_device_name": "演示设备", "receiver_device_name": "DEMO-NODE"},
           "OTHER": {"uu_device_name": "Other UU", "receiver_device_name": "Other receiver"}}
SESSION = "b" * 32


def envelope(data, outer_ok=True, may_have_run=None, error_code=None):
    return {"ok": outer_ok, "result": {"data": data}, "error_code": error_code,
            "evidence": {"action_may_have_run": may_have_run}}


def available_failure(code="uu_device_offline", may_have_run=False, **data):
    return envelope({"ok": False, "error_code": code, "action_may_have_run": may_have_run, **data},
                    may_have_run=may_have_run)


class FakeUU:
    def __init__(self, connect=None):
        self.calls = []
        self.responses = {}
        self.responses["connect"] = connect if connect is not None else envelope({"ok": True, "connected": True, "keyboard_ready": True})

    async def __call__(self, action, args, confirm):
        self.calls.append((action, copy.deepcopy(args), confirm))
        result = self.responses.get(action, envelope({"ok": True, "connected": True, "keyboard_ready": True}))
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class FakeReceiver:
    def __init__(self):
        self.calls = []

    async def perform(self, action, args, confirm=False):
        self.calls.append((action, copy.deepcopy(args), confirm))
        if action == "connect":
            return {"ok": True, "status": "queued", "terminal": False, "session_state": "starting",
                    "session_id": SESSION, "command_id": "c" * 32}
        if action == "enroll":
            return {"ok": True, "status": "completed", "terminal": True, "enrollment_token": "once_only"}
        if action == "close":
            return {"ok": True, "status": "closed", "terminal": True, "session_id": SESSION}
        return {"ok": True, "status": "completed", "terminal": True, "session_id": SESSION}


def call(controller, action, args=None, confirm=True):
    return asyncio.run(controller.perform(action, args, confirm))


def connect(controller, operation="connect-1", device="DEMO-NODE"):
    return call(controller, "connect", {"device_name": device, "operation_id": operation})


def test_uu_is_first_and_ready_has_opaque_nonterminal_session():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert result["transport"] == result["preferred_transport"] == "uu"
    assert result["status"] == "ready" and result["terminal"] is False
    assert len(result["session_id"]) == 32 and result["session_id"] != SESSION
    assert uu.calls == [("connect", {"device_name": "演示设备"}, True)]
    assert receiver.calls == []


@pytest.mark.parametrize("code", ["uu_device_offline", "uu_not_configured", "uu_cli_unavailable",
    "uu_cli_worker_unavailable", "uu_login_required", "uu_login_pending", "uu_graphics_proxy_unavailable",
    "uu_startup_timeout", "uu_connect_timeout", "uu_operation_timeout"])
def test_only_proven_unavailable_connect_can_choose_receiver(code):
    uu, receiver = FakeUU(available_failure(code)), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert result["transport"] == "receiver" and result["status"] == "queued"
    assert result["terminal"] is False and result["session_id"] == SESSION
    assert result["uu_unavailable_code"] == code
    assert receiver.calls == [("connect", {"device_name": "DEMO-NODE", "operation_id": "connect-1"}, True)]


@pytest.mark.parametrize("code", ["uu_default_receiver_required", "uu_receiver_identity_changed", "uu_executable_invalid",
    "uu_interactive_desktop_required", "uu_device_ambiguous", "uu_session_busy", "uu_endpoint_cleanup_required",
    "uu_remote_outcome_unknown", "uu_remote_input_timeout", "uu_shared_gui_failed", "permission_denied"])
def test_safety_identity_ambiguous_unknown_and_other_errors_never_fallback(code):
    uu, receiver = FakeUU(available_failure(code)), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert not result["ok"] and result["transport"] == "uu"
    assert result["error_code"] == code and receiver.calls == []


@pytest.mark.parametrize("may_have_run", [True, None])
def test_no_fallback_without_explicit_no_input_proof(may_have_run):
    uu, receiver = FakeUU(available_failure(may_have_run=may_have_run)), FakeReceiver()
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["transport"] == "uu" and not result["ok"] and receiver.calls == []


def test_conflicting_evidence_true_overrides_false():
    response = available_failure()
    response["evidence"]["action_may_have_run"] = True
    receiver = FakeReceiver()
    result = connect(PreferredRemoteController(FakeUU(response), receiver, ALIASES))
    assert result["transport"] == "uu" and receiver.calls == []


def test_read_only_cli_timeout_requires_initial_connect_phase():
    for phase, expected in [("connect", "receiver"), ("remote_endpoint_start", "uu"), ("remote_capture_configure", "uu"), (None, "uu")]:
        uu, receiver = FakeUU(available_failure("uu_cli_timeout", phase=phase)), FakeReceiver()
        assert connect(PreferredRemoteController(uu, receiver, ALIASES))["transport"] == expected


def test_uu_exception_is_unknown_not_a_reason_to_fallback_or_repeat_same_connect():
    uu, receiver = FakeUU(TimeoutError("private exception")), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert result["error_code"] == "uu_transport_failure" and "private exception" not in json.dumps(result)
    assert connect(controller) == result and len(uu.calls) == 1 and receiver.calls == []


def test_outer_envelope_success_does_not_hide_inner_failure():
    uu, receiver = FakeUU(available_failure()), FakeReceiver()
    assert connect(PreferredRemoteController(uu, receiver, ALIASES))["transport"] == "receiver"


def test_connect_dedupes_same_operation_and_reuses_active_uu_for_new_operation():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    first = connect(controller)
    assert connect(controller)["session_id"] == first["session_id"]
    assert connect(controller, "new-connect")["session_id"] == first["session_id"]
    assert len(uu.calls) == 1 and receiver.calls == []
    assert connect(controller, "other", "OTHER")["error_code"] == "uu_route_busy"
    assert len(uu.calls) == 1


def test_uu_subsequent_actions_stay_uu_and_preserve_supported_input_schema():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    target = {"hwnd": 10, "pid": 20, "creation_time_utc_ticks": "123"}
    result = call(controller, "type", {"session_id": session, "operation_id": "type-1", "text": "hello", "expected_target": target, "activate": True})
    assert result["terminal"] is True and result["verification_required"]
    assert uu.calls[-1] == ("type", {"operation_id": "type-1", "text": "hello", "expected_target": target, "activate": True}, True)
    call(controller, "screenshot", {"session_id": session, "operation_id": "shot-1"})
    assert uu.calls[-1] == ("screenshot", {}, True)
    status = call(controller, "session_status", {"session_id": session}, False)
    assert uu.calls[-1] == ("status", {}, False) and status["terminal"] is False
    assert call(controller, "command_status", {"session_id": session, "command_id": "d" * 32})["error_code"] == "uu_command_status_not_supported"
    assert receiver.calls == []


def test_unknown_uu_input_is_never_replayed_or_switched_to_receiver():
    uu, receiver = FakeUU(), FakeReceiver()
    uu.responses["type"] = available_failure("uu_remote_outcome_unknown", True)
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    args = {"session_id": session, "operation_id": "type-1", "text": "once"}
    first = call(controller, "type", args)
    assert first["status"] == "outcome_unknown"
    assert call(controller, "type", args) == first
    assert call(controller, "type", dict(args, text="different"))["error_code"] == "operation_id_conflict"
    assert call(controller, "key", {"session_id": session, "operation_id": "key-1", "keys": "ENTER"})["error_code"] == "remote_outcome_unknown"
    assert len(uu.calls) == 2 and receiver.calls == []


def test_receiver_route_is_fixed_even_when_uu_later_recovers():
    uu, receiver = FakeUU(available_failure()), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    uu.responses["connect"] = envelope({"ok": True, "connected": True, "keyboard_ready": True})
    args = {"session_id": session, "operation_id": "type-1", "text": "hello"}
    assert call(controller, "type", args)["transport"] == "receiver"
    assert receiver.calls[-1] == ("type", args, True)
    assert connect(controller, "connect-again")["session_id"] == session
    assert len(uu.calls) == 1


def test_setup_enroll_devices_never_start_uu_and_one_time_enrollment_is_explicit():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    for action in ("setup", "devices", "status"):
        assert call(controller, action, {})["transport"] == "receiver"
    assert call(controller, "enroll", {"device_name": "second", "expected_computer": "SECOND"})["enrollment_token"] == "once_only"
    assert uu.calls == []


def test_missing_receiver_does_not_disable_available_uu():
    uu = FakeUU()
    assert connect(PreferredRemoteController(uu, None, ALIASES))["transport"] == "uu"
    result = connect(PreferredRemoteController(FakeUU(available_failure()), None, ALIASES))
    assert result["error_code"] == "remote_receiver_not_configured"


def test_close_only_selected_route_is_idempotent_and_other_receiver_route_is_untouched():
    uu, receiver = FakeUU(), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    result = call(controller, "close", {"session_id": session, "operation_id": "close-1"})
    assert result["status"] == "closed" and uu.calls[-1] == ("close", {}, True)
    count = len(uu.calls)
    assert call(controller, "close", {"session_id": session, "operation_id": "close-1"})["status"] == "closed"
    assert len(uu.calls) == count and receiver.calls == []
    assert call(controller, "close", {"session_id": "f" * 32, "operation_id": "other"})["error_code"] == "remote_route_not_found"
    assert len(uu.calls) == count


def test_routes_and_alias_copy_are_bounded_and_no_config_is_modified(tmp_path):
    config = tmp_path / "uu.json"
    config.write_text('{"enabled":true}', encoding="utf-8")
    before = config.read_bytes()
    aliases = copy.deepcopy(ALIASES)
    uu, receiver = FakeUU(), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    controller = PreferredRemoteController(uu, receiver, aliases)
    for number in range(70):
        session = connect(controller, str(number))["session_id"]
        call(controller, "close", {"session_id": session, "operation_id": "close-" + str(number)})
    assert len(controller._routes) <= 64 and len(controller._connect_operations) <= 128
    assert config.read_bytes() == before and aliases == ALIASES


def test_same_session_input_calls_are_serialized():
    active, maximum = 0, 0
    uu = FakeUU()
    async def delayed(action, args, confirm):
        nonlocal active, maximum
        if action != "connect":
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(.01)
            active -= 1
        return await uu(action, args, confirm)
    controller = PreferredRemoteController(delayed, FakeReceiver(), ALIASES)
    async def exercise():
        session = (await controller.perform("connect", {"device_name": "DEMO-NODE", "operation_id": "connect"}, True))["session_id"]
        await asyncio.gather(*(controller.perform("type", {"session_id": session, "operation_id": str(index), "text": "hello"}, True) for index in range(5)))
    asyncio.run(exercise())
    assert maximum == 1


def test_confirm_required_before_any_connect_or_input():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = call(controller, "connect", {"device_name": "DEMO-NODE", "operation_id": "connect"}, False)
    assert result["error_code"] == "confirmation_required" and not uu.calls and not receiver.calls


def test_response_does_not_expose_backend_credentials_or_echoed_input():
    uu = FakeUU()
    uu.responses["screenshot"] = envelope({"ok": True, "token": "secret-token", "text": "typed secret", "capture": {"path": "C:/private/image.png", "sha256": "a" * 64, "upload_token": "secret-upload"}})
    controller = PreferredRemoteController(uu, FakeReceiver(), ALIASES)
    session = connect(controller)["session_id"]
    result = call(controller, "screenshot", {"session_id": session})
    assert result["capture"]["path"] == "C:/private/image.png"
    assert "secret-token" not in json.dumps(result) and "typed secret" not in json.dumps(result) and "secret-upload" not in json.dumps(result)


def acceptance(controller, operation="acceptance-1", device="DEMO-NODE", confirm=True):
    return call(controller, "connect", {"device_name": device, "operation_id": operation,
                                       "acceptance_test": True}, confirm)


def test_explicit_receiver_acceptance_uses_receiver_without_checking_available_uu():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = acceptance(controller)
    assert result["transport"] == "receiver" and result["preferred_transport"] == "uu"
    assert result["explicit_receiver_acceptance"] is True and result["session_id"] == SESSION
    assert result["status"] == "queued" and result["terminal"] is False
    assert "uu_unavailable_code" not in result and uu.calls == []
    assert receiver.calls == [("connect", {"device_name": "DEMO-NODE", "operation_id": "acceptance-1"}, True)]


@pytest.mark.parametrize("value", [False, 0, 1, "true", None, [], {}])
def test_acceptance_marker_only_accepts_explicit_boolean_true(value):
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = call(controller, "connect", {"device_name": "DEMO-NODE", "operation_id": "acceptance-1", "acceptance_test": value})
    assert result["error_code"] == "invalid_arguments" and not uu.calls and not receiver.calls


def test_acceptance_still_requires_confirmation():
    uu, receiver = FakeUU(), FakeReceiver()
    result = acceptance(PreferredRemoteController(uu, receiver, ALIASES), confirm=False)
    assert result["error_code"] == "confirmation_required" and not uu.calls and not receiver.calls


def test_acceptance_same_operation_dedupes_and_changed_marker_conflicts():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = acceptance(controller)
    assert acceptance(controller)["session_id"] == result["session_id"]
    assert len(receiver.calls) == 1 and not uu.calls
    changed = connect(controller, "acceptance-1")
    assert changed["error_code"] == "operation_id_conflict" and len(receiver.calls) == 1 and not uu.calls


def test_default_connect_cannot_be_reinterpreted_as_acceptance_with_same_id():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    connect(controller, "same-id")
    result = acceptance(controller, "same-id")
    assert result["error_code"] == "operation_id_conflict" and len(uu.calls) == 1 and not receiver.calls


@pytest.mark.parametrize("device", ["DEMO-NODE", "OTHER"])
def test_acceptance_never_skips_an_active_uu_route(device):
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    existing = connect(controller)["session_id"]
    result = acceptance(controller, device=device)
    assert result["error_code"] == "remote_route_busy" and not receiver.calls
    assert controller._routes[existing].transport == "uu" and len(uu.calls) == 1


def test_acceptance_does_not_skip_uncertain_input_route():
    uu, receiver = FakeUU(), FakeReceiver()
    uu.responses["type"] = available_failure("uu_remote_outcome_unknown", True)
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    call(controller, "type", {"session_id": session, "operation_id": "type-1", "text": "once"})
    assert acceptance(controller, device="OTHER")["error_code"] == "remote_route_busy"
    assert len(uu.calls) == 2 and not receiver.calls


def test_acceptance_does_not_replace_active_receiver_route_with_a_new_operation():
    uu, receiver = FakeUU(available_failure()), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    existing = connect(controller)["session_id"]
    result = acceptance(controller)
    assert result["error_code"] == "remote_route_busy" and result["session_id"] == existing
    assert len(uu.calls) == 1 and len(receiver.calls) == 1


def test_acceptance_after_closed_uu_uses_bound_receiver_for_later_images_and_input():
    class ImageReceiver(FakeReceiver):
        async def perform(self, action, args, confirm=False):
            result = await super().perform(action, args, confirm)
            if action == "screenshot":
                result["capture"] = {"path": "C:/private/verified.png", "mime_type": "image/png"}
                result["capture_verified"] = True
            return result
    uu, receiver = FakeUU(), ImageReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    old = connect(controller)["session_id"]
    call(controller, "close", {"session_id": old, "operation_id": "close-1"})
    session = acceptance(controller)["session_id"]
    image = call(controller, "screenshot", {"session_id": session, "operation_id": "shot"})
    assert image["capture"]["path"] == "C:/private/verified.png" and image["capture_verified"]
    assert image["explicit_receiver_acceptance"] is True
    call(controller, "type", {"session_id": session, "operation_id": "type-1", "text": "hello"})
    assert [item[0] for item in uu.calls] == ["connect", "close"]
    assert [item[0] for item in receiver.calls] == ["connect", "screenshot", "type"]


def test_acceptance_does_not_make_receiver_mandatory_for_default_uu():
    uu = FakeUU()
    controller = PreferredRemoteController(uu, None, ALIASES)
    result = acceptance(controller)
    assert result["error_code"] == "remote_receiver_not_configured" and not uu.calls
    assert connect(controller)["transport"] == "uu"


def bootstrap_failure(stage="bootstrap_chunk_9", phase="remote_endpoint_start", may_have_run=False,
                      code="uu_bootstrap_integrity_failed"):
    response = available_failure(code, may_have_run, phase=phase, stage=stage)
    response.update(ok=False, status="failed", terminal=True,
                    error_code=code, error={"code": code, "message": "The temporary remote endpoint did not start"})
    return response


@pytest.mark.parametrize("stage", ["bootstrap_reader", "bootstrap_chunk_0", "bootstrap_chunk_9",
                                  "bootstrap_chunk_123", "verify", "helper_ready"])
def test_known_bootstrap_transport_failure_falls_back_only_after_exact_cleanup(stage):
    events = []
    class OrderedUU(FakeUU):
        async def __call__(self, action, args, confirm):
            events.append("uu:" + action)
            return await super().__call__(action, args, confirm)
    class OrderedReceiver(FakeReceiver):
        async def perform(self, action, args, confirm=False):
            events.append("receiver:" + action)
            return await super().perform(action, args, confirm)
    uu, receiver = OrderedUU(bootstrap_failure(stage)), OrderedReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True,
                                      "local_receiver_processes_preserved": True})
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert events == ["uu:connect", "uu:close", "receiver:connect"]
    assert uu.calls[-1] == ("close", {}, True)
    assert result["transport"] == "receiver" and result["status"] == "queued"
    assert result["uu_unavailable_code"] == "uu_bootstrap_integrity_failed"
    assert result["uu_cleanup_confirmed"] is True
    assert connect(controller)["session_id"] == result["session_id"]
    assert events == ["uu:connect", "uu:close", "receiver:connect"]


@pytest.mark.parametrize("cleanup", [
    envelope({"ok": True, "cleanup_complete": False}),
    envelope({"ok": True}),
    envelope({"ok": True, "cleanup_complete": "true"}),
    envelope({"ok": False, "cleanup_complete": True}),
    envelope({"ok": True, "cleanup_complete": True}, outer_ok=False),
    envelope({"ok": True, "cleanup_complete": True}, may_have_run=True),
    envelope({"ok": True, "cleanup_complete": True, "error_code": "uu_remote_outcome_unknown"}),
    available_failure("uu_terminal_management_failed", True),
    TimeoutError("PRIVATE_CLEANUP_ERROR"),
])
def test_bootstrap_fallback_refuses_failed_unknown_exception_or_unproven_cleanup(cleanup):
    uu, receiver = FakeUU(bootstrap_failure()), FakeReceiver()
    uu.responses["close"] = cleanup
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    result = connect(controller)
    assert result["error_code"] == "uu_fallback_cleanup_unconfirmed"
    assert result["transport"] == "uu" and not result["ok"]
    assert not result.get("uu_cleanup_confirmed") and receiver.calls == []
    assert [item[0] for item in uu.calls] == ["connect", "close"]
    assert "PRIVATE_CLEANUP_ERROR" not in json.dumps(result)
    assert connect(controller) == result and len(uu.calls) == 2


@pytest.mark.parametrize("overrides", [
    {"may_have_run": True}, {"may_have_run": None}, {"phase": "connect"}, {"phase": "request_type"},
    {"stage": "request_type"}, {"stage": "bootstrap_chunk_1000"}, {"stage": "bootstrap_chunk_9_secret"},
    {"stage": "bootstrap_chunk_9\nPRIVATE"}, {"stage": None},
    {"code": "uu_receiver_identity_changed"}, {"code": "uu_remote_runtime_rejected"},
    {"code": "interactive_desktop_required"},
    {"code": "uu_remote_input_timeout", "phase": "remote_capture_configure"},
])
def test_bootstrap_cleanup_branch_is_limited_to_known_pre_input_transport_evidence(overrides):
    uu, receiver = FakeUU(bootstrap_failure(**overrides)), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["transport"] == "uu" and not result["ok"]
    assert [item[0] for item in uu.calls] == ["connect"] and not receiver.calls


def test_in_session_bootstrap_named_read_error_never_changes_bound_transport():
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    uu.responses["screenshot"] = bootstrap_failure()
    result = call(controller, "screenshot", {"session_id": session})
    assert result["transport"] == "uu" and not result["ok"]
    assert [item[0] for item in uu.calls] == ["connect", "screenshot"] and not receiver.calls


TERMINAL_STARTUP_CODES = ["uu_terminal_management_failed", "uu_terminal_management_rejected",
    "uu_remote_input_timeout", "uu_terminal_exited", "uu_terminal_read_failed", "uu_terminal_poll_failed"]


@pytest.mark.parametrize("code", TERMINAL_STARTUP_CODES)
def test_terminal_startup_transport_codes_require_ordered_proven_cleanup_before_receiver(code):
    events = []
    class OrderedUU(FakeUU):
        async def __call__(self, action, args, confirm):
            events.append("uu:" + action)
            return await super().__call__(action, args, confirm)
    class OrderedReceiver(FakeReceiver):
        async def perform(self, action, args, confirm=False):
            events.append("receiver:" + action)
            return await super().perform(action, args, confirm)
    uu, receiver = OrderedUU(bootstrap_failure(stage="term_create", code=code)), OrderedReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True,
        "remote_terminal_absent": True, "local_receiver_processes_preserved": True})
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert events == ["uu:connect", "uu:close", "receiver:connect"]
    assert uu.calls[-1] == ("close", {}, True)
    assert result["transport"] == "receiver" and result["uu_cleanup_confirmed"] is True
    assert result["uu_unavailable_code"] == code


@pytest.mark.parametrize("code", TERMINAL_STARTUP_CODES)
@pytest.mark.parametrize("proof", [True, None])
def test_terminal_startup_failure_without_false_input_proof_cannot_cleanup_or_fallback(code, proof):
    uu, receiver = FakeUU(bootstrap_failure(stage="term_create", code=code, may_have_run=proof)), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["transport"] == "uu" and not result["ok"]
    assert [item[0] for item in uu.calls] == ["connect"] and not receiver.calls


@pytest.mark.parametrize("code", TERMINAL_STARTUP_CODES)
def test_terminal_startup_failure_never_falls_back_when_cleanup_is_unconfirmed(code):
    uu, receiver = FakeUU(bootstrap_failure(stage="probe", code=code)), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": False})
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["error_code"] == "uu_fallback_cleanup_unconfirmed"
    assert [item[0] for item in uu.calls] == ["connect", "close"] and not receiver.calls


@pytest.mark.parametrize("stage", ["term_create", "orphan_cleanup", "probe", "shell_prompt",
    "bootstrap_reader", "bootstrap_chunk_0", "bootstrap_chunk_999", "verify", "helper_ready"])
def test_terminal_initialization_stages_are_finite_and_can_be_reconciled(stage):
    uu, receiver = FakeUU(bootstrap_failure(stage=stage, code="uu_terminal_management_rejected")), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    assert connect(PreferredRemoteController(uu, receiver, ALIASES))["transport"] == "receiver"


@pytest.mark.parametrize("stage", ["ready", "request_type", "request_capture", "term_create_extra",
    "bootstrap_chunk_1000", "shell_prompt\nsecret", None])
def test_terminal_transport_failure_outside_allowed_startup_stages_cannot_switch(stage):
    uu, receiver = FakeUU(bootstrap_failure(stage=stage, code="uu_terminal_management_rejected")), FakeReceiver()
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["transport"] == "uu" and [item[0] for item in uu.calls] == ["connect"] and not receiver.calls


@pytest.mark.parametrize("code", ["uu_remote_runtime_rejected", "uu_remote_protocol_invalid",
    "uu_remote_protocol_mismatch", "remote_computer_mismatch", "uu_receiver_identity_changed",
    "interactive_desktop_required", "interactive_session_required", "permission_denied"])
def test_startup_identity_runtime_protocol_and_security_codes_never_cleanup_to_fallback(code):
    uu, receiver = FakeUU(bootstrap_failure(stage="helper_ready", code=code)), FakeReceiver()
    uu.responses["close"] = envelope({"ok": True, "cleanup_complete": True})
    result = connect(PreferredRemoteController(uu, receiver, ALIASES))
    assert result["transport"] == "uu" and not result["ok"]
    assert [item[0] for item in uu.calls] == ["connect"] and not receiver.calls


@pytest.mark.parametrize("code", TERMINAL_STARTUP_CODES)
def test_terminal_errors_during_bound_inputs_never_switch_or_close_the_channel(code):
    uu, receiver = FakeUU(), FakeReceiver()
    controller = PreferredRemoteController(uu, receiver, ALIASES)
    session = connect(controller)["session_id"]
    uu.responses["type"] = bootstrap_failure(stage="probe", code=code)
    result = call(controller, "type", {"session_id": session, "operation_id": "type-1", "text": "once"})
    assert result["transport"] == "uu" and not result["ok"]
    assert [item[0] for item in uu.calls] == ["connect", "type"] and not receiver.calls
