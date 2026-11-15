"""No GUI, real remote host, or external network is used by these tests."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from web_tools.remote_receiver_server import (
    COMMAND_RECEIPT_SECONDS, MAX_BODY_BYTES, RemoteReceiverError, RemoteReceiverManager,
    create_receiver_router, receiver_route_uses_own_auth,
)


ORIGIN = "https://receiver.example"
INSTANCE = "b" * 32
COMPUTER = "SECOND-PC"
EXPECT = {"hwnd": 42, "pid": 123, "creation_time_utc_ticks": "123456789"}
TARGET = dict(EXPECT, title="Test", class_name="Window", process_name="notepad", session_id=1)


@pytest.fixture
def manager(tmp_path):
    value = RemoteReceiverManager(tmp_path, ORIGIN)
    yield value
    value.close()


def enrolled(manager, name="second", computer=COMPUTER):
    ticket = manager.issue_enrollment(name, computer)
    return manager.enroll(ticket["enrollment_id"], ticket["enrollment_token"], computer)


def poll(manager, device, instance=INSTANCE, session_id=None):
    return asyncio.run(manager.poll(device["device_id"], device["device_token"], COMPUTER, instance,
                                    session_id, timeout_seconds=0))


def reply(manager, device, command, result, ok=True):
    return manager.reply(device["device_id"], device["device_token"], command["command_id"],
                         command["session_id"], ok, result if ok else None, None if ok else result)


def ready(manager):
    device = enrolled(manager)
    start = manager.start_session("second", "start-1")
    command = poll(manager, device)["command"]
    reply(manager, device, command, {"runtime": {"computer": COMPUTER, "session_id": 1}, "foreground": TARGET})
    assert manager.session_status(start["session_id"])["state"] == "ready"
    return device, start["session_id"]


def assert_code(code, call):
    with pytest.raises(RemoteReceiverError) as error:
        call()
    assert error.value.code == code


def test_tokens_hashed_at_rest_single_use_and_hostname_bound(manager):
    ticket = manager.issue_enrollment("second", COMPUTER)
    assert len(ticket["enrollment_id"]) == 32
    assert_code("receiver_computer_mismatch", lambda: manager.enroll(ticket["enrollment_id"], ticket["enrollment_token"], "OTHER-PC"))
    device = manager.enroll(ticket["enrollment_id"], ticket["enrollment_token"], COMPUTER.lower())
    assert_code("receiver_authentication_required", lambda: manager.enroll(ticket["enrollment_id"], ticket["enrollment_token"], COMPUTER))
    dump = "\n".join(manager._db.iterdump())
    assert ticket["enrollment_token"] not in dump and device["device_token"] not in dump
    assert "token" not in json.dumps(manager.devices())


@pytest.mark.parametrize("computer", ["XIAORUO", "xiaoruo", "XiaoRuo"])
def test_controller_host_is_forbidden(manager, computer):
    assert_code("receiver_controller_forbidden", lambda: manager.issue_enrollment("self", computer))


def test_expired_enrollment_cannot_be_consumed(manager):
    ticket = manager.issue_enrollment("second", COMPUTER, ttl_seconds=1)
    manager._db.execute("UPDATE enrollments SET expires_at=0")
    assert_code("receiver_authentication_required", lambda: manager.enroll(ticket["enrollment_id"], ticket["enrollment_token"], COMPUTER))


def test_only_fixed_actions_and_exact_gui_schema(manager):
    _, session = ready(manager)
    for action in ("exec", "powershell", "open_url", "configure_capture", "create_test_window", "stop"):
        assert_code("receiver_action_invalid", lambda: manager.submit(session, action, {}, "bad"))
    assert_code("receiver_arguments_invalid", lambda: manager.submit(session, "type", {"text": "hello"}, "bad-type"))
    assert_code("receiver_arguments_invalid", lambda: manager.submit(session, "key", {"keys": "CTRL+ALT+DELETE", "expect": EXPECT}, "bad-key"))


def test_start_deduplication_ready_and_one_pending_command(manager):
    device = enrolled(manager)
    start = manager.start_session("second", "start-1")
    assert manager.start_session("second", "start-1")["command_id"] == start["command_id"]
    assert_code("receiver_session_not_ready", lambda: manager.submit(start["session_id"], "status", {}, "too-soon"))
    command = poll(manager, device)["command"]
    assert command["arguments"] == {"origin": ORIGIN}
    reply(manager, device, command, {"runtime": {"computer": COMPUTER}, "foreground": TARGET})
    first = manager.submit(start["session_id"], "type", {"text": "private typed text", "expect": EXPECT}, "type-1")
    assert manager.submit(start["session_id"], "type", {"expect": EXPECT, "text": "private typed text"}, "type-1")["command_id"] == first["command_id"]
    assert_code("receiver_operation_conflict", lambda: manager.submit(start["session_id"], "type", {"text": "different", "expect": EXPECT}, "type-1"))
    assert_code("receiver_command_busy", lambda: manager.submit(start["session_id"], "status", {}, "status-1"))
    assert "private typed text" not in json.dumps(manager.session_status(start["session_id"]))
    assert "arguments" not in manager.command_status(start["session_id"], first["command_id"])


def test_reconnect_never_redelivers_input_and_late_receipt_reconciles(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "type", {"text": "once", "expect": EXPECT}, "type-1")
    command = poll(manager, device)["command"]
    assert poll(manager, device) == {"kind": "idle"}
    assert manager.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    assert manager.session_status(session)["state"] == "fenced"
    assert_code("receiver_session_not_ready", lambda: manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "new-input"))
    reply(manager, device, command, {"foreground": TARGET, "remote_effect_verified": False})
    assert manager.command_status(session, issued["command_id"])["state"] == "completed"
    assert manager.session_status(session)["state"] == "ready"


def test_reply_idempotent_conflicting_receipt_rejected_and_private_fields_dropped(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "status", {}, "status-1")
    command = poll(manager, device)["command"]
    result = {"computer": COMPUTER, "foreground": TARGET, "token": "secret", "text": "private text", "stdout": "private"}
    ack = reply(manager, device, command, result)
    assert reply(manager, device, command, result) == ack
    assert_code("receiver_reply_conflict", lambda: reply(manager, device, command, dict(result, computer="changed")))
    public = json.dumps(manager.command_status(session, issued["command_id"]))
    assert "secret" not in public and "private" not in public


def test_restart_marks_dispatch_unknown_without_replay_and_keeps_same_operation(tmp_path):
    manager = RemoteReceiverManager(tmp_path, ORIGIN)
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-1")
    command = poll(manager, device)["command"]
    manager.close()
    replacement = RemoteReceiverManager(tmp_path, ORIGIN)
    try:
        assert replacement.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
        assert poll(replacement, device) == {"kind": "idle"}
        assert replacement.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-1")["command_id"] == command["command_id"]
        reply(replacement, device, command, {"foreground": TARGET})
        assert replacement.session_status(session)["state"] == "ready"
    finally:
        replacement.close()


def test_new_receiver_instance_fences_old_child_even_after_late_receipt(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-1")
    command = poll(manager, device)["command"]
    assert poll(manager, device, "c" * 32) == {"kind": "idle"}
    reply(manager, device, command, {"foreground": TARGET})
    assert manager.command_status(session, issued["command_id"])["state"] == "completed"
    assert manager.session_status(session)["state"] == "fenced"


def test_queued_new_start_survives_new_receiver_process(manager):
    device = enrolled(manager)
    assert poll(manager, device) == {"kind": "idle"}
    start = manager.start_session("second", "start-1")
    command = poll(manager, device, "c" * 32)["command"]
    reply(manager, device, command, {"runtime": {"computer": COMPUTER}, "foreground": TARGET})
    assert manager.session_status(start["session_id"])["state"] == "ready"


def test_capture_token_memory_only_and_restart_marks_queued_ticket_lost(tmp_path):
    manager = RemoteReceiverManager(tmp_path, ORIGIN)
    device, session = ready(manager)
    capture_id, token = "c" * 32, "capture_secret_" + "A" * 40
    args = {"capture_id": capture_id, "upload_url": ORIGIN + "/remote-gui/captures/" + capture_id, "token": token}
    issued = manager.submit(session, "capture", args, "capture-1")
    dump = "\n".join(manager._db.iterdump())
    assert token not in dump and args["upload_url"] not in dump
    manager.close()
    replacement = RemoteReceiverManager(tmp_path, ORIGIN)
    try:
        assert replacement.command_status(session, issued["command_id"])["error_code"] == "receiver_capture_ticket_lost"
        assert poll(replacement, device) == {"kind": "idle"}
    finally:
        replacement.close()


def test_capture_delivery_and_last_receipt(manager):
    device, session = ready(manager)
    capture_id = "c" * 32
    args = {"capture_id": capture_id, "upload_url": ORIGIN + "/remote-gui/captures/" + capture_id, "token": "A" * 48}
    manager.submit(session, "capture", args, "capture-1")
    command = poll(manager, device)["command"]
    assert command["arguments"] == args
    result = {"capture_id": capture_id, "sha256": "d" * 64, "remote_bounds": {"left": 0, "top": 0, "width": 100, "height": 100}, "foreground": TARGET}
    reply(manager, device, command, result)
    assert manager.session_status(session)["last_capture"] == result
    cross = dict(args, upload_url="https://other.example/remote-gui/captures/" + capture_id)
    assert_code("receiver_capture_origin_invalid", lambda: manager.submit(session, "capture", cross, "capture-2"))


def test_stop_requires_observed_helper_shutdown(manager):
    device, session = ready(manager)
    stop = manager.close_session(session, "close-1")
    command = poll(manager, device)["command"]
    assert command["action"] == "stop"
    assert manager.close_session(session, "close-2")["command_id"] == stop["command_id"]
    assert_code("receiver_stop_unconfirmed", lambda: reply(manager, device, command, {"stopping": True}))
    reply(manager, device, command, {"helper_stopped": True, "receiver_exiting": True})
    assert manager.session_status(session)["state"] == "closed"
    assert manager.start_session("second", "start-2")["state"] == "queued"


def test_close_cancels_queued_input_and_queues_exact_stop(manager):
    device, session = ready(manager)
    pending = manager.submit(session, "type", {"text": "not delivered", "expect": EXPECT}, "type-1")
    manager.close_session(session, "close-1")
    assert manager.command_status(session, pending["command_id"])["error_code"] == "receiver_command_cancelled"
    assert poll(manager, device)["command"]["action"] == "stop"


def test_device_heartbeat_does_not_extend_controller_activity_and_idle_queues_stop(manager):
    device, session = ready(manager)
    manager._db.execute("UPDATE sessions SET activity_at=0 WHERE session_id=?", (session,))
    command = poll(manager, device)["command"]
    assert command["action"] == "stop"
    assert manager.session_status(session)["state"] == "closing"


def test_controller_status_refreshes_activity(manager):
    _, session = ready(manager)
    manager._db.execute("UPDATE sessions SET activity_at=activity_at-100 WHERE session_id=?", (session,))
    prior = manager._db.execute("SELECT activity_at FROM sessions WHERE session_id=?", (session,)).fetchone()[0]
    manager.session_status(session)
    assert manager._db.execute("SELECT activity_at FROM sessions WHERE session_id=?", (session,)).fetchone()[0] > prior


def test_only_exact_post_routes_bypass_outer_auth():
    for path in ("/remote-receiver/enroll", "/remote-receiver/poll", "/remote-receiver/reply"):
        assert receiver_route_uses_own_auth({"type": "http", "method": "POST", "path": path})
        for method in ("GET", "PUT", "DELETE"):
            assert not receiver_route_uses_own_auth({"type": "http", "method": method, "path": path})
        assert not receiver_route_uses_own_auth({"type": "http", "method": "POST", "path": path + "/"})
    assert not receiver_route_uses_own_auth({"type": "http", "method": "POST", "path": "/remote-receiver/exec"})


def test_asgi_enroll_poll_reply_auth_limits_and_safe_errors(tmp_path):
    manager = RemoteReceiverManager(tmp_path, ORIGIN)
    ticket = manager.issue_enrollment("second", COMPUTER)
    app = FastAPI()
    app.include_router(create_receiver_router(manager))
    with TestClient(app) as client:
        assert client.post("/remote-receiver/enroll", json={}).status_code == 401
        auth = {"Authorization": "Bearer " + ticket["enrollment_token"]}
        response = client.post("/remote-receiver/enroll", headers=auth, json={"enrollment_id": ticket["enrollment_id"], "computer_name": COMPUTER})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        device = response.json()
        auth = {"Authorization": "Bearer " + device["device_token"]}
        start = manager.start_session("second", "start-1")
        response = client.post("/remote-receiver/poll", headers=auth, json={"device_id": device["device_id"], "computer_name": COMPUTER, "instance_id": INSTANCE})
        command = response.json()["command"]
        receipt = {"device_id": device["device_id"], "command_id": command["command_id"], "session_id": start["session_id"], "ok": True, "result": {"runtime": {"computer": COMPUTER}, "foreground": TARGET}}
        assert client.post("/remote-receiver/reply", headers=auth, json=receipt).json()["acknowledged"]
        assert client.post("/remote-receiver/reply", headers=auth, json=receipt).status_code == 200
        too_big = client.post("/remote-receiver/reply", headers=auth, content=b"x" * (MAX_BODY_BYTES + 1))
        assert too_big.status_code == 413
        bad = client.post("/remote-receiver/reply", headers=auth, content=b"not json SECRET")
        assert bad.status_code == 400 and "SECRET" not in bad.text
        assert client.post("/remote-receiver/poll", headers=auth, json={"device_id": device["device_id"], "computer_name": "OTHER-PC", "instance_id": INSTANCE}).status_code == 403


def test_device_cannot_read_or_reply_to_another_session(manager):
    device, session = ready(manager)
    other = enrolled(manager, "other", "OTHER-PC")
    queued = manager.submit(session, "status", {}, "status-1")
    assert_code("receiver_session_mismatch", lambda: manager.reply(other["device_id"], other["device_token"], queued["command_id"], session, True, {}))
    assert_code("receiver_authentication_required", lambda: manager.reply(device["device_id"], "z" * 48, queued["command_id"], session, True, {}))


def test_duplicate_concurrent_poll_is_rejected_without_touching_queue(manager):
    device = enrolled(manager)
    async def exercise():
        first = asyncio.create_task(manager.poll(device["device_id"], device["device_token"], COMPUTER, INSTANCE, timeout_seconds=.3))
        await asyncio.sleep(.02)
        with pytest.raises(RemoteReceiverError) as error:
            await manager.poll(device["device_id"], device["device_token"], COMPUTER, INSTANCE, timeout_seconds=0)
        assert error.value.code == "receiver_poll_busy"
        return await first
    assert asyncio.run(exercise()) == {"kind": "idle"}


def test_queue_wakes_longpoll_without_blocking_event_loop(manager):
    device = enrolled(manager)
    async def exercise():
        pending = asyncio.create_task(manager.poll(device["device_id"], device["device_token"], COMPUTER, INSTANCE, timeout_seconds=1))
        await asyncio.sleep(.02)
        start = manager.start_session("second", "start-1")
        response = await pending
        assert response["command"]["command_id"] == start["command_id"]
    asyncio.run(exercise())


def test_lost_http_command_response_fences_without_replay(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-1")
    app = FastAPI()
    app.include_router(create_receiver_router(manager))
    body = json.dumps({"device_id": device["device_id"], "computer_name": COMPUTER,
                       "instance_id": INSTANCE}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "https", "path": "/remote-receiver/poll", "raw_path": b"/remote-receiver/poll",
             "query_string": b"", "root_path": "", "headers": [(b"authorization", ("Bearer " + device["device_token"]).encode()),
             (b"content-length", str(len(body)).encode()), (b"content-type", b"application/json")],
             "client": ("203.0.113.10", 1234), "server": ("receiver.example", 443)}
    delivered = False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("disconnected fake client")
    async def exercise():
        with pytest.raises(OSError):
            await app(scope, receive, send)
    asyncio.run(exercise())
    assert manager.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    assert manager.session_status(session)["state"] == "fenced"
    assert poll(manager, device) == {"kind": "idle"}


def test_receipt_deadline_fences_and_late_exact_reply_still_resolves(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "click", {"x": 2, "y": 2, "bounds": {"left": 0, "top": 0, "width": 100, "height": 100}, "button": "left", "count": 1}, "click-1")
    command = poll(manager, device)["command"]
    manager._db.execute("UPDATE commands SET dispatched_at=0 WHERE command_id=?", (issued["command_id"],))
    assert manager.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    reply(manager, device, command, {"foreground": TARGET, "remote_effect_verified": False})
    assert manager.command_status(session, issued["command_id"])["state"] == "completed"


def test_device_count_is_bounded_and_duplicate_enrollment_name_rejected(manager):
    for number in range(32):
        enrolled(manager, "device-" + str(number), "PC-" + str(number))
    assert_code("receiver_device_limit", lambda: manager.issue_enrollment("one-too-many", "ANOTHER-PC"))
    assert_code("receiver_device_exists", lambda: manager.issue_enrollment("DEVICE-0", "PC-0"))


def test_construction_without_runtime_recovery_keeps_live_dispatch_until_initialized(tmp_path):
    running = RemoteReceiverManager(tmp_path, ORIGIN)
    device, session = ready(running)
    issued = running.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-1")
    command = poll(running, device)["command"]
    observer = RemoteReceiverManager(tmp_path, ORIGIN, recover_on_startup=False)
    try:
        assert observer._db.execute("SELECT state FROM commands WHERE command_id=?", (issued["command_id"],)).fetchone()[0] == "dispatched"
        assert observer._db.execute("SELECT state FROM sessions WHERE session_id=?", (session,)).fetchone()[0] == "ready"
        observer.initialize_runtime()
        assert observer.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
        # A second call cannot reinterpret newly dispatched commands as a restart.
        reply(observer, device, command, {"foreground": TARGET})
        fresh = observer.submit(session, "key", {"keys": "TAB", "expect": EXPECT}, "key-2")
        poll(observer, device)
        observer.initialize_runtime()
        assert observer.command_status(session, fresh["command_id"])["state"] == "dispatched"
    finally:
        observer.close()
        running.close()


def test_configured_app_factory_does_not_recover_production_like_live_state(tmp_path):
    running = RemoteReceiverManager(tmp_path, ORIGIN)
    device, session = ready(running)
    issued = running.submit(session, "type", {"text": "once", "expect": EXPECT}, "type-1")
    poll(running, device)
    config = tmp_path / "receiver.json"
    config.write_text(json.dumps({"enabled": True, "public_origin": ORIGIN}), encoding="utf-8")
    from web_mcp.server import _configured_receiver_manager
    observer = _configured_receiver_manager(SimpleNamespace(project_root=tmp_path, data_root=tmp_path,
        extra={"remote_receiver_config": str(config)}), None)
    try:
        assert observer._runtime_initialized is False
        assert running.command_status(session, issued["command_id"])["state"] == "dispatched"
        observer.initialize_runtime()
        assert running.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    finally:
        observer.close()
        running.close()


@pytest.mark.parametrize("age,availability", [
    (None, "never_seen"), (0, "recently_seen"), (29, "recently_seen"),
    (45, "recently_seen"), (60, "recently_seen"), (90, "recently_seen"),
    (120, "recently_seen"), (121, "not_recently_seen"), (180, "not_recently_seen"),
])
def test_device_presentation_describes_periodic_recent_visits(manager, monkeypatch, age, availability):
    device = enrolled(manager)
    current = 2_000_000_000.0
    monkeypatch.setattr("web_tools.remote_receiver_server.time.time", lambda: current)
    manager._db.execute("UPDATE devices SET last_seen=? WHERE device_id=?",
                        (None if age is None else current - age, device["device_id"]))
    status = manager.devices()[0]
    assert status["availability"] == availability
    assert status["last_seen_age_seconds"] == age
    assert status["poll_interval_seconds"] == 60
    assert status["connection_mode"] == "periodic_poll"
    assert "online" not in status


@pytest.mark.parametrize("last_seen", [None, 0.0])
def test_start_session_queues_for_an_idle_or_not_recently_seen_device(manager, last_seen):
    device = enrolled(manager)
    manager._db.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (last_seen, device["device_id"]))
    start = manager.start_session("second", "start-when-idle")
    assert start["state"] == "queued" and start["session_state"] == "starting"


@pytest.mark.parametrize("code", [
    "capture_upload_failed", "invalid_capture_ticket", "capture_origin_mismatch",
    "helper_timeout", "helper_delivery_unknown", "network_unavailable", "server_rejected",
    "target_identity_mismatch", "interactive_desktop_required", "input_delivery_incomplete",
])
def test_known_remote_error_codes_are_preserved_without_remote_messages(manager, code):
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-error")
    command = poll(manager, device)["command"]
    remote_error = {"code": code, "message": "PRIVATE_GUI_TEXT " + device["device_token"],
                    "token": "PRIVATE_UPLOAD_TOKEN", "details": {"stdout": "PRIVATE_PROCESS_OUTPUT"}}
    acknowledgement = reply(manager, device, command, remote_error, ok=False)
    assert reply(manager, device, command, remote_error, ok=False) == acknowledgement
    status = manager.command_status(session, issued["command_id"])
    assert status["state"] == "failed" and status["error_code"] == status["error"]["code"] == code
    session_status = manager.session_status(session)
    assert session_status["state"] == "fenced" and session_status["error_code"] == code
    public_and_storage = json.dumps(session_status) + "\n" + "\n".join(manager._db.iterdump())
    for secret in ("PRIVATE_GUI_TEXT", device["device_token"], "PRIVATE_UPLOAD_TOKEN", "PRIVATE_PROCESS_OUTPUT"):
        assert secret not in public_and_storage


@pytest.mark.parametrize("code", [
    "private_token_abcdefghijklmnopqrstuvwx", "made_up_machine_error", "capture_network_token_secret",
    "capture_upload_failed_PRIVATE_TOKEN", "capture_upload_failed\nPRIVATE_TOKEN", None, 123,
])
def test_unknown_remote_codes_are_not_persisted_or_exposed(manager, code):
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "key-error")
    command = poll(manager, device)["command"]
    reply(manager, device, command, {"code": code, "message": "PRIVATE_NATIVE_EXCEPTION"}, ok=False)
    status = manager.command_status(session, issued["command_id"])
    assert status["error_code"] == status["error"]["code"] == "receiver_command_failed"
    assert manager.session_status(session)["error_code"] == "receiver_command_failed"
    stored = "\n".join(manager._db.iterdump())
    assert "PRIVATE_NATIVE_EXCEPTION" not in stored
    if isinstance(code, str):
        assert code not in stored


def test_capture_failure_keeps_known_code_without_changing_session_fence_rules(manager):
    device, session = ready(manager)
    capture_id = "c" * 32
    issued = manager.submit(session, "capture", {"capture_id": capture_id,
        "upload_url": ORIGIN + "/remote-gui/captures/" + capture_id, "token": "A" * 48}, "capture-error")
    command = poll(manager, device)["command"]
    reply(manager, device, command, {"code": "capture_upload_failed", "message": "PRIVATE_NETWORK_ERROR"}, ok=False)
    assert manager.command_status(session, issued["command_id"])["error_code"] == "capture_upload_failed"
    assert manager.session_status(session)["state"] == "ready"
    assert poll(manager, device) == {"kind": "idle"}


@pytest.mark.parametrize("code", [
    "capture_upload_http_401", "capture_upload_http_403", "capture_upload_http_404",
    "capture_upload_http_409", "capture_upload_http_413", "capture_upload_http_415",
    "capture_upload_http_429", "capture_upload_http_500", "capture_upload_http_502",
    "capture_upload_http_503", "capture_upload_http_504", "capture_upload_timeout",
    "capture_upload_tls_failed", "capture_upload_connection_failed", "capture_upload_dns_failed",
    "capture_upload_timeout_open", "capture_upload_timeout_write", "capture_upload_timeout_response",
    "capture_encoding_failed",
])
def test_receiver_upload_diagnostics_keep_only_new_fixed_codes(manager, code):
    device, session = ready(manager)
    issued = manager.submit(session, "status", {}, "diagnostic")
    command = poll(manager, device)["command"]
    reply(manager, device, command, {"code": code, "message": "PRIVATE_UPLOAD_EXCEPTION"}, ok=False)
    status = manager.command_status(session, issued["command_id"])
    assert status["error_code"] == status["error"]["code"] == code
    assert "PRIVATE_UPLOAD_EXCEPTION" not in json.dumps(status)


def test_receipt_window_covers_capture_upload_and_fences_only_after_ninety_seconds(manager, monkeypatch):
    device, session = ready(manager)
    issued = manager.submit(session, "key", {"keys": "ENTER", "expect": EXPECT}, "deadline-key")
    poll(manager, device)
    dispatched = manager._db.execute("SELECT dispatched_at FROM commands WHERE command_id=?", (issued["command_id"],)).fetchone()[0]
    assert COMMAND_RECEIPT_SECONDS == 90
    monkeypatch.setattr("web_tools.remote_receiver_server.time.time", lambda: dispatched + 89)
    assert manager.command_status(session, issued["command_id"])["state"] == "dispatched"
    monkeypatch.setattr("web_tools.remote_receiver_server.time.time", lambda: dispatched + 91)
    assert manager.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    assert manager.session_status(session)["state"] == "fenced"


def test_new_receiver_instance_queues_one_distinct_cleanup_stop_and_requires_real_receipt(manager):
    device, session = ready(manager)
    old_stop = manager.close_session(session, "close-original")
    assert poll(manager, device)["command"]["command_id"] == old_stop["command_id"]
    replacement = poll(manager, device, "c" * 32)["command"]
    assert replacement["action"] == "stop" and replacement["arguments"] == {}
    assert replacement["command_id"] != old_stop["command_id"]
    assert replacement["session_id"] == session
    assert manager.command_status(session, old_stop["command_id"])["state"] == "outcome_unknown"
    assert manager.session_status(session)["state"] == "closing"
    # Without a receipt a repeated poll must neither declare closure nor
    # redeliver either stop, including the replacement cleanup command.
    assert poll(manager, device, "c" * 32) == {"kind": "idle"}
    assert manager.session_status(session)["state"] == "closing"
    assert manager._db.execute("SELECT COUNT(*) FROM commands WHERE session_id=? AND action='stop'", (session,)).fetchone()[0] == 2
    assert manager._db.execute("SELECT COUNT(*) FROM operations WHERE scope=? AND operation_id=?",
        ("session:" + session, "internal-instance-stop:" + "c" * 32)).fetchone()[0] == 1
    reply(manager, device, replacement, {"helper_stopped": True, "receiver_exiting": True})
    assert manager.session_status(session)["state"] == "closed"
    assert manager.command_status(session, old_stop["command_id"])["state"] == "outcome_unknown"
    assert manager.command_status(session, replacement["command_id"])["state"] == "completed"


def test_instance_change_dispatches_existing_queued_stop_without_creating_another(manager):
    device, session = ready(manager)
    stop = manager.close_session(session, "close-original")
    command = poll(manager, device, "c" * 32)["command"]
    assert command["command_id"] == stop["command_id"] and command["action"] == "stop"
    assert manager._db.execute("SELECT COUNT(*) FROM commands WHERE session_id=? AND action='stop'", (session,)).fetchone()[0] == 1
    assert manager.session_status(session)["state"] == "closing"


def test_new_instance_reconciles_a_persisted_unknown_stop_only_with_new_shutdown_receipt(tmp_path):
    original = RemoteReceiverManager(tmp_path, ORIGIN)
    device, session = ready(original)
    old_stop = original.close_session(session, "close-original")
    poll(original, device)
    original.close()
    replacement = RemoteReceiverManager(tmp_path, ORIGIN)
    try:
        assert replacement.command_status(session, old_stop["command_id"])["state"] == "outcome_unknown"
        assert replacement.session_status(session)["state"] == "closing"
        command = poll(replacement, device, "c" * 32)["command"]
        assert command["action"] == "stop" and command["command_id"] != old_stop["command_id"]
        assert replacement.session_status(session)["state"] == "closing"
        reply(replacement, device, command, {"helper_stopped": True, "receiver_exiting": True})
        assert replacement.session_status(session)["state"] == "closed"
        assert replacement.command_status(session, old_stop["command_id"])["state"] == "outcome_unknown"
    finally:
        replacement.close()


def test_instance_cleanup_does_not_replay_unknown_input_or_create_stop_for_fenced_session(manager):
    device, session = ready(manager)
    issued = manager.submit(session, "type", {"text": "only once", "expect": EXPECT}, "input-once")
    poll(manager, device)
    assert poll(manager, device, "c" * 32) == {"kind": "idle"}
    assert poll(manager, device, "d" * 32) == {"kind": "idle"}
    assert manager.command_status(session, issued["command_id"])["state"] == "outcome_unknown"
    assert manager.session_status(session)["state"] == "fenced"
    assert manager._db.execute("SELECT COUNT(*) FROM commands WHERE session_id=? AND action IN ('type','stop')", (session,)).fetchone()[0] == 1
