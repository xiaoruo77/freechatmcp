"""Receiver controller tests use no GUI, local input, or external network."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from web_tools.remote_gui_capture import CaptureTicketManager
from web_tools.remote_receiver_control import RemoteReceiverController


class FakeManager:
    def __init__(self):
        self.calls = []
        self.commands = {}

    def devices(self):
        return [{"device_name": "lab", "expected_computer": "LAB-PC", "online": True,
                 "token": "must-never-appear"}]

    def issue_enrollment(self, name, computer):
        self.calls.append(("enroll", name, computer))
        return {"enrollment_id": "enroll-1", "enrollment_token": "private-enrollment",
                "device_name": name, "expected_computer": computer,
                "origin": "https://remote.example", "expires_at": 1,
                "main_token": "must-never-appear"}

    def start_session(self, name, operation_id):
        self.calls.append(("connect", name, operation_id))
        return {"session_id": "session-1", "command_id": "command-start", "state": "queued"}

    def session_status(self, session_id):
        return {"session_id": session_id, "device_name": "lab", "state": "ready", "token": "private"}

    def submit(self, session_id, action, args, operation_id):
        self.calls.append((session_id, action, dict(args), operation_id))
        command_id = f"command-{len(self.commands) + 1}"
        value = {"session_id": session_id, "command_id": command_id, "action": action,
                 "state": "queued", "arguments": args}
        self.commands[(session_id, command_id)] = value
        return value

    def command_status(self, session_id, command_id):
        return self.commands[(session_id, command_id)]

    def close_session(self, session_id, operation_id):
        self.calls.append(("close", session_id, operation_id))
        return {"session_id": session_id, "command_id": "command-close", "state": "queued"}


@pytest.fixture
def setup(tmp_path):
    manager = FakeManager()
    tickets = CaptureTicketManager(tmp_path, "https://remote.example")
    controller = RemoteReceiverController(manager, tickets, tmp_path)
    return controller, manager, tickets


async def completed_capture(setup, *, operation_id="shot-1", left=-10, top=-20,
                            mime_type="image/png", exif_orientation=None):
    controller, manager, tickets = setup
    result = await controller.perform("screenshot", {"session_id": "session-1", "operation_id": operation_id})
    command = manager.commands[("session-1", result["command_id"])]
    ticket = command["arguments"]
    image = io.BytesIO()
    picture = Image.new("RGB", (3, 2), "green")
    options = {}
    if exif_orientation is not None:
        exif = picture.getexif()
        exif[274] = exif_orientation
        options["exif"] = exif
    picture.save(image, format="JPEG" if mime_type == "image/jpeg" else "PNG", **options)
    payload = image.getvalue()
    tickets.claim(ticket["capture_id"], ticket["token"])
    if mime_type == "image/png":
        tickets.store(ticket["capture_id"], payload, (3, 2))
    else:
        tickets.store(ticket["capture_id"], payload, (3, 2), mime_type=mime_type)
    receipt = {"capture_id": ticket["capture_id"], "sha256": hashlib.sha256(payload).hexdigest(),
               "remote_bounds": {"left": left, "top": top, "width": 3, "height": 2},
               "foreground": {"available": True, "hwnd": 123, "pid": 456,
                              "creation_time_utc_ticks": "123456"}}
    command.update(state="completed", result=receipt)
    return result, command, receipt


async def poll(controller, result):
    return await controller.perform("command_status", {"session_id": "session-1", "command_id": result["command_id"]})


@pytest.mark.asyncio
async def test_no_write_without_explicit_confirmation(setup):
    controller, manager, _ = setup
    result = await controller.perform("connect", {"device_name": "lab", "operation_id": "connect-1"})
    assert result["error_code"] == "confirmation_required"
    assert not manager.calls


@pytest.mark.asyncio
async def test_devices_and_enrollment_filter_secrets(setup):
    controller, _, _ = setup
    status = await controller.perform("status", {"device_name": "lab"})
    assert status["devices"] == [{"device_name": "lab", "expected_computer": "LAB-PC", "online": True}]
    enrollment = await controller.perform("enroll", {"device_name": "lab", "expected_computer": "LAB-PC"}, True)
    assert enrollment["enrollment_token"] == "private-enrollment"
    assert "must-never-appear" not in json.dumps(enrollment)


@pytest.mark.asyncio
async def test_real_manager_device_poll_presentation_survives_controller_projection(tmp_path):
    from web_tools.remote_receiver_server import DEVICE_POLL_INTERVAL_SECONDS, RemoteReceiverManager
    manager = RemoteReceiverManager(tmp_path, "https://remote.example")
    controller = RemoteReceiverController(manager, None, tmp_path)
    try:
        invitation = manager.issue_enrollment("lab", "LAB-PC")
        device = manager.enroll(invitation["enrollment_id"], invitation["enrollment_token"], "LAB-PC")
        first = (await controller.perform("devices", {}))["devices"][0]
        assert first["availability"] == "never_seen"
        assert first["last_seen_age_seconds"] is None
        assert first["poll_interval_seconds"] == DEVICE_POLL_INTERVAL_SECONDS
        assert first["connection_mode"] == "periodic_poll"
        assert "online" not in first
        await manager.poll(device["device_id"], device["device_token"], "LAB-PC", "1" * 32,
                           timeout_seconds=0)
        seen = (await controller.perform("status", {"device_name": "lab"}))["devices"][0]
        assert seen["availability"] == "recently_seen"
        assert isinstance(seen["last_seen_age_seconds"], float) and seen["last_seen_age_seconds"] >= 0
        assert seen["poll_interval_seconds"] == DEVICE_POLL_INTERVAL_SECONDS
        assert seen["connection_mode"] == "periodic_poll"
        serialized = json.dumps(seen)
        assert device["device_token"] not in serialized and invitation["enrollment_token"] not in serialized
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_queued_is_nonterminal_and_never_claims_remote_success(setup):
    controller, _, tickets = setup
    result = await controller.perform("screenshot", {"session_id": "session-1", "operation_id": "shot"})
    assert result["status"] == "queued" and result["terminal"] is False
    assert result["remote_effect_verified"] is False
    assert result["background_only"] is True
    assert result["host_input_used"] is result["foreground_input_used"] is False
    assert len(tickets._tickets) == 1
    assert "token" not in json.dumps(result)
    assert "arguments" not in result


@pytest.mark.asyncio
async def test_receiver_without_capture_service_reports_unavailable_without_dispatch(tmp_path):
    manager = FakeManager()
    controller = RemoteReceiverController(manager, None, tmp_path)
    result = await controller.perform("screenshot", {"session_id": "session-1", "operation_id": "shot"})
    assert result["error_code"] == "remote_capture_unavailable"
    assert manager.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
async def test_capture_verified_then_ticket_revoked_without_deleting_image(setup, mime_type):
    controller, _, tickets = setup
    queued, _, receipt = await completed_capture(setup, mime_type=mime_type)
    result = await poll(controller, queued)
    assert result["status"] == "completed" and result["capture_verified"] is True
    assert result["remote_effect_verified"] is False
    assert result["capture"]["width"] == 3
    assert result["capture"]["mime_type"] == mime_type
    assert Path(result["capture"]["path"]).suffix == (".jpg" if mime_type == "image/jpeg" else ".png")
    assert Path(result["capture"]["path"]).is_file()
    assert receipt["capture_id"] not in tickets._tickets
    assert (await poll(controller, queued))["capture"] == result["capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
@pytest.mark.parametrize("tamper", ["digest", "bounds", "identity", "path", "fingerprint"])
async def test_capture_verification_rejects_tampering(setup, tmp_path, tamper, mime_type):
    controller, _, tickets = setup
    queued, _, receipt = await completed_capture(setup, mime_type=mime_type)
    capture_id = receipt["capture_id"]
    if tamper == "digest":
        receipt["sha256"] = "0" * 64
    elif tamper == "bounds":
        receipt["remote_bounds"]["width"] = 4
    elif tamper == "identity":
        receipt["capture_id"] = "a" * 32
    elif tamper == "fingerprint":
        receipt["foreground"]["hwnd"] = True
    else:
        outside = tmp_path / "foreign.png"
        outside.write_bytes(Path(tickets._tickets[capture_id].capture["path"]).read_bytes())
        tickets._tickets[capture_id].capture["path"] = str(outside)
    result = await poll(controller, queued)
    assert result["status"] == "failed"
    assert result["error_code"] == "remote_capture_verification_failed"
    assert "capture" not in result
    assert capture_id not in tickets._tickets
    assert (await poll(controller, queued))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
@pytest.mark.parametrize("tamper", ["format", "extension", "unsupported_mime", "receipt_mime"])
async def test_image_format_and_exact_ticket_path_cannot_be_relabeled(setup, mime_type, tamper):
    controller, _, tickets = setup
    queued, _, receipt = await completed_capture(setup, mime_type=mime_type)
    metadata = tickets._tickets[receipt["capture_id"]].capture
    path = Path(metadata["path"])
    other = "image/jpeg" if mime_type == "image/png" else "image/png"
    if tamper == "format":
        image = io.BytesIO()
        Image.new("RGB", (3, 2), "red").save(image, format="JPEG" if other == "image/jpeg" else "PNG")
        content = image.getvalue()
        path.write_bytes(content)
        # Keep hashes, size, bounds and image dimensions consistent so only
        # the claimed MIME versus the actual decoded format causes rejection.
        receipt["sha256"] = metadata["sha256"] = hashlib.sha256(content).hexdigest()
        metadata["size"] = len(content)
    elif tamper == "extension":
        renamed = path.with_suffix(".jpg" if other == "image/jpeg" else ".png")
        path.rename(renamed)
        metadata["path"] = str(renamed)
    elif tamper == "unsupported_mime":
        metadata["mime_type"] = "image/gif"
    else:
        receipt["mime_type"] = other
    result = await poll(controller, queued)
    assert result["status"] == "failed"
    assert result.get("capture_verified") is not True
    assert result["error_code"] == "remote_capture_verification_failed"
    assert "capture" not in result and receipt["capture_id"] not in tickets._tickets


@pytest.mark.asyncio
async def test_jpeg_rotation_metadata_is_rejected_instead_of_transforming_coordinates(setup):
    controller, manager, _ = setup
    queued, _, _ = await completed_capture(setup, mime_type="image/jpeg", exif_orientation=6)
    result = await poll(controller, queued)
    assert result["status"] == "failed" and "capture" not in result
    count = len(manager.calls)
    click = await controller.perform("click", {"session_id": "session-1", "operation_id": "jpeg-click", "x": 2, "y": 1}, True)
    assert click["error_code"] == "fresh_remote_screenshot_required"
    assert len(manager.calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
async def test_pointer_coordinates_translated_once_and_snapshot_consumed(setup, mime_type):
    controller, manager, _ = setup
    queued, _, _ = await completed_capture(setup, mime_type=mime_type)
    await poll(controller, queued)
    args = {"session_id": "session-1", "operation_id": "click-1", "x": 2, "y": 1}
    result = await controller.perform("click", args, True)
    assert result["status"] == "queued"
    submitted = manager.calls[-1]
    assert submitted[1] == "click"
    assert submitted[2] == {"x": -8, "y": -19, "button": "left", "count": 1,
                            "bounds": {"left": -10, "top": -20, "width": 3, "height": 2}}
    count = len(manager.calls)
    repeated = await controller.perform("click", args, True)
    assert repeated["command_id"] == result["command_id"] and len(manager.calls) == count
    next_input = await controller.perform("key", {"session_id": "session-1", "operation_id": "key-2", "keys": "TAB"}, True)
    assert next_input["error_code"] == "fresh_remote_screenshot_required"
    # Polling the old image cannot restore a screenshot consumed by input.
    await poll(controller, queued)
    assert "session-1" not in controller._snapshots


@pytest.mark.asyncio
async def test_type_uses_only_verified_capture_fingerprint_and_requires_new_image(setup):
    controller, manager, _ = setup
    queued, _, _ = await completed_capture(setup)
    await poll(controller, queued)
    result = await controller.perform("type", {"session_id": "session-1", "operation_id": "type-1", "text": "测试🙂"}, True)
    payload = manager.calls[-1][2]
    assert payload == {"text": "测试🙂", "activate": True,
                       "expect": {"hwnd": 123, "pid": 456, "creation_time_utc_ticks": "123456"}}
    command = manager.commands[("session-1", result["command_id"])]
    command.update(state="completed", result={"delivered": True, "token": "must-never-appear"})
    final = await poll(controller, result)
    assert final["delivery_only"] is True and final["verification_required"] is True
    assert final["remote_effect_verified"] is False
    assert "must-never-appear" not in json.dumps(final)


@pytest.mark.asyncio
async def test_stale_capture_cannot_authorize_input(setup):
    controller, manager, _ = setup
    queued, _, _ = await completed_capture(setup)
    await poll(controller, queued)
    captured = controller._snapshots["session-1"].captured_at
    controller._clock = lambda: captured + 61
    count = len(manager.calls)
    result = await controller.perform("click", {"session_id": "session-1", "operation_id": "old-click", "x": 0, "y": 0}, True)
    assert result["error_code"] == "fresh_remote_screenshot_required"
    assert len(manager.calls) == count


@pytest.mark.asyncio
async def test_same_screenshot_operation_reuses_ticket_and_rejects_conflict(setup):
    controller, manager, tickets = setup
    args = {"session_id": "session-1", "operation_id": "shot"}
    first = await controller.perform("screenshot", args)
    second = await controller.perform("screenshot", args)
    assert first["command_id"] == second["command_id"]
    assert len(manager.calls) == len(tickets._tickets) == 1
    conflicting = await controller.perform("key", dict(args, keys="TAB"), True)
    assert conflicting["error_code"] == "operation_id_conflict"


@pytest.mark.asyncio
async def test_failed_and_unknown_capture_are_terminal_and_revoke_ticket(setup):
    controller, manager, tickets = setup
    for index, state in enumerate(("failed", "outcome_unknown")):
        queued = await controller.perform("screenshot", {"session_id": "session-1", "operation_id": f"shot-{index}"})
        manager.commands[("session-1", queued["command_id"])].update(state=state)
        result = await poll(controller, queued)
        assert result["status"] == state and result["terminal"] is True and result["ok"] is False
        assert not tickets._tickets


@pytest.mark.asyncio
async def test_close_revokes_pending_capture_without_requiring_another_poll(setup):
    controller, manager, tickets = setup
    queued = await controller.perform("screenshot", {"session_id": "session-1", "operation_id": "shot"})
    operation = controller._commands[("session-1", queued["command_id"])]
    assert operation.capture_id in tickets._tickets
    closed = await controller.perform("close", {"session_id": "session-1", "operation_id": "close"}, True)
    assert closed["status"] == "queued"
    assert not tickets._tickets
    assert operation.ticket_deleted is True
    manager.commands[("session-1", queued["command_id"])].update(state="completed", result={})
    assert (await poll(controller, queued))["status"] == "failed"
    assert not controller._snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("action,user", [
    ("click", {"x": True, "y": 0}),
    ("click", {"x": 3, "y": 0}),
    ("type", {"text": "x" * 1001}),
    ("type", {"text": "x", "expect": {"hwnd": 1}}),
    ("key", {"keys": "CTRL+ALT+DELETE"}),
    ("drag", {"x": 0, "y": 0, "to_x": 1, "to_y": 1, "duration_ms": 0}),
])
async def test_invalid_input_rejected_before_remote_submission(setup, action, user):
    controller, manager, _ = setup
    queued, _, _ = await completed_capture(setup)
    await poll(controller, queued)
    count = len(manager.calls)
    result = await controller.perform(action, {"session_id": "session-1", "operation_id": "invalid", **user}, True)
    assert result["ok"] is False
    assert len(manager.calls) == count


@pytest.mark.asyncio
async def test_errors_do_not_return_secret_exception_details(setup):
    controller, manager, _ = setup
    def fail():
        raise RuntimeError("private-enrollment and MCP master token")
    manager.devices = fail
    result = await controller.perform("devices", {})
    assert result["error_code"] == "remote_control_failed"
    assert "master token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_late_capture_queued_before_input_never_restores_snapshot(setup):
    controller, _, _ = setup
    first, _, _ = await completed_capture(setup, operation_id="first")
    await poll(controller, first)
    late, _, _ = await completed_capture(setup, operation_id="late")
    click = await controller.perform("click", {"session_id": "session-1", "operation_id": "click", "x": 0, "y": 0}, True)
    assert click["status"] == "queued"
    assert (await poll(controller, late))["capture_verified"] is True
    assert "session-1" not in controller._snapshots


@pytest.mark.asyncio
async def test_receipt_delivery_after_sixty_seconds_does_not_extend_capture_freshness(setup):
    controller, _, _ = setup
    first, _, _ = await completed_capture(setup)
    submitted = controller._commands[("session-1", first["command_id"])].submitted_at
    controller._clock = lambda: submitted + 61
    assert (await poll(controller, first))["capture_verified"] is True
    click = await controller.perform("click", {"session_id": "session-1", "operation_id": "late-click", "x": 0, "y": 0}, True)
    assert click["error_code"] == "fresh_remote_screenshot_required"


@pytest.mark.asyncio
async def test_fenced_session_status_reports_failure_without_runtime_secrets(setup):
    controller, manager, _ = setup
    manager.session_status = lambda session: {"session_id": session, "state": "fenced",
                                             "runtime": {"token": "must-never-appear"}}
    result = await controller.perform("session_status", {"session_id": "session-1"})
    assert result["ok"] is False and result["terminal"] is True
    assert "must-never-appear" not in json.dumps(result)


@pytest.mark.asyncio
async def test_controller_without_ticket_history_cannot_verify_old_capture_receipt(setup, tmp_path):
    controller, manager, tickets = setup
    queued, _, _ = await completed_capture(setup)
    restarted = RemoteReceiverController(manager, tickets, tmp_path)
    result = await poll(restarted, queued)
    assert result["status"] == "failed"
    assert result["error_code"] == "remote_capture_evidence_unavailable"
    assert result["capture_verified"] is False
    assert not restarted._snapshots


@pytest.mark.asyncio
async def test_unstable_capture_foreground_cannot_authorize_guessed_keyboard_target(setup):
    controller, manager, _ = setup
    queued, _, receipt = await completed_capture(setup)
    receipt["foreground"] = {"available": False, "target_changed": True}
    assert (await poll(controller, queued))["capture_verified"] is True
    count = len(manager.calls)
    result = await controller.perform("type", {"session_id": "session-1", "operation_id": "type", "text": "abc"}, True)
    assert result["error_code"] == "remote_foreground_unavailable"
    assert len(manager.calls) == count


@pytest.mark.asyncio
async def test_real_manager_enrollment_capture_input_unknown_and_close(tmp_path):
    from web_tools.remote_receiver_server import RemoteReceiverManager
    tickets = CaptureTicketManager(tmp_path, "https://remote.example")
    manager = RemoteReceiverManager(tmp_path, "https://remote.example", tickets)
    controller = RemoteReceiverController(manager, tickets, tmp_path)
    instance_id = "1" * 32
    try:
        enrollment = await controller.perform("enroll", {"device_name": "lab", "expected_computer": "LAB-PC"}, True)
        device = manager.enroll(enrollment["enrollment_id"], enrollment["enrollment_token"], "LAB-PC")
        auth = (device["device_id"], device["device_token"])
        start = await controller.perform("connect", {"device_name": "lab", "operation_id": "start"}, True)
        assert start["status"] == "queued" and start["terminal"] is False
        session_id = start["session_id"]
        delivered = await manager.poll(*auth, "LAB-PC", instance_id, timeout_seconds=0)
        command = delivered["command"]
        assert command["action"] == "start"
        manager.reply(*auth, command["command_id"], session_id, True,
                      {"runtime": {"computer": "LAB-PC"}})
        assert (await controller.perform("session_status", {"session_id": session_id}))["status"] == "ready"

        screenshot = await controller.perform("screenshot", {"session_id": session_id, "operation_id": "shot"})
        command = (await manager.poll(*auth, "LAB-PC", instance_id, session_id, timeout_seconds=0))["command"]
        ticket = command["arguments"]
        image = io.BytesIO()
        Image.new("RGB", (3, 2), "blue").save(image, format="PNG")
        payload = image.getvalue()
        tickets.claim(ticket["capture_id"], ticket["token"])
        metadata = tickets.store(ticket["capture_id"], payload, (3, 2))
        manager.reply(*auth, command["command_id"], session_id, True, {
            "capture_id": ticket["capture_id"], "sha256": metadata["sha256"],
            "remote_bounds": {"left": -20, "top": 30, "width": 3, "height": 2},
            "foreground": {"available": True, "hwnd": 101, "pid": 202, "creation_time_utc_ticks": "303"}})
        verified = await controller.perform("command_status", {"session_id": session_id, "command_id": screenshot["command_id"]})
        assert verified["status"] == "completed", verified
        assert verified["capture_verified"] is True
        assert not tickets._tickets

        click_args = {"session_id": session_id, "operation_id": "click", "x": 1, "y": 1}
        click = await controller.perform("click", click_args, True)
        delivered = await manager.poll(*auth, "LAB-PC", instance_id, session_id, timeout_seconds=0)
        assert delivered["command"]["arguments"]["x"] == -19
        # Receiver loss after delivery fences this exact operation; the adapter
        # returns the unknown outcome rather than creating another command.
        assert (await manager.poll(*auth, "LAB-PC", instance_id, session_id, timeout_seconds=0))["kind"] == "idle"
        unknown = await controller.perform("click", click_args, True)
        assert unknown["status"] == "outcome_unknown" and unknown["terminal"] is True
        assert unknown["command_id"] == click["command_id"]
        assert unknown["remote_effect_verified"] is False

        close = await controller.perform("close", {"session_id": session_id, "operation_id": "close"}, True)
        assert close["status"] == "queued"
        stop = (await manager.poll(*auth, "LAB-PC", instance_id, session_id, timeout_seconds=0))["command"]
        assert stop["action"] == "stop"
        manager.reply(*auth, stop["command_id"], session_id, True, {"helper_stopped": True})
        assert (await controller.perform("session_status", {"session_id": session_id}))["status"] == "closed"
    finally:
        manager.close()
