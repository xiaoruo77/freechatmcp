"""Keyboard endpoint state and capture/target pairing, without GUI or input."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from web_tools.background_uu import UUBackgroundError, UUBackgroundSession
from web_tools.uu_remote_keyboard import RemoteKeyboardError


def _foreground(hwnd=101, pid=202, created="638000000000000000", **extra):
    return {"available": True, "hwnd": hwnd, "pid": pid,
            "creation_time_utc_ticks": created, "session_id": 1, **extra}


class Endpoint:
    def __init__(self, error=None, records=None, cleanup=None, reply=None):
        self.error = error
        self._requests = {} if records is None else records
        self.cleanup = {"cleanup_complete": True} if cleanup is None else cleanup
        self.reply = {"ok": True, "result": {"foreground": _foreground()}} if reply is None else reply
        self.calls = []
        self.closed = 0
        self.started = 0

    def request(self, action, args, **kwargs):
        assert action == "status" and args == {}, "Recovery must only probe status"
        self.calls.append((action, deepcopy(args)))
        if self.error:
            # Like the real controller, the failed read can retain a pending
            # read receipt. This must not be mistaken for a write.
            self._requests["health-probe"] = {"action": "status", "reply": None}
            raise RemoteKeyboardError(self.error, "test endpoint state", may_have_run=True,
                                      request_id="health-probe", stage="request_status")
        return deepcopy(self.reply)

    def start(self, **kwargs):
        self.started += 1
        return {"computer": "REMOTE_TEST", "session_id": 1}

    def close(self):
        self.closed += 1
        return deepcopy(self.cleanup)


@pytest.fixture
def session(tmp_path):
    value = UUBackgroundSession(tmp_path, tmp_path / "GameViewer.exe", lambda: [], lambda: {})
    value._viewer = lambda: 123
    value._validate_window = lambda hwnd: {"hwnd": hwnd, "pid": 456}
    value._connected = True
    value._target_id = "test-device-identifier"
    value._target_name = "Test remote device"
    return value


def _factory(session, replacement):
    calls = []
    session._keyboard_factory = lambda cli, device: calls.append((cli, device)) or replacement
    return calls


def test_initial_start_uses_factory_once_and_no_status_input(session):
    endpoint = Endpoint()
    factory = _factory(session, endpoint)
    session._ensure_keyboard()
    assert len(factory) == 1 and factory[0][1] == "test-device-identifier"
    assert endpoint.started == 1 and endpoint.calls == []
    assert session._keyboard_ready is True


def test_healthy_endpoint_is_only_read_and_not_replaced(session):
    endpoint = Endpoint(records={"old-write": {"action": "type", "reply": None}})
    replacement = Endpoint()
    factory = _factory(session, replacement)
    session._keyboard = endpoint
    session._keyboard_ready = True
    session._ensure_keyboard()
    assert endpoint.calls == [("status", {})]
    assert endpoint.closed == 0 and factory == []
    assert endpoint._requests["old-write"]["reply"] is None


@pytest.mark.parametrize("code", ["uu_remote_helper_stopped", "uu_terminal_exited", "uu_remote_request_limit"])
def test_dead_idle_or_exhausted_endpoint_is_confirmed_closed_then_rebuilt(session, code):
    endpoint = Endpoint(error=code, records={"read-old": {"action": "status", "reply": None},
        "written": {"action": "key", "reply": {"ok": True, "result": {"inserted_events": 4}}}})
    replacement = Endpoint()
    factory = _factory(session, replacement)
    session._keyboard = endpoint
    session._keyboard_ready = True
    session._last_remote_target = _foreground()
    session._ensure_keyboard()
    assert endpoint.closed == 1 and len(factory) == 1
    assert replacement.started == 1 and session._keyboard is replacement
    assert session._keyboard_ready is True and session._last_remote_target is None
    assert session._last_keyboard_cleanup == {"cleanup_complete": True}


@pytest.mark.parametrize("record", [
    {"action": "type", "reply": None},
    {"action": "key", "reply": None},
    {"digest": "old-format", "reply": None},
    {"digest": "old-format", "reply": {"ok": True}},
    {"action": "type", "reply": {"ok": False, "error": {"code": "input_delivery_incomplete"}}},
    {"action": "key", "reply": {"ok": False, "error": {"code": "remote_input_failed"}}},
    {"action": "key", "reply": {"ok": True}, "unknown_outcome": True},
    {"action": "unclassified_operation", "reply": None},
])
def test_unknown_write_or_legacy_evidence_prevents_automatic_teardown(session, record):
    endpoint = Endpoint(error="uu_remote_helper_stopped", records={"prior-write": deepcopy(record)})
    factory = _factory(session, Endpoint())
    session._keyboard = endpoint
    session._keyboard_ready = True
    with pytest.raises(UUBackgroundError) as caught:
        session._ensure_keyboard()
    assert caught.value.code == "uu_remote_keyboard_outcome_unknown"
    assert caught.value.may_have_run is True
    assert endpoint.closed == 0 and factory == []
    assert endpoint._requests["prior-write"] == record
    assert session._keyboard is endpoint and session._keyboard_ready is False
    # A later call retains the same evidence and still cannot replace it.
    with pytest.raises(UUBackgroundError, match="previous keyboard write"):
        session._ensure_keyboard()
    assert endpoint.closed == 0 and factory == []


def test_proven_rejected_write_can_be_retired_without_replay(session):
    endpoint = Endpoint(error="uu_terminal_exited", records={"rejected": {
        "action": "type", "reply": {"ok": False, "error": {"code": "target_not_foreground"}}}})
    replacement = Endpoint()
    _factory(session, replacement)
    session._keyboard = endpoint
    session._keyboard_ready = True
    session._ensure_keyboard()
    assert endpoint.closed == 1 and replacement.started == 1
    assert endpoint.calls == [("status", {})]


def test_incomplete_cleanup_preserves_old_endpoint_and_can_be_rechecked(session):
    endpoint = Endpoint(error="uu_terminal_exited", cleanup={"cleanup_complete": False})
    replacement = Endpoint()
    factory = _factory(session, replacement)
    session._keyboard = endpoint
    session._keyboard_ready = True
    with pytest.raises(UUBackgroundError) as caught:
        session._ensure_keyboard()
    assert caught.value.code == "uu_remote_keyboard_cleanup_pending"
    assert session._keyboard is endpoint and factory == []
    endpoint.cleanup = {"cleanup_complete": True}
    session._ensure_keyboard()
    assert endpoint.closed == 2 and len(factory) == 1 and replacement.started == 1


def test_unconfirmed_status_timeout_never_rebuilds_or_replays(session):
    endpoint = Endpoint(error="uu_remote_input_timeout")
    factory = _factory(session, Endpoint())
    session._keyboard = endpoint
    session._keyboard_ready = True
    with pytest.raises(UUBackgroundError) as caught:
        session._ensure_keyboard()
    assert caught.value.code == "uu_remote_input_timeout"
    assert caught.value.may_have_run is False  # This failed probe was only a read.
    assert endpoint.closed == 0 and factory == []


def test_runtime_status_refusal_is_not_treated_as_a_dead_endpoint(session):
    endpoint = Endpoint(reply={"ok": False, "error": {"code": "interactive_desktop_required", "message": "locked"}})
    factory = _factory(session, Endpoint())
    session._keyboard = endpoint
    session._keyboard_ready = True
    with pytest.raises(UUBackgroundError) as caught:
        session._ensure_keyboard()
    assert caught.value.code == "interactive_desktop_required"
    assert endpoint.closed == 0 and factory == []


def test_read_only_keyboard_status_does_not_create_an_endpoint(session):
    factory = _factory(session, Endpoint())
    result = session.perform("keyboard_status")
    assert result["ok"] and result["keyboard_ready"] is False
    assert factory == [] and session._keyboard is None


@pytest.fixture
def capture_probe(session, monkeypatch):
    events = []
    session._keyboard_ready = True
    session._last_remote_target = _foreground(hwnd=999)

    @contextmanager
    def dpi():
        events.append("dpi-enter")
        try:
            yield
        finally:
            events.append("dpi-exit")

    def capture(hwnd, path):
        events.append("capture")
        return {"path": str(path), "width": 800, "height": 600}

    monkeypatch.setattr("web_tools.background_capture.capture_window", capture)
    monkeypatch.setattr("web_tools.uu_window_transport.dpi_context", dpi)
    return session, events


def _statuses(session, events, values):
    values = iter(values)

    def request(action, args):
        assert action == "status" and args == {}
        events.append("status")
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return {"foreground": deepcopy(value)}

    session._keyboard_request = request


def test_capture_publishes_only_identity_stable_before_and_after(capture_probe):
    session, events = capture_probe
    target = _foreground()
    _statuses(session, events, [target, target])
    result = session._screenshot(123)
    assert events == ["status", "dpi-enter", "capture", "dpi-exit", "status"]
    assert result["remote_foreground"] == target
    assert session._last_remote_target == target


@pytest.mark.parametrize("after", [
    _foreground(hwnd=102), _foreground(pid=203), _foreground(created="638000000000000001"),
    {"available": False},
])
def test_capture_does_not_bind_old_image_to_changed_target(capture_probe, after):
    session, events = capture_probe
    _statuses(session, events, [_foreground(), after])
    result = session._screenshot(123)
    assert "capture" in result and result["target_changed"] is True
    assert result["keyboard_status_error"]["code"] == "uu_remote_target_changed"
    assert "remote_foreground" not in result and session._last_remote_target is None


def test_capture_without_any_foreground_returns_image_without_target(capture_probe):
    session, events = capture_probe
    _statuses(session, events, [{"available": False}, {"available": False}])
    result = session._screenshot(123)
    assert "capture" in result and "remote_foreground" not in result
    assert result["keyboard_status_error"]["code"] == "uu_remote_foreground_unavailable"
    assert session._last_remote_target is None


@pytest.mark.parametrize("failure_position", ["before", "after"])
def test_capture_status_failure_does_not_leave_cached_target(capture_probe, failure_position):
    session, events = capture_probe
    error = UUBackgroundError("uu_remote_input_timeout", "read acknowledgement missing")
    values = [error] if failure_position == "before" else [_foreground(), error]
    _statuses(session, events, values)
    result = session._screenshot(123)
    assert "capture" in result and "remote_foreground" not in result
    assert result["keyboard_status_error"]["code"] == "uu_remote_input_timeout"
    assert session._last_remote_target is None


def test_capture_observes_stopped_endpoint_without_starting_it(capture_probe):
    session, events = capture_probe
    factory = _factory(session, Endpoint())
    _statuses(session, events, [UUBackgroundError("uu_remote_helper_stopped", "idle exit")])
    result = session._screenshot(123)
    assert "capture" in result and result["capabilities"]["remote_keyboard"] is False
    assert session._keyboard_recovery_needed is True
    assert factory == [] and session._last_remote_target is None


def test_no_keyboard_capture_preserves_existing_no_factory_behavior(capture_probe):
    session, events = capture_probe
    session._keyboard_ready = False
    session._keyboard_request = lambda *a: pytest.fail("must not start/read keyboard")
    result = session._screenshot(123)
    assert "capture" in result and "remote_foreground" not in result
    assert events == ["dpi-enter", "capture", "dpi-exit"]
    assert session._last_remote_target is None


def test_failed_window_validation_invalidates_previous_keyboard_target(capture_probe):
    session, events = capture_probe

    def invalid(hwnd):
        raise UUBackgroundError("uu_foreign_window", "stale private viewer")

    session._validate_window = invalid
    with pytest.raises(UUBackgroundError) as caught:
        session._screenshot(123)
    assert caught.value.code == "uu_foreign_window"
    assert session._last_remote_target is None and events == []


def test_failed_capture_invalidates_previous_keyboard_target(capture_probe, monkeypatch):
    session, events = capture_probe
    _statuses(session, events, [_foreground()])

    def failed(*args):
        raise RuntimeError("private window capture unavailable")

    monkeypatch.setattr("web_tools.background_capture.capture_window", failed)
    with pytest.raises(UUBackgroundError) as caught:
        session._screenshot(123)
    assert caught.value.code == "uu_capture_unavailable"
    assert session._last_remote_target is None
