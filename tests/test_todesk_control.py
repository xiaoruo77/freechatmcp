"""Session tests use injected native/capture fakes and never touch real UI."""
from copy import deepcopy
import json
import struct
import zlib

import pytest

from web_tools.todesk_control import ToDeskSession
from web_tools.todesk_native import NativeToDeskError


SECRET = "never-emit-a-password-or-config"


class FakeNative:
    def __init__(self):
        self.record = {"hwnd": 101, "pid": 202, "create_time": 123.5,
                       "title": "DEMO-NODE", "viewer": True,
                       "client_width": 120, "client_height": 80,
                       "desktop": "Default", "exe": "ToDesk.exe"}
        self.messages = []
        self.connections = []
        self.verify_error = None
        self.input_result = {"ok": True}
        self.input_error = None
        self.closed = True

    def inventory(self):
        return [deepcopy(self.record), {"title": SECRET, "viewer": False}]

    def verify(self, record):
        if self.verify_error:
            raise self.verify_error
        if (record["hwnd"], record["pid"], record["create_time"]) != (self.record["hwnd"], self.record["pid"], self.record["create_time"]):
            raise NativeToDeskError("process_identity_mismatch", SECRET)
        return deepcopy(self.record)

    def connect(self, name):
        self.connections.append(name)
        return deepcopy(self.record)

    def perform(self, record, action, args):
        self.messages.append((deepcopy(record), action, deepcopy(args)))
        if action == "close":
            return {"ok": True, "closed": self.closed}
        if self.input_error:
            raise self.input_error
        return deepcopy(self.input_result)


def _png(width, height):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff)
    pixels = b"\x00" + b"\x20\x40\x60" * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels * height)) + chunk(b"IEND", b""))


class FakeCapture:
    def __init__(self):
        self.calls = []
        self.extra = {}

    def __call__(self, record, path, *, verifier):
        self.calls.append((deepcopy(record), path))
        verifier(record)
        width, height = record["client_width"] + 16, record["client_height"] + 40
        path.write_bytes(_png(width, height))
        verifier(record)
        return {"path": str(path), "width": width, "height": height,
                "client_origin": {"x": 8, "y": 32},
                "client_width": record["client_width"], "client_height": record["client_height"],
                "password": SECRET, **self.extra}


@pytest.fixture
def setup(tmp_path):
    native, capture = FakeNative(), FakeCapture()
    session = ToDeskSession(tmp_path, tmp_path / "ToDesk.exe",
                           {"DEMO-NODE": {"device_id": "282828282", "password": SECRET}},
                           native=native, capture=capture)
    return session, native, capture


def ready(setup):
    session, native, capture = setup
    assert session.perform("connect", {"device_name": "DEMO-NODE"})["verified"]
    assert session.perform("screenshot")["ok"]
    return session, native, capture


def test_import_and_status_do_not_create_native_backend(setup):
    session, native, _ = setup
    status = session.perform("status")
    assert status["ok"] and not status["attached"] and not status["verified"]
    assert status["supported_actions"] == []
    assert native.connections == [] and native.messages == []


def test_devices_contains_only_safe_configured_names(setup):
    session, native, _ = setup
    result = session.perform("devices")
    assert result["devices"] == [{"device_name": "DEMO-NODE", "configured": True}]
    assert result["verified_viewer_count"] == 1
    assert not result["automatic_connection_supported"]
    assert SECRET not in json.dumps(result) and "282828282" not in json.dumps(result)
    assert native.connections == []


@pytest.mark.parametrize("action,args", [
    ("connect", {"device_name": "282828282"}),
    ("connect", {"device_name": "other"}),
    ("connect", {"device_name": SECRET, "password": SECRET}),
    ("connect", {"device_name": []}),
    ("screenshot", {"path": SECRET}),
    ("status", {"password": SECRET}),
    ("unsupported" + SECRET, {}),
    (None, {}),
    ("click", []),
])
def test_invalid_public_request_has_no_effect_or_private_echo(setup, action, args):
    session, native, capture = setup
    result = session.perform(action, args)
    assert not result["ok"] and SECRET not in json.dumps(result)
    assert native.connections == [] and native.messages == [] and capture.calls == []


def test_explicit_connect_is_idempotent_and_owns_exact_viewer(setup):
    session, native, _ = setup
    first = session.perform("connect", {"device_name": "DEMO-NODE"})
    second = session.perform("connect", {"device_name": "DEMO-NODE"})
    assert first["session_id"] == second["session_id"]
    assert first["created_by_us"] is False
    assert first["window"]["hwnd"] == 101
    assert native.connections == ["DEMO-NODE"]
    assert "click" not in first["supported_actions"]


def test_capture_contract_and_sensitive_backend_metadata_filtering(setup):
    session, _, capture = ready(setup)
    result = session.perform("screenshot")
    cap = result["capture"]
    assert result["mode"] == "todesk_window_control"
    assert cap["width"] == 136 and cap["height"] == 120
    assert cap["client_origin"] == {"x": 8, "y": 32}
    assert cap["client_bounds"] == {"left": 8, "top": 32, "right": 128, "bottom": 112}
    assert cap["mime_type"] == "image/png" and len(cap["sha256"]) == 64
    assert str(capture.calls[-1][1].parent).replace(chr(92), "/").endswith("background_desktop/captures")
    assert SECRET not in json.dumps(result)
    assert session.perform("status")["screenshot_fresh"]


@pytest.mark.parametrize("action,args,expected", [
    ("click", {"x": 8, "y": 32}, {"x": 0, "y": 0, "button": "left", "count": 1}),
    ("click", {"x": 127, "y": 111, "button": "right", "count": 2}, {"x": 119, "y": 79, "button": "right", "count": 2}),
    ("type", {"text": "你好\n世界"}, {"text": "你好\n世界"}),
    ("key", {"keys": "enter"}, {"keys": "ENTER"}),
    ("key", {"keys": "F24"}, {"keys": "F24"}),
    ("scroll", {"x": 30, "y": 50, "vertical": -240}, {"x": 22, "y": 18, "vertical": -240, "horizontal": 0}),
    ("drag", {"x": 8, "y": 32, "to_x": 127, "to_y": 111}, {"x": 0, "y": 0, "to_x": 119, "to_y": 79, "duration_ms": 250}),
])
def test_input_coordinates_and_schemas(setup, action, args, expected):
    session, native, _ = ready(setup)
    result = session.perform(action, {"operation_id": "once", **args})
    assert result["ok"] and not result["remote_effect_verified"]
    assert native.messages[-1][1:] == (action, expected)


@pytest.mark.parametrize("action,args", [
    ("click", {"x": True, "y": 40}),
    ("click", {"x": 20, "y": float("nan")}),
    ("click", {"x": 20, "y": 40, "button": "keyboard"}),
    ("click", {"x": 20, "y": 40, "count": 3}),
    ("click", {"x": 20, "y": 40, "password": SECRET}),
    ("type", {"text": "a" * 16385}),
    ("type", {"text": "\x00"}),
    ("type", {"text": "\ud800"}),
    ("key", {"keys": "CTRL+A"}),
    ("key", {"keys": []}),
    ("key", {"keys": "unsupported"}),
    ("scroll", {"x": 20, "y": 40, "vertical": 1}),
    ("scroll", {"x": 20, "y": 40}),
    ("drag", {"x": 20, "y": 40, "to_x": 21, "to_y": 41, "duration_ms": 5001}),
    ("drag", {"x": 20, "y": 40, "to_x": 21}),
])
def test_invalid_input_is_rejected_before_native_dispatch(setup, action, args):
    session, native, _ = ready(setup)
    result = session.perform(action, {"operation_id": "invalid", **args})
    assert not result["ok"] and native.messages == []
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("args", [{"x": 7, "y": 40}, {"x": 20, "y": 31}, {"x": 128, "y": 40}, {"x": 20, "y": 112}])
def test_window_decoration_and_border_are_not_input_coordinates(setup, args):
    session, native, _ = ready(setup)
    result = session.perform("click", {"operation_id": "border", **args})
    assert result["error_code"] == "coordinate_outside_client" and native.messages == []


def test_stable_operation_id_is_at_most_once_and_conflict_rejected(setup):
    session, native, _ = ready(setup)
    args = {"operation_id": "one", "x": 20, "y": 40}
    first = session.perform("click", args)
    assert session.perform("click", args) == first
    assert session.perform("click", {**args, "x": 21})["error_code"] == "operation_id_conflict"
    assert len(native.messages) == 1
    # Public receipts are copies, so the caller cannot change cache state.
    first["ok"] = False
    assert session.perform("click", args)["ok"]


def test_fresh_screenshot_and_unchanged_geometry_are_required(setup, monkeypatch):
    session, native, _ = setup
    assert session.perform("connect", {"device_name": "DEMO-NODE"})["ok"]
    args = {"operation_id": "no-image", "x": 20, "y": 40}
    assert session.perform("click", args)["error_code"] == "screenshot_required"
    monkeypatch.setattr("web_tools.todesk_control.time.monotonic", lambda: 100.0)
    assert session.perform("screenshot")["ok"]
    monkeypatch.setattr("web_tools.todesk_control.time.monotonic", lambda: 161.0)
    assert session.perform("click", {**args, "operation_id": "expired"})["error_code"] == "screenshot_stale"
    assert session.perform("screenshot")["ok"]
    native.record["client_width"] += 1
    assert session.perform("click", {**args, "operation_id": "resized"})["error_code"] == "viewer_geometry_changed"
    assert native.messages == []


def test_changed_exact_identity_is_not_reselected_from_inventory(setup):
    session, native, _ = ready(setup)
    native.record["create_time"] += 1
    result = session.perform("click", {"operation_id": "reuse", "x": 20, "y": 40})
    assert not result["ok"] and not result["action_may_have_run"]
    status = session.perform("status")
    assert status["attached"] and not status["verified"] and not status["supported_actions"]
    assert native.messages == []


@pytest.mark.parametrize("kind", ["partial", "exception", "explicit_unknown"])
def test_unknown_delivery_fences_new_operation_ids_even_after_capture(setup, kind):
    session, native, _ = ready(setup)
    if kind == "partial":
        native.input_result = {"ok": False, "status": "delivery_unknown", "may_have_run": True, "retry_safe": False}
    elif kind == "exception":
        native.input_error = RuntimeError(SECRET)
    else:
        native.input_error = NativeToDeskError("message_delivery_unknown", SECRET, may_have_run=True)
    args = {"operation_id": "unknown", "x": 20, "y": 40}
    first = session.perform("click", args)
    assert first["error_code"] == "input_delivery_unknown" and first["action_may_have_run"]
    assert session.perform("click", args) == first
    native.input_error, native.input_result = None, {"ok": True}
    assert session.perform("screenshot")["input_blocked"]
    assert session.perform("click", {**args, "operation_id": "bypass"})["error_code"] == "input_fenced"
    assert len(native.messages) == 1 and SECRET not in json.dumps(first)


def test_known_pre_dispatch_failure_does_not_fence(setup):
    session, native, _ = ready(setup)
    native.input_error = NativeToDeskError("point_outside_canvas", SECRET)
    result = session.perform("click", {"operation_id": "rejected", "x": 20, "y": 40})
    assert result["error_code"] == "point_outside_canvas" and not result["action_may_have_run"]
    assert not session.perform("status")["input_blocked"]


def test_close_only_attached_viewer_and_preserve_on_unconfirmed_close(setup):
    session, native, _ = ready(setup)
    original = deepcopy(native.record)
    native.closed = False
    result = session.perform("disconnect")
    assert not result["ok"] and not result["cleanup_complete"]
    assert session.perform("status")["attached"]
    assert native.messages[-1] == (original, "close", {})
    native.closed = True
    assert session.perform("close")["cleanup_complete"]
    assert not session.perform("status")["attached"]
    assert session.perform("close")["cleanup_complete"]
    assert len(native.messages) == 2


def test_only_confirmed_close_and_new_session_clears_fence_but_keeps_receipts(setup):
    session, native, _ = ready(setup)
    old_session = session.perform("status")["session_id"]
    args = {"operation_id": "unknown", "x": 20, "y": 40}
    native.input_result = {"ok": False}
    first = session.perform("click", args)
    assert session.perform("disconnect")["cleanup_complete"]
    native.input_result = {"ok": True}
    assert session.perform("connect", {"device_name": "DEMO-NODE"})["session_id"] != old_session
    assert session.perform("screenshot")["ok"]
    assert session.perform("click", args) == first
    assert session.perform("click", {**args, "operation_id": "new-session"})["ok"]
    assert len(native.messages) == 3


def test_cache_never_evicts_old_receipts(setup):
    session, native, _ = ready(setup)
    session.MAX_RECEIPTS = 1
    first = session.perform("click", {"operation_id": "first", "x": 20, "y": 40})
    assert session.perform("click", {"operation_id": "second", "x": 20, "y": 40})["error_code"] == "operation_cache_full"
    assert session.perform("click", {"operation_id": "first", "x": 20, "y": 40}) == first
    assert len(native.messages) == 1


def test_invalid_capture_metadata_is_not_returned_or_usable(setup):
    session, native, capture = setup
    assert session.perform("connect", {"device_name": "DEMO-NODE"})["ok"]
    capture.extra["client_origin"] = {"x": -1, "y": 32}
    result = session.perform("screenshot")
    assert result["error_code"] == "capture_invalid" and "capture" not in result
    assert not capture.calls[-1][1].exists()
    assert session.perform("click", {"operation_id": "invalid-capture", "x": 20, "y": 40})["error_code"] == "screenshot_required"
    assert native.messages == []


def test_lazy_real_native_constructor_and_dispatch_contract(tmp_path, monkeypatch):
    """Exercise the real NativeToDesk with a fake Win32 seam, including Path conversion."""
    from web_tools import todesk_native

    executable = tmp_path / "ToDesk.exe"

    class API:
        def __init__(self):
            self.messages = []

        def desktop_windows(self, desktop):
            assert desktop == "Default"
            return [101]

        def exists(self, hwnd):
            return hwnd in {101, 102}

        def root(self, hwnd):
            return 101

        def pid(self, hwnd):
            return 202

        def process(self, pid):
            return {"exe": str(executable), "create_time": 123.5}

        def title(self, hwnd):
            return "DEMO-NODE"

        def class_name(self, hwnd):
            return "SciterVideoClass" if hwnd == 102 else "Viewer"

        def client_size(self, hwnd):
            return 120, 80

        def is_iconic(self, hwnd):
            return False

        def children(self, root):
            return [102]

        def is_child(self, root, hwnd):
            return root == 101 and hwnd == 102

        def map_point(self, source, target, x, y):
            return x, y

        def send(self, hwnd, message, wparam, lparam, timeout):
            self.messages.append((hwnd, message, wparam, lparam))

    api = API()
    monkeypatch.setattr(todesk_native, "_Win32", lambda: api)
    session = ToDeskSession(tmp_path, executable, {"DEMO-NODE": {"device_id": "282828282"}},
                           capture=FakeCapture())
    assert session.perform("devices")["verified_viewer_count"] == 1
    assert session.perform("connect", {"device_name": "DEMO-NODE"})["verified"]
    assert session.perform("screenshot")["ok"]
    result = session.perform("click", {"operation_id": "native-contract", "x": 28, "y": 52})
    assert result["ok"] and not result["remote_effect_verified"]
    assert len(api.messages) == 3 and all(message[0] == 102 for message in api.messages)
    assert session.perform("click", {"operation_id": "native-contract", "x": 28, "y": 52}) == result
    assert len(api.messages) == 3
