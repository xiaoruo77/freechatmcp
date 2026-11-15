"""All ToDesk tests use an in-memory Windows seam; no real UI/input calls."""
from copy import deepcopy

import pytest

from web_tools.todesk_native import (
    NativeToDesk, NativeToDeskError, _Win32, WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS,
    WM_CHAR, WM_CLOSE, WM_KEYDOWN, WM_KEYUP,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_MOUSEWHEEL,
)


EXE = r"D:\Apps\ToDesk\ToDesk.exe"


class FakeConnector:
    def __init__(self, callback=None, receipt=None):
        self.callback = callback
        self.receipt = receipt if receipt is not None else {"ok": False, "connection_request_invoked": False}
        self.calls = []

    def connect(self, name):
        self.calls.append(name)
        if self.callback:
            self.callback()
        return deepcopy(self.receipt)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.after_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.after_sleep:
            self.after_sleep()


class FakeWin32:
    def __init__(self):
        self.windows = {
            10: {"pid": 77, "title": "ToDesk", "class": "MainForm", "size": (500, 500), "root": 10},
            20: {"pid": 77, "title": "DEMO-NODE", "class": "Viewer", "size": (1200, 800), "root": 20},
            21: {"pid": 77, "title": "", "class": "RemoteCanvas", "size": (1200, 760), "root": 20},
            30: {"pid": 88, "title": "DEMO-NODE", "class": "Viewer", "size": (600, 400), "root": 30},
        }
        self.processes = {77: {"exe": EXE, "create_time": 123.25},
                          88: {"exe": r"D:\Other\ToDesk.exe", "create_time": 987.0}}
        self.default = [10, 20, 30]
        self.messages = []
        self.fail_at = None
        self.after_send = None
        self.close_immediately = True
        self.minimized = set()
        self.restores = []
        self.hit_result = 20
        self.focus_result = 20
        self.hit_queries = []
        self.foreground_value = 900
        self.windows[900] = {"pid": 999, "title": "Other application", "class": "Other", "size": (500, 500), "root": 900}

    def desktop_windows(self, desktop):
        assert desktop == "Default"
        return list(self.default)

    def exists(self, hwnd):
        return hwnd in self.windows

    def root(self, hwnd):
        return self.windows[hwnd]["root"]

    def children(self, hwnd):
        return [h for h, w in self.windows.items() if w["root"] == hwnd and h != hwnd]

    def is_child(self, root, hwnd):
        return hwnd in self.windows and hwnd != root and self.windows[hwnd]["root"] == root

    def is_iconic(self, hwnd):
        return hwnd in self.minimized

    def restore_background(self, hwnd):
        self.restores.append(hwnd)
        self.minimized.discard(hwnd)
        self.windows[hwnd]["size"] = (1200, 800)

    def hit_test(self, root, x, y):
        self.hit_queries.append((root, x, y))
        return self.hit_result

    def focus_window(self, root):
        assert root == 20
        return self.focus_result

    def foreground(self):
        return self.foreground_value

    def pid(self, hwnd):
        return self.windows[hwnd]["pid"]

    def process(self, pid):
        return deepcopy(self.processes[pid])

    def title(self, hwnd):
        return self.windows[hwnd]["title"]

    def class_name(self, hwnd):
        return self.windows[hwnd]["class"]

    def client_size(self, hwnd):
        return self.windows[hwnd]["size"]

    def map_point(self, source, target, x, y):
        if source == 20 and target == 21:
            return x, y - 40
        if source == 21 and target == 0:
            return x + 500, y + 600
        return x, y

    def scan_code(self, vk):
        return 28 if vk == 13 else 1

    def send(self, hwnd, message, wparam, lparam, timeout_ms):
        assert timeout_ms == 500
        if self.fail_at == len(self.messages):
            raise NativeToDeskError("message_delivery_unknown", "timeout", may_have_run=True)
        self.messages.append((hwnd, message, wparam, lparam))
        if self.after_send:
            self.after_send(self)
        if message == WM_CLOSE and self.close_immediately:
            self.windows.pop(hwnd)
        return 0


@pytest.fixture
def backend():
    api = FakeWin32()
    return NativeToDesk(EXE, ["DEMO-NODE"], _api=api, _connector=FakeConnector()), api


def viewer(native):
    return native.connect("DEMO-NODE")


def test_inventory_filters_executable_and_identifies_exact_viewer(backend):
    native, api = backend
    items = native.inventory()
    assert [item["hwnd"] for item in items] == [10, 20]
    assert [item["viewer"] for item in items] == [False, True]
    assert items[1]["client_width"] == 1200
    assert items[1]["client_height"] == 800
    assert items[1]["desktop"] == "Default"
    assert items[1]["executable"] == items[1]["exe"] == EXE
    assert items[1]["visible"] is True
    assert api.messages == []


def test_inventory_reports_native_visibility_without_excluding_hidden_windows(backend):
    native, api = backend
    api.visible = lambda hwnd: hwnd != 10
    items = native.inventory()
    assert items[0]["visible"] is False
    assert items[1]["visible"] is True
    assert api.messages == []


def test_connect_attaches_existing_without_input(backend):
    native, api = backend
    assert viewer(native)["attached_existing"] is True
    assert api.messages == []
    api.default.remove(20)
    with pytest.raises(NativeToDeskError, match="No connected viewer"):
        native.connect("DEMO-NODE")


@pytest.mark.parametrize("mutation,code", [
    (lambda r, a: r.update(desktop="other"), "desktop_mismatch"),
    (lambda r, a: r.update(exe=r"D:\Wrong\ToDesk.exe"), "executable_mismatch"),
    (lambda r, a: r.update(executable=r"D:\Wrong\ToDesk.exe"), "executable_mismatch"),
    (lambda r, a: a.processes[77].update(create_time=200.0), "process_identity_mismatch"),
    (lambda r, a: a.windows[20].update(title="ToDesk"), "viewer_mismatch"),
    (lambda r, a: a.windows[20].update(pid=88), "executable_mismatch"),
    (lambda r, a: a.default.remove(20), "desktop_mismatch"),
])
def test_identity_changes_rejected_before_any_input(backend, mutation, code):
    native, api = backend
    record = viewer(native)
    mutation(record, api)
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(record, "click", {"x": 30, "y": 100})
    assert exc.value.code == code
    assert api.messages == []


def test_main_gui_cannot_be_closed_as_viewer(backend):
    native, api = backend
    main = native.inventory()[0]
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(main, "close")
    assert exc.value.code == "viewer_mismatch"
    assert api.messages == []


def test_utf16_surrogate_pairs_deliver_only_to_canvas(backend):
    native, api = backend
    result = native.perform(viewer(native), "type", {"text": "中😀"})
    assert [(h, m, w) for h, m, w, _ in api.messages] == [(21, WM_CHAR, 0x4E2D), (21, WM_CHAR, 0xD83D), (21, WM_CHAR, 0xDE00)]
    assert result["status"] == "messages_delivered"
    assert result["remote_effect_verified"] is False
    assert result["host_input_used"] is False


def test_click_maps_root_client_pixels_to_canvas(backend):
    native, api = backend
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["ok"] is True
    assert api.messages == [(21, WM_MOUSEMOVE, 0, 30 | 60 << 16),
                            (21, WM_LBUTTONDOWN, 1, 30 | 60 << 16),
                            (21, WM_LBUTTONUP, 0, 30 | 60 << 16)]


def test_toolbar_point_is_rejected_without_input(backend):
    native, api = backend
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), "click", {"x": 10, "y": 10})
    assert exc.value.code == "point_outside_canvas"
    assert api.messages == []


def test_scroll_uses_screen_coordinates_and_signed_wheel_delta(backend):
    native, api = backend
    native.perform(viewer(native), "scroll", {"x": 30, "y": 100, "vertical": -120})
    assert api.messages == [(21, WM_MOUSEWHEEL, ((-120) & 0xFFFF) << 16, 530 | 660 << 16)]


def test_key_uses_vk_scan_and_release_bits(backend):
    native, api = backend
    native.perform(viewer(native), "key", {"keys": "Enter"})
    assert api.messages == [(21, WM_KEYDOWN, 13, 1 | 28 << 16),
                            (21, WM_KEYUP, 13, 1 | 28 << 16 | 0xC0000000)]


@pytest.mark.parametrize("args", [{"keys": "Ctrl+A"}, {"keys": "Alt+Tab"}, {"keys": "Shift"}])
def test_modifier_chords_fail_explicitly_before_delivery(backend, args):
    native, api = backend
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), "key", args)
    assert exc.value.code == "modifier_chord_unsupported"
    assert api.messages == []


def test_drag_is_bounded_and_releases_left_button(backend):
    native, api = backend
    native.perform(viewer(native), "drag", {"x": 10, "y": 100, "to_x": 20, "to_y": 110, "duration_ms": 0})
    assert api.messages[-2:] == [(21, WM_MOUSEMOVE, 1, 20 | 70 << 16), (21, WM_LBUTTONUP, 0, 20 | 70 << 16)]


def test_unknown_delivery_is_not_replayed(backend):
    native, api = backend
    api.fail_at = 1
    result = native.perform(viewer(native), "type", {"text": "abc"})
    assert len(api.messages) == 1
    assert result["status"] == "delivery_unknown"
    assert result["messages_sent"] == 1
    assert result["retry_safe"] is False
    assert result["may_have_run"] is True


def test_identity_rechecked_between_each_message(backend):
    native, api = backend
    api.after_send = lambda a: a.windows[20].update(title="Other device")
    result = native.perform(viewer(native), "type", {"text": "abc"})
    assert len(api.messages) == 1
    assert result["status"] == "delivery_unknown"
    assert result["code"] == "viewer_mismatch"


def test_changed_canvas_geometry_stops_remaining_messages(backend):
    native, api = backend
    api.after_send = lambda a: a.windows[21].update(size=(600, 400))
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert len(api.messages) == 1
    assert result["status"] == "delivery_unknown"
    assert result["code"] == "input_target_changed"


def test_unknown_first_delivery_is_not_marked_safe_to_retry(backend):
    native, api = backend
    api.fail_at = 0
    result = native.perform(viewer(native), "type", {"text": "a"})
    assert api.messages == []
    assert result["messages_sent"] == 0
    assert result["may_have_run"] is True
    assert result["retry_safe"] is False


def test_unrecognized_child_fails_closed(backend):
    native, api = backend
    api.windows[21]["class"] = "Edit"
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), "type", {"text": "secret"})
    assert exc.value.code == "input_target_unavailable"
    assert api.messages == []


def test_close_targets_only_viewer_and_observes_absence(backend):
    native, api = backend
    result = native.perform(viewer(native), "close")
    assert result["closed"] is True
    assert api.messages == [(20, WM_CLOSE, 0, 0)]
    assert 10 in api.windows


def test_close_request_does_not_claim_closure(backend):
    native, api = backend
    api.close_immediately = False
    result = native.perform(viewer(native), "close")
    assert result["closed"] is False
    assert result["status"] == "close_requested"


def test_ambiguous_existing_viewer_is_rejected(backend):
    native, api = backend
    api.windows[40] = {**api.windows[20], "root": 40}
    api.default.append(40)
    with pytest.raises(NativeToDeskError) as exc:
        viewer(native)
    assert exc.value.code == "ambiguous_viewer"
    assert api.messages == []


def test_known_sciter_video_canvas_is_selected(backend):
    native, api = backend
    api.windows[21]["class"] = "SciterVideoClass"
    native.perform(viewer(native), "type", {"text": "a"})
    assert api.messages[0][0] == 21


def test_connect_restores_only_verified_minimized_viewer(backend):
    native, api = backend
    api.windows[20]["size"] = (209, 33)
    api.minimized.add(20)
    record = viewer(native)
    assert record["client_width"] == 1200
    assert api.restores == [20]
    assert api.messages == []


def test_close_already_missing_or_reused_identity_never_messages_new_window(backend):
    native, api = backend
    record = viewer(native)
    api.windows[20]["pid"] = 88
    result = native.perform(record, "close")
    assert result["closed"] is True
    assert api.messages == []
    api.windows.pop(20)
    assert native.perform(record, "close")["closed"] is True


def configure_sciter_viewer(api):
    api.windows[20]["class"] = "H-SMILE-FRAME"
    api.windows[21]["class"] = "SciterVideoClass"
    api.windows[22] = {"pid": 77, "title": "", "class": "SciterUIClass", "size": (1200, 800), "root": 20}


def test_known_sciter_viewer_root_receives_hit_tested_mouse_messages(backend):
    native, api = backend
    configure_sciter_viewer(api)
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["target_hwnd"] == 20
    assert all(message[0] == 20 for message in api.messages)
    assert api.messages[0][3] == 30 | 100 << 16
    assert api.hit_queries and all(query == (20, 30, 100) for query in api.hit_queries)


def test_known_sciter_root_receives_keyboard_only_with_existing_root_focus(backend):
    native, api = backend
    configure_sciter_viewer(api)
    result = native.perform(viewer(native), "type", {"text": "a"})
    assert result["target_hwnd"] == 20
    assert api.messages == [(20, WM_CHAR, 97, 1)]
    assert api.hit_queries == []


@pytest.mark.parametrize("kind", ["hit", "focus"])
def test_sciter_evidence_must_not_point_to_shared_main_window(backend, kind):
    native, api = backend
    configure_sciter_viewer(api)
    if kind == "hit":
        api.hit_result = 10
        action, args = "click", {"x": 30, "y": 100}
    else:
        api.focus_result = 10
        action, args = "type", {"text": "a"}
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), action, args)
    assert exc.value.code == "input_target_unavailable"
    assert api.messages == []


def test_root_target_requires_verified_sciter_video_and_ui_structure(backend):
    native, api = backend
    configure_sciter_viewer(api)
    api.windows.pop(21)
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert exc.value.code == "input_target_unavailable"
    assert api.messages == []


def test_root_hit_test_change_stops_remaining_messages(backend):
    native, api = backend
    configure_sciter_viewer(api)
    api.after_send = lambda a: setattr(a, "hit_result", 22)
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert len(api.messages) == 1
    assert result["status"] == "delivery_unknown"
    assert result["code"] == "input_target_changed"


def test_sciter_hit_test_can_select_verified_input_child(backend):
    native, api = backend
    configure_sciter_viewer(api)
    api.hit_result = 22
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["target_hwnd"] == 22
    assert all(message[0] == 22 for message in api.messages)


def test_connect_request_requires_observed_verified_viewer_and_preserves_config():
    api, clock = FakeWin32(), FakeClock()
    api.default.remove(20)
    config = {"DEMO-NODE": {"device_id": "123456789"}}
    connector = FakeConnector(receipt={"ok": True, "connection_request_invoked": True, "foreground_changed": False})
    clock.after_sleep = lambda: api.default.append(20) if 20 not in api.default else None
    native = NativeToDesk(EXE, config, _api=api, _connector=connector, _clock=clock, _sleep=clock.sleep)
    config["DEMO-NODE"]["device_id"] = "987654321"
    result = native.connect("demo-node")
    assert result["hwnd"] == 20
    assert result["connection_verified"] is True
    assert result["attached_existing"] is False
    assert connector.calls == ["DEMO-NODE"]
    assert native.allowed_devices["DEMO-NODE"]["device_id"] == "123456789"
    assert api.messages == []


def test_connect_request_without_viewer_times_out_without_claiming_success():
    api, clock = FakeWin32(), FakeClock()
    api.default.remove(20)
    connector = FakeConnector(receipt={"ok": True, "connection_request_invoked": True})
    native = NativeToDesk(EXE, {"DEMO-NODE": {"device_id": "123456789"}}, _api=api,
                          _connector=connector, _clock=clock, _sleep=clock.sleep)
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "connection_not_established"
    assert exc.value.may_have_run is True
    assert clock.now == pytest.approx(9.0)
    assert connector.calls == ["DEMO-NODE"]
    assert api.messages == []


def test_existing_viewer_never_invokes_saved_device_connector():
    api, connector = FakeWin32(), FakeConnector()
    native = NativeToDesk(EXE, ["DEMO-NODE"], _api=api, _connector=connector)
    assert native.connect("DEMO-NODE")["attached_existing"] is True
    assert connector.calls == []


def test_connect_rejects_foreground_change_without_polling_or_input():
    api, clock = FakeWin32(), FakeClock()
    api.default.remove(20)
    connector = FakeConnector(receipt={"ok": True, "connection_request_invoked": True, "foreground_changed": True})
    native = NativeToDesk(EXE, ["DEMO-NODE"], _api=api, _connector=connector, _clock=clock, _sleep=clock.sleep)
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "connection_foreground_changed"
    assert exc.value.may_have_run is True
    assert clock.sleeps == []
    assert api.messages == []


def test_ambiguous_viewers_after_request_are_rejected():
    api, clock = FakeWin32(), FakeClock()
    api.default.remove(20)
    def create_viewers():
        api.default.append(20)
        api.windows[40] = {**api.windows[20], "root": 40}
        api.default.append(40)
    connector = FakeConnector(callback=create_viewers, receipt={"ok": True, "connection_request_invoked": True})
    native = NativeToDesk(EXE, ["DEMO-NODE"], _api=api, _connector=connector, _clock=clock, _sleep=clock.sleep)
    with pytest.raises(NativeToDeskError) as exc:
        native.connect("DEMO-NODE")
    assert exc.value.code == "ambiguous_viewer"
    assert exc.value.may_have_run is True
    assert api.messages == []


class FakeMessageAPI:
    def __init__(self, accept_post=True):
        self.queued = []
        self.synchronous = []
        self.accept_post = accept_post

    def PostMessageW(self, hwnd, message, wparam, lparam):
        self.queued.append((hwnd, message, wparam, lparam))
        return self.accept_post

    def SendMessageTimeoutW(self, hwnd, message, wparam, lparam, flags, timeout, output):
        self.synchronous.append((hwnd, message, wparam, lparam, flags, timeout))
        return 1


def test_native_transport_queues_input_and_spaces_press_release(monkeypatch):
    api = object.__new__(_Win32)
    api.user = FakeMessageAPI()
    delays = []
    monkeypatch.setattr("web_tools.todesk_native.time.sleep", delays.append)
    for message in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_MOUSEWHEEL):
        assert api.send(20, message, 1, 2, 500) == 0
    assert [item[1] for item in api.user.queued] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_MOUSEWHEEL]
    assert api.user.synchronous == []
    assert delays == [0.02] * 4


def test_native_transport_keeps_close_and_null_bounded_synchronous():
    api = object.__new__(_Win32)
    api.user = FakeMessageAPI()
    api.send(20, WM_CLOSE, 0, 0, 500)
    api.send(20, 0, 0, 0, 300)
    assert api.user.queued == []
    assert api.user.synchronous == [(20, WM_CLOSE, 0, 0, 0x23, 500), (20, 0, 0, 0, 0x23, 300)]


def test_failed_post_has_fixed_error_without_sync_fallback_or_replay():
    api = object.__new__(_Win32)
    api.user = FakeMessageAPI(accept_post=False)
    with pytest.raises(NativeToDeskError) as exc:
        api.send(20, WM_CHAR, 97, 1, 500)
    assert exc.value.code == "message_queue_failed"
    assert exc.value.may_have_run is False
    assert api.user.queued == [(20, WM_CHAR, 97, 1)]
    assert api.user.synchronous == []


def test_optional_activation_notifications_surround_queued_viewer_action(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert [m[1] for m in api.messages] == [WM_ACTIVATE, WM_SETFOCUS, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_KILLFOCUS, WM_ACTIVATE]
    assert api.messages[0] == (20, WM_ACTIVATE, 1, 900)
    assert api.messages[-1] == (20, WM_ACTIVATE, 0, 900)
    assert all(m[0] == 20 for m in api.messages)
    assert result["message_only_activation_used"] is True
    assert result["foreground_before"] == result["foreground_after"] == 900
    assert result["foreground_changed"] is False
    assert result["remote_effect_verified"] is False


def test_activation_notifications_are_opt_in(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert [m[1] for m in api.messages] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]


def test_activation_denied_when_another_todesk_window_owns_foreground(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    api.foreground_value = 10
    with pytest.raises(NativeToDeskError) as exc:
        native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert exc.value.code == "activation_same_process_foreground_denied"
    assert exc.value.may_have_run is False
    assert api.messages == []


def test_existing_foreground_viewer_needs_no_notifications(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    api.foreground_value = 20
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["message_only_activation_used"] is False
    assert [m[1] for m in api.messages] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]


def test_user_selecting_viewer_mid_action_skips_loss_of_focus_notifications(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    def select_viewer(a):
        if a.messages[-1][1] == WM_LBUTTONDOWN:
            a.foreground_value = 20
    api.after_send = select_viewer
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["activation_cleanup_skipped_viewer_foreground"] is True
    assert result["foreground_after"] == 20
    assert result["foreground_changed"] is True
    assert WM_KILLFOCUS not in [m[1] for m in api.messages]
    assert (20, WM_ACTIVATE, 0, 20) not in api.messages


def test_notification_triggered_foreground_change_aborts_input_and_reports_change(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    api.after_send = lambda a: setattr(a, "foreground_value", 20)
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert api.messages == [(20, WM_ACTIVATE, 1, 900)]
    assert result["ok"] is False
    assert result["code"] == "foreground_changed"
    assert result["foreground_changed"] is True
    assert result["foreground_before"] == 900
    assert result["foreground_after"] == 20
    assert result["messages_sent"] == 0
    assert result["retry_safe"] is False


def test_failed_input_still_restores_notification_state_without_replay(backend):
    native, api = backend
    configure_sciter_viewer(api)
    native.message_only_activation = True
    api.fail_at = 3
    result = native.perform(viewer(native), "click", {"x": 30, "y": 100})
    assert result["ok"] is False
    assert result["retry_safe"] is False
    assert [m[1] for m in api.messages].count(WM_MOUSEMOVE) == 1
    # The seam fails every subsequent dispatch at this point; cleanup is
    # reported unknown rather than claiming that focus notifications reverted.
    assert result["activation_cleanup_unknown"] is True
