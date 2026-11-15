import pytest

from web_tools.uu_window_transport import (
    WindowMessageTransport, WindowTransportError, WM_CHAR, WM_KEYDOWN, WM_KEYUP,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_MOUSEMOVE, WM_MOUSEWHEEL,
)


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.pids = {10: 100, 20: 100, 30: 300}
        self.resizes = []
        self.on_send = None

    def exists(self, hwnd): return hwnd in self.pids
    def pid(self, hwnd): return self.pids[hwnd]
    def is_child(self, root, child): return root == 10 and child in {20, 30}
    def rect(self, hwnd, client=False):
        if client: return (0, 0, 800, 560) if hwnd == 10 else (0, 0, 800, 520)
        return (100, 200, 900, 800) if hwnd == 10 else (100, 280, 900, 800)
    def screen_to_client(self, hwnd, x, y): return x-100, y-(240 if hwnd == 10 else 280)
    def child_at(self, hwnd, x, y): return 20 if hwnd == 10 and y >= 40 else hwnd
    def describe(self, hwnd):
        return {"hwnd": hwnd, "pid": self.pid(hwnd), "class_name": "QtWindow",
                "rect": self.rect(hwnd), "client_rect": self.rect(hwnd, True), "visible": True}
    def children(self, root): return [20, 30]
    def send(self, *args):
        self.messages.append(args)
        if self.on_send: self.on_send(args)
    def scan(self, vk): return vk
    def resize(self, hwnd, width, height): self.resizes.append((hwnd, width, height))


@pytest.fixture
def setup():
    api = FakeAPI()
    validator_calls = []
    def validate(hwnd):
        validator_calls.append(hwnd)
        return {"pid": 100} if hwnd == 10 else None
    return WindowMessageTransport(validate, api=api), api, validator_calls


def test_inspect_filters_other_process_child(setup):
    transport, api, _ = setup
    result = transport.inspect(10)
    assert [c["hwnd"] for c in result["children"]] == [20]
    assert result["foreground_input_used"] is False
    assert not api.messages


def test_click_maps_screenshot_point_to_private_child(setup):
    transport, api, calls = setup
    result = transport.perform(10, "click", {"x": 50, "y": 100, "focus": False})
    assert result["target_hwnd"] == 20
    assert [m[1] for m in api.messages] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
    assert all(m[0] == 20 and m[3] == (50 | (20 << 16)) for m in api.messages)
    assert len(calls) >= len(api.messages)+1
    assert result["status"] == "dispatched"
    assert result["remote_effect_verified"] is False


def test_double_click_delivers_windows_double_click_sequence(setup):
    transport, api, _ = setup
    transport.perform(10, "click", {"x": 50, "y": 100, "count": 2, "focus": False})
    assert [m[1] for m in api.messages] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP,
                                          WM_LBUTTONDBLCLK, WM_LBUTTONUP]


@pytest.mark.parametrize("args", [{"x": -1, "y": 100}, {"x": 800, "y": 100},
                                 {"x": 50, "y": 20}, {"x": True, "y": 100},
                                 {"x": float("nan"), "y": 100},
                                 {"x": 50, "y": 100, "target_hwnd": 30}])
def test_invalid_point_or_foreign_child_never_receives_messages(setup, args):
    transport, api, _ = setup
    with pytest.raises(WindowTransportError): transport.perform(10, "click", args)
    assert not api.messages


def test_stale_process_is_rejected_between_messages(setup):
    transport, api, _ = setup
    api.on_send = lambda _: api.pids.__setitem__(10, 101)
    with pytest.raises(WindowTransportError, match="process changed"):
        transport.perform(10, "click", {"x": 50, "y": 100, "focus": False})
    assert len(api.messages) == 1


def test_root_validator_failure_never_reaches_native_transport():
    api = FakeAPI()
    transport = WindowMessageTransport(lambda _: None, api=api)
    with pytest.raises(WindowTransportError): transport.perform(10, "type", {"text": "abc"})
    assert not api.messages


def test_type_unicode_uses_utf16_char_messages_not_clipboard(setup):
    transport, api, _ = setup
    transport.perform(10, "type", {"target_hwnd": 20, "text": "A中😀\n", "focus": False})
    assert [m[1] for m in api.messages] == [WM_CHAR]*5
    assert [m[2] for m in api.messages] == [65, 0x4E2D, 0xD83D, 0xDE00, 13]


def test_key_chord_releases_in_reverse_order(setup):
    transport, api, _ = setup
    transport.perform(10, "key", {"target_hwnd": 20, "keys": "CTRL+A", "focus": False})
    assert [(m[1], m[2]) for m in api.messages] == [(WM_KEYDOWN, 0x11), (WM_KEYDOWN, 65),
                                                  (WM_KEYUP, 65), (WM_KEYUP, 0x11)]


@pytest.mark.parametrize("keys", ["WIN+R", "CTRL+CONTROL", "CTRL+CTRL", [], [42]])
def test_unsupported_or_duplicate_keys_fail_before_delivery(setup, keys):
    transport, api, _ = setup
    with pytest.raises(WindowTransportError): transport.perform(10, "key", {"keys": keys})
    assert not api.messages


def test_wheel_uses_screen_coordinates_and_signed_delta(setup):
    transport, api, _ = setup
    transport.perform(10, "scroll", {"x": 50, "y": 100, "vertical": -120, "focus": False})
    assert api.messages == [(20, WM_MOUSEWHEEL, ((-120 & 0xFFFF) << 16), 150 | (300 << 16), 300)]


def test_drag_stays_on_same_validated_child_and_releases(setup):
    transport, api, _ = setup
    transport.perform(10, "drag", {"x": 50, "y": 100, "to_x": 100, "to_y": 150,
                                    "steps": 3, "duration_ms": 0, "focus": False})
    assert api.messages[1][1] == WM_LBUTTONDOWN
    assert api.messages[-1][1] == WM_LBUTTONUP
    assert all(m[0] == 20 for m in api.messages)
    assert api.messages[-1][3] == 100 | (70 << 16)


def test_resize_is_bounded_and_reports_unverified_remote_effect(setup):
    transport, api, _ = setup
    result = transport.perform(10, "resize", {"width": 1248, "height": 872})
    assert api.resizes == [(10, 1248, 872)]
    assert result["remote_effect_verified"] is False
    with pytest.raises(WindowTransportError): transport.perform(10, "resize", {"width": 8192, "height": 8192})
    assert len(api.resizes) == 1


def test_no_implicit_fallback_or_unknown_argument(setup):
    transport, api, _ = setup
    with pytest.raises(WindowTransportError): transport.perform(10, "click", {"x": 50, "y": 100, "activate": True})
    with pytest.raises(WindowTransportError): transport.perform(10, "unknown", {})
    assert not api.messages


def test_transport_failure_releases_keys_already_delivered(setup):
    transport, api, _ = setup
    def fail_a_down(message):
        if message[1:3] == (WM_KEYDOWN, 65):
            raise WindowTransportError("window_message_not_delivered", "hung target")
    api.on_send = fail_a_down
    with pytest.raises(WindowTransportError, match="hung target"):
        transport.perform(10, "key", {"target_hwnd": 20, "keys": "CTRL+A", "focus": False})
    assert api.messages[-1][1:3] == (WM_KEYUP, 0x11)


def test_action_budget_stops_long_message_sequence(setup, monkeypatch):
    import web_tools.uu_window_transport as module
    transport, api, _ = setup
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    api.on_send = lambda _: now.__setitem__(0, now[0]+9)
    with pytest.raises(WindowTransportError, match="time budget"):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "AB", "focus": False})
    assert len(api.messages) == 1


@pytest.mark.parametrize("fail_inside", [False, True])
def test_dpi_context_restores_previous_thread_mode_even_after_error(monkeypatch, fail_inside):
    import ctypes
    from types import SimpleNamespace
    import web_tools.uu_window_transport as module
    calls = []
    class Switch:
        def __call__(self, context):
            calls.append(context.value if isinstance(context, ctypes.c_void_p) else context)
            return 777 if len(calls) == 1 else ctypes.c_void_p(-4).value
    switch = Switch()
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(SetThreadDpiAwarenessContext=switch), raising=False)
    if fail_inside:
        with pytest.raises(RuntimeError, match="capture failed"):
            with module.dpi_context():
                raise RuntimeError("capture failed")
    else:
        with module.dpi_context():
            assert calls == [ctypes.c_void_p(-4).value]
    assert calls == [ctypes.c_void_p(-4).value, 777]


def test_native_geometry_and_delivery_share_one_restored_context(setup):
    from contextlib import contextmanager
    transport, api, _ = setup
    active = [False]
    entries = []
    @contextmanager
    def context():
        assert not active[0]
        active[0] = True
        entries.append("enter")
        try: yield
        finally:
            active[0] = False
            entries.append("exit")
    api.dpi_context = context
    original_rect = api.rect
    original_send = api.send
    def rect(*a, **kw):
        assert active[0]
        return original_rect(*a, **kw)
    def send(*a, **kw):
        assert active[0]
        return original_send(*a, **kw)
    api.rect, api.send = rect, send
    transport.perform(10, "click", {"x": 50, "y": 100, "focus": False})
    transport.inspect(10)
    with pytest.raises(WindowTransportError): transport.perform(10, "click", {"x": -1, "y": 100})
    assert entries == ["enter", "exit"]*3
    assert not active[0]


def test_dpi_context_rejects_failed_entry_and_reports_failed_restore(monkeypatch):
    from types import SimpleNamespace
    import web_tools.uu_window_transport as module
    class Switch:
        def __init__(self, results): self.results = iter(results)
        def __call__(self, _): return next(self.results)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    switch = Switch([0])
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(SetThreadDpiAwarenessContext=switch), raising=False)
    with pytest.raises(WindowTransportError, match="Cannot enter"):
        with module.dpi_context(): pytest.fail("failed entry must not yield")
    switch = Switch([777, 0])
    with pytest.raises(WindowTransportError, match="Cannot restore"):
        with module.dpi_context(): pass


class PrivateFocusAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.pids[40] = 100
        self.desktop_names = {1: "Xiaoruo1Background_UU", 2: "Xiaoruo1Background_UU"}
        self.input_name = "Default"
        self.membership = True
        self.foreign_desktop_unavailable = False
        self.state = {"focus": 40, "active": 10}
        self.attachments = []
        self.focus_log = []
    def current_thread(self): return 1
    def window_thread(self, hwnd): return 2
    def thread_desktop(self, thread):
        if thread == 2 and self.foreign_desktop_unavailable:
            raise WindowTransportError("private_focus_desktop_unknown", "foreign desktop handle")
        return self.desktop_names[thread]
    def window_on_desktop(self, hwnd, expected):
        return self.membership and hwnd == 10 and expected == "Xiaoruo1Background_UU"
    def input_desktop(self): return self.input_name
    def focus_state(self, thread): return dict(self.state)
    def attach(self, actor, gui, enabled):
        self.attachments.append((actor, gui, enabled))
        return True
    def set_active(self, hwnd):
        self.state["active"] = hwnd
        self.focus_log.append(("active", hwnd))
    def set_focus(self, hwnd):
        self.state["focus"] = hwnd
        self.focus_log.append(("focus", hwnd))
    def private_focus(self, *args):
        from web_tools.uu_window_transport import _private_focus_context
        return _private_focus_context(self, *args)


@pytest.mark.parametrize("raise_inside", [False, True])
def test_native_private_focus_is_scoped_and_restored_on_failure(raise_inside):
    api = PrivateFocusAPI()
    transport = WindowMessageTransport(lambda _: {"pid": 100, "desktop": "Xiaoruo1Background_UU"}, api=api)
    observed = []
    def capture_state(message):
        if message[1] == WM_CHAR:
            observed.append(dict(api.state))
            if raise_inside:
                raise RuntimeError("UU failed")
    api.on_send = capture_state
    if raise_inside:
        with pytest.raises(RuntimeError, match="UU failed"):
            transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    else:
        transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert observed == [{"active": 10, "focus": 20}]
    assert api.state == {"active": 10, "focus": 40}
    assert api.attachments == [(1, 2, True), (1, 2, False)]
    assert api.focus_log == [("active", 10), ("focus", 20), ("focus", 40), ("active", 10)]


@pytest.mark.parametrize("mutate", [
    lambda api: api.desktop_names.__setitem__(1, "Default"),
    lambda api: api.desktop_names.__setitem__(2, "Default"),
    lambda api: setattr(api, "input_name", "Xiaoruo1Background_UU"),
    lambda api: api.pids.__setitem__(40, 300),
    lambda api: setattr(api, "membership", False),
])
def test_native_focus_never_attaches_default_or_foreign_state(mutate):
    api = PrivateFocusAPI()
    mutate(api)
    transport = WindowMessageTransport(lambda _: {"pid": 100, "desktop": "Xiaoruo1Background_UU"}, api=api)
    with pytest.raises(WindowTransportError):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert not api.attachments
    assert not api.focus_log
    assert not api.messages


def test_native_focus_requires_explicit_validated_desktop():
    api = PrivateFocusAPI()
    for record in (True, {"pid": 100}, {"pid": 100, "desktop": "Default"}):
        transport = WindowMessageTransport(lambda _: record, api=api)
        with pytest.raises(WindowTransportError, match="explicit verified private desktop"):
            transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert not api.attachments


def test_native_focus_does_not_silently_fallback_if_unavailable(setup):
    transport, api, _ = setup
    with pytest.raises(WindowTransportError, match="unavailable"):
        transport.perform(10, "type", {"text": "A", "focus_mode": "native_private"})
    assert not api.messages


def test_native_focus_detaches_even_if_target_becomes_stale():
    api = PrivateFocusAPI()
    valid = [True]
    def validate(_): return {"pid": 100, "desktop": "Xiaoruo1Background_UU"} if valid[0] else None
    def invalidate(message):
        if message[1] == WM_CHAR: valid[0] = False
    api.on_send = invalidate
    transport = WindowMessageTransport(validate, api=api)
    with pytest.raises(WindowTransportError, match="ownership"):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "AB", "focus_mode": "native_private"})
    assert api.attachments == [(1, 2, True), (1, 2, False)]


def test_native_focus_foreign_desktop_handle_requires_actual_root_membership():
    api = PrivateFocusAPI()
    api.foreign_desktop_unavailable = True
    transport = WindowMessageTransport(lambda _: {"pid": 100, "desktop": "Xiaoruo1Background_UU"}, api=api)
    transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert api.attachments == [(1, 2, True), (1, 2, False)]
    api.membership = False
    api.attachments.clear()
    with pytest.raises(WindowTransportError, match="not a member"):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert not api.attachments


def test_native_focus_rechecks_membership_before_restoring_and_always_detaches():
    api = PrivateFocusAPI()
    transport = WindowMessageTransport(lambda _: {"pid": 100, "desktop": "Xiaoruo1Background_UU"}, api=api)
    def remove_membership(message):
        if message[1] == WM_CHAR: api.membership = False
    api.on_send = remove_membership
    with pytest.raises(WindowTransportError, match="not a member"):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "focus_mode": "native_private"})
    assert api.attachments == [(1, 2, True), (1, 2, False)]


class PostedAPI(PrivateFocusAPI):
    def __init__(self):
        super().__init__()
        self.posted = []
        self.on_post = None
    def post(self, *args):
        self.posted.append(args)
        if self.on_post: self.on_post(args)


def test_posted_key_waits_inside_native_private_focus_then_restores(monkeypatch):
    import web_tools.uu_window_transport as module
    api = PostedAPI()
    during_post = []
    api.on_post = lambda _: during_post.append(dict(api.state))
    waits = []
    monkeypatch.setattr(module.time, "sleep", lambda duration: waits.append((duration, dict(api.state))))
    transport = WindowMessageTransport(lambda _: {"pid": 100, "desktop": "Xiaoruo1Background_UU"}, api=api)
    result = transport.perform(10, "key", {"target_hwnd": 20, "keys": "N",
                                             "delivery": "posted", "focus_mode": "native_private"})
    assert [(m[1], m[2]) for m in api.posted] == [(WM_KEYDOWN, ord("N")), (WM_KEYUP, ord("N"))]
    assert during_post == [{"focus": 20, "active": 10}]*2
    assert waits == [(.05, {"focus": 20, "active": 10})]
    assert api.state == {"focus": 40, "active": 10}
    assert all(m[1] == 0 for m in api.messages)
    assert result["input_delivery"] == "posted_window_messages"
    assert result["remote_effect_verified"] is False


def test_posted_unicode_type_queues_chars_without_sent_keyboard_fallback(monkeypatch):
    import web_tools.uu_window_transport as module
    api = PostedAPI()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    transport = WindowMessageTransport(lambda _: {"pid": 100}, api=api)
    transport.perform(10, "type", {"target_hwnd": 20, "text": "中😀", "delivery": "posted", "focus": False})
    assert [(m[1], m[2]) for m in api.posted] == [(WM_CHAR, 0x4E2D), (WM_CHAR, 0xD83D), (WM_CHAR, 0xDE00)]
    assert all(m[1] == 0 for m in api.messages)


def test_posted_delivery_is_explicit_and_requires_queue_support(setup):
    transport, api, _ = setup
    with pytest.raises(WindowTransportError, match="unavailable"):
        transport.perform(10, "key", {"keys": "N", "delivery": "posted"})
    with pytest.raises(WindowTransportError, match="Unsupported"):
        transport.perform(10, "key", {"keys": "N", "delivery": "global"})
    assert not api.messages


def test_posted_each_message_revalidates_ownership(monkeypatch):
    import web_tools.uu_window_transport as module
    api = PostedAPI()
    valid = [True]
    api.on_post = lambda _: valid.__setitem__(0, False)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    transport = WindowMessageTransport(lambda _: {"pid": 100} if valid[0] else None, api=api)
    with pytest.raises(WindowTransportError):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "AB", "delivery": "posted", "focus": False})
    assert len(api.posted) == 1
    assert not api.messages


def test_rejected_post_does_not_retry_as_sent_input(monkeypatch):
    import web_tools.uu_window_transport as module
    api = PostedAPI()
    def reject(_): raise WindowTransportError("window_message_not_posted", "queue rejected")
    api.on_post = reject
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    transport = WindowMessageTransport(lambda _: {"pid": 100}, api=api)
    with pytest.raises(WindowTransportError, match="queue rejected"):
        transport.perform(10, "type", {"target_hwnd": 20, "text": "A", "delivery": "posted", "focus": False})
    assert len(api.posted) == 1
    assert not api.messages
