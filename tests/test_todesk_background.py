"""Strict background guardrails are exercised only with in-memory fakes."""
from copy import deepcopy

import pytest

from web_tools.todesk_background import StrictBackgroundToDesk
from web_tools.todesk_native import NativeToDeskError, WM_CLOSE


EXE = r"D:\Apps\ToDesk\ToDesk.exe"


class ForbiddenConnector:
    def connect(self, name):
        raise AssertionError("Strict background mode must never invoke a connector")


class FakeAPI:
    def __init__(self):
        self.windows = {
            10: {"title": "ToDesk", "pid": 77, "root": 10},
            20: {"title": "DEMO-NODE", "pid": 77, "root": 20},
        }
        self.creation_time = 123.25
        self.minimized = set()
        self.sent = []

    def desktop_windows(self, desktop):
        assert desktop == "Default"
        return list(self.windows)

    def exists(self, hwnd):
        return hwnd in self.windows

    def root(self, hwnd):
        return self.windows[hwnd]["root"]

    def pid(self, hwnd):
        return self.windows[hwnd]["pid"]

    def process(self, pid):
        assert pid == 77
        return {"exe": EXE, "create_time": self.creation_time}

    def client_size(self, hwnd):
        return (209, 33) if hwnd in self.minimized else (1200, 800)

    def title(self, hwnd):
        return self.windows[hwnd]["title"]

    def class_name(self, hwnd):
        return "H-SMILE-FRAME"

    def visible(self, hwnd):
        return True

    def is_iconic(self, hwnd):
        return hwnd in self.minimized

    def restore_background(self, hwnd):
        raise AssertionError("Strict background mode must never show/restore a window")

    def foreground(self):
        raise AssertionError("Strict background mode must never run activation logic")

    def send(self, hwnd, message, wparam, lparam, timeout_ms):
        assert message == WM_CLOSE, "Strict background mode must never dispatch input/activation"
        self.sent.append((hwnd, message, wparam, lparam))
        self.windows.pop(hwnd)


@pytest.fixture
def backend():
    api = FakeAPI()
    native = StrictBackgroundToDesk(EXE, {"DEMO-NODE": {"device_id": "123456789"}},
                                   _api=api, _connector=ForbiddenConnector())
    return native, api


def test_reports_only_verified_background_capabilities(backend):
    native, _ = backend
    assert native.capabilities() == {"background_only": True, "input_verified": False, "auto_connect_verified": False}


def test_attach_existing_viewer_never_connects_restores_or_activates(backend):
    native, api = backend
    # Even a mistakenly enabled experimental parent option cannot open its
    # activation path through this production guard.
    native.message_only_activation = True
    record = native.connect("demo-node")
    assert record["hwnd"] == 20
    assert record["attached_existing"] is True
    assert record["background_only"] is True
    assert record["host_input_used"] is False
    assert api.sent == []


def test_missing_viewer_is_unverified_without_attempting_connection(backend):
    native, api = backend
    api.windows.pop(20)
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "background_connection_unverified"
    assert exc.value.may_have_run is False
    assert api.sent == []


def test_minimized_viewer_is_rejected_without_restore(backend):
    native, api = backend
    api.minimized.add(20)
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "background_viewer_minimized"
    assert exc.value.may_have_run is False
    assert api.sent == []


def test_multiple_existing_viewers_are_not_guessed(backend):
    native, api = backend
    api.windows[30] = {**api.windows[20], "root": 30}
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "ambiguous_viewer"
    assert api.sent == []


def test_unconfigured_device_is_rejected(backend):
    native, api = backend
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("OTHER")
    assert exc.value.code == "device_not_allowed"
    assert api.sent == []


@pytest.mark.parametrize("action,args", [
    ("click", {"x": 30, "y": 100}), ("type", {"text": "test"}),
    ("key", {"keys": "Enter"}), ("scroll", {"x": 30, "y": 100, "vertical": 120}),
    ("drag", {"x": 30, "y": 100, "to_x": 50, "to_y": 120}),
])
def test_all_unverified_input_is_blocked_before_any_dispatch(backend, action, args):
    native, api = backend
    native.message_only_activation = True
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(native.connect("DEMO-NODE"), action, args)
    assert exc.value.code == "background_input_unverified"
    assert exc.value.may_have_run is False
    assert api.sent == []


def test_close_only_closes_exact_viewer_even_if_parent_activation_option_enabled(backend):
    native, api = backend
    native.message_only_activation = True
    record = native.connect("DEMO-NODE")
    result = native.perform(record, "close")
    assert result["closed"] is True
    assert api.sent == [(20, WM_CLOSE, 0, 0)]
    assert 10 in api.windows


def test_close_rejects_stale_identity_and_keeps_shared_gui(backend):
    native, api = backend
    record = native.connect("DEMO-NODE")
    # A changed title on the same process cannot authorize closing the main UI.
    api.windows[20]["title"] = "ToDesk"
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(record, "close")
    assert exc.value.code == "viewer_mismatch"
    assert 10 in api.windows and 20 in api.windows
    assert api.sent == []


def test_close_already_missing_original_viewer_is_a_readonly_proof(backend):
    native, api = backend
    record = deepcopy(native.connect("DEMO-NODE"))
    api.windows.pop(20)
    result = native.perform(record, "close")
    assert result["closed"] is True
    assert api.sent == []
