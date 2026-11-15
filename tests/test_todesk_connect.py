"""Saved-device connector boundary tests; no real COM or Windows UI is used."""
from copy import deepcopy
import json

import pytest

from web_tools.todesk_connect import ToDeskSavedDeviceConnector, ToDeskConnectError


EXE = r"D:\Apps\ToDesk\ToDesk.exe"
PRIVATE = "a-private-password-must-never-be-emitted"


class Windows:
    def __init__(self):
        self.main = {"hwnd": 123, "pid": 45, "create_time": 1.5, "title": "ToDesk",
                     "class_name": "H-SMILE-FRAME", "viewer": False, "exe": EXE}
        self.items = [self.main]
        self.is_minimized = False
        self.restores = []
        self.foreground_value = 999
        self.verify_error = False
        self.clicks = []
        self.click_error = None
        self.after_click = None

    def inventory(self):
        return deepcopy(self.items)

    def verify(self, record):
        if self.verify_error:
            raise ToDeskConnectError("saved_device_main_identity_changed")
        return deepcopy(next(item for item in self.items if item["hwnd"] == record["hwnd"]))

    def foreground(self):
        return self.foreground_value

    def minimized(self, record):
        return self.is_minimized

    def restore(self, record):
        self.restores.append(deepcopy(record))
        self.is_minimized = False
        next(item for item in self.items if item["hwnd"] == record["hwnd"])["visible"] = True

    def click_control(self, record, rect, verify_target):
        self.verify(record)
        assert verify_target() == rect
        self.clicks.append((deepcopy(record), deepcopy(rect)))
        if self.after_click:
            self.after_click()
        if self.click_error:
            raise self.click_error


class UIA:
    def __init__(self):
        self.nodes = [
            {"token": 1, "parent": None, "name": "ToDesk", "control_type": 50032},
            {"token": 2, "parent": 1, "name": "DEMO-NODE", "control_type": 50020},
            {"token": 3, "parent": 1, "name": "DEMO-NODE", "control_type": 50020},
            {"token": 4, "parent": 1, "name": "", "control_type": 50026},
            {"token": 5, "parent": 4, "name": "DEMO-NODE", "control_type": 50020},
            {"token": 6, "parent": 4, "name": "", "control_type": 50026},
            {"token": 7, "parent": 6, "name": "", "control_type": 50004, "device_code": "282828282"},
            {"token": 8, "parent": 4, "name": "", "control_type": 50007,
             "enabled": True, "patterns": ["selection", "legacy"], "legacy_default_action": "",
             "rect": {"left": 1207, "top": 548, "right": 1297, "bottom": 626}},
            {"token": 9, "parent": 8, "name": "远程控制", "control_type": 50020,
             "enabled": True, "patterns": ["legacy"], "legacy_default_action": ""},
        ]
        self.snapshots = []
        self.actions = []
        self.error = None
        self.after_action = None
        self.after_snapshot = None

    def snapshot(self, record):
        self.snapshots.append(deepcopy(record))
        if self.after_snapshot:
            self.after_snapshot()
        if record["hwnd"] != 123:
            return [{"token": 1, "parent": None, "name": "ToDesk", "control_type": 50032}]
        return deepcopy(self.nodes)

    def perform(self, token, method, record):
        assert token == 8 and method == "legacy_remote_control"
        self.actions.append((token, method, deepcopy(record)))
        if self.after_action:
            self.after_action()
        if self.error:
            raise self.error

    def bounds(self, token, record):
        return deepcopy(next(node["rect"] for node in self.nodes if node["token"] == token))


@pytest.fixture
def setup():
    windows, uia = Windows(), UIA()
    connector = ToDeskSavedDeviceConnector(EXE, {"DEMO-NODE": {"device_id": "282828282", "password": PRIVATE}},
                                            _windows=windows, _uia=uia)
    return connector, windows, uia


def test_selected_detail_code_uses_exact_authorized_legacy_tile_once(setup):
    connector, windows, uia = setup
    result = connector.connect("DEMO-NODE")
    assert result["ok"] and result["connection_request_invoked"] and not result["connection_verified"]
    assert result["request_method"] == "legacy_remote_control"
    assert uia.actions == [(8, "legacy_remote_control", windows.main)]
    assert not windows.clicks
    assert result["foreground_before"] == result["foreground_after"] == 999
    assert not result["foreground_changed"] and not result["host_input_used"]
    assert PRIVATE not in json.dumps(result) and "282828282" not in json.dumps(result)


@pytest.mark.parametrize("device", ["unknown", "282828282", PRIVATE, None, []])
def test_unconfigured_device_has_no_ui_access(setup, device):
    connector, windows, uia = setup
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect(device)
    assert caught.value.code == "device_not_allowed"
    assert PRIVATE not in str(caught.value) and not uia.snapshots and not uia.actions and not windows.restores


def test_mismatched_selected_code_does_not_select_device_or_restore(setup):
    connector, windows, uia = setup
    uia.nodes[6]["device_code"] = "111222333"
    windows.is_minimized = True
    with pytest.raises(ToDeskConnectError, match="saved-device") as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code == "saved_device_selected_code_mismatch"
    assert not caught.value.may_have_run and not uia.actions
    assert not windows.restores


@pytest.mark.parametrize("mutation,code", [
    (lambda nodes: nodes.append({**nodes[6], "token": 10}), "saved_device_code_ambiguous"),
    (lambda nodes: nodes.append({**nodes[8], "token": 10}), "saved_device_control_ambiguous"),
    (lambda nodes: nodes[7].update(enabled=False), "saved_device_control_disabled"),
    (lambda nodes: nodes[7].update(rect=None), "saved_device_target_stale"),
    (lambda nodes: nodes[6].update(parent=1), "saved_device_detail_unverified"),
    (lambda nodes: nodes[4].update(name="wrong device"), "saved_device_detail_unverified"),
])
def test_ambiguous_or_unverified_detail_is_rejected_without_action(setup, mutation, code):
    connector, windows, uia = setup
    mutation(uia.nodes)
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code == code and not uia.actions and not windows.restores


def test_empty_legacy_action_label_is_allowed_only_for_bound_remote_control_tile(setup):
    connector, windows, uia = setup
    uia.nodes[7].update(patterns=["invoke", "selection", "legacy"], legacy_default_action="")
    assert connector.connect("DEMO-NODE")["request_method"] == "legacy_remote_control"
    assert not windows.clicks and len(uia.actions) == 1


def test_minimized_main_is_restored_in_background_then_identity_and_detail_rechecked(setup):
    connector, windows, uia = setup
    windows.is_minimized = True
    result = connector.connect("DEMO-NODE")
    assert result["restored_in_background"]
    assert len(windows.restores) == 1 and len(uia.snapshots) == 2
    assert not result["foreground_changed"]


def test_minimized_empty_root_bounds_do_not_prevent_detail_selection(setup):
    connector, windows, original_uia = setup
    windows.is_minimized = True
    events = []
    class MinimizedRootUIA(UIA):
        def snapshot(self, record):
            events.append("snapshot")
            nodes = super().snapshot(record)
            # Real minimized provider fixture: the root has no rectangle,
            # children retain normal rectangles. The device-code label now
            # determines value reads independently of the empty root rectangle.
            nodes[0]["rect"] = None if windows.is_minimized else {"left": 749, "top": 400, "right": 1588, "bottom": 940}
            nodes[6]["rect"] = {"left": 1452, "top": 485, "right": 1545, "bottom": 508}
            return nodes
    uia = MinimizedRootUIA()
    connector._uia = uia
    restore = windows.restore
    def restore_first(record):
        events.append("restore")
        restore(record)
    windows.restore = restore_first
    result = connector.connect("DEMO-NODE")
    assert result["connection_request_invoked"] and result["restored_in_background"]
    assert events == ["snapshot", "restore", "snapshot"]
    assert not windows.clicks and len(uia.actions) == 1


def test_provider_foreground_change_is_reported_without_switching_back(setup):
    connector, windows, uia = setup
    uia.after_action = lambda: setattr(windows, "foreground_value", 123)
    result = connector.connect("DEMO-NODE")
    assert result["foreground_changed"] and result["foreground_before"] == 999 and result["foreground_after"] == 123
    assert windows.foreground_value == 123


def test_identity_change_after_snapshot_prevents_native_pattern(setup):
    connector, windows, uia = setup
    uia.after_snapshot = lambda: setattr(windows, "verify_error", True)
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code == "saved_device_main_identity_changed" and not uia.actions


def test_unknown_pattern_delivery_is_never_retried_or_false_success(setup):
    connector, windows, uia = setup
    uia.error = RuntimeError(PRIVATE)
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code == "saved_device_delivery_unknown" and caught.value.may_have_run
    assert not windows.clicks and len(uia.actions) == 1
    assert PRIVATE not in str(caught.value) and PRIVATE not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize("count", [0, 2])
def test_main_window_must_be_unique(setup, count):
    connector, windows, uia = setup
    windows.items = [windows.main] * count
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code in {"saved_device_main_ambiguous", "saved_device_main_unavailable"}
    assert not uia.actions and not windows.clicks and not windows.restores


def test_hidden_disconnected_viewer_does_not_make_main_window_ambiguous(setup):
    connector, windows, uia = setup
    windows.main["visible"] = True
    windows.items.append({**windows.main, "hwnd": 395276, "visible": False})
    windows.items.append({**windows.main, "hwnd": 395277, "class_name": "OldViewer"})
    result = connector.connect("DEMO-NODE")
    assert result["connection_request_invoked"] and result["main_hwnd"] == 123
    assert len(uia.actions) == 1 and uia.actions[0][2]["hwnd"] == 123
    assert not windows.clicks


def test_tray_hidden_main_is_selected_by_detail_tree_over_hidden_stale_viewer(setup):
    connector, windows, uia = setup
    windows.main["visible"] = False
    windows.items.append({**windows.main, "hwnd": 395276})
    result = connector.connect("DEMO-NODE")
    assert result["connection_request_invoked"] and result["main_hwnd"] == 123
    assert result["restored_in_background"] and windows.main["visible"]
    assert len(windows.restores) == 1 and windows.restores[0]["hwnd"] == 123
    assert [record["hwnd"] for record in uia.snapshots] == [123, 395276, 123]
    assert uia.actions[0][2]["hwnd"] == 123 and not windows.clicks


def test_config_validation_before_ui_access(setup):
    connector, _, uia = setup
    connector.allowed_devices["DEMO-NODE"]["device_id"] = PRIVATE
    with pytest.raises(ToDeskConnectError) as caught:
        connector.connect("DEMO-NODE")
    assert caught.value.code == "saved_device_config_invalid" and not uia.snapshots


@pytest.mark.parametrize("result,owned", [(0, True), (1, True), (-2147417850, False), (0x80010106, False)])
def test_com_apartment_balances_only_successful_own_count(result, owned):
    from web_tools.todesk_connect import _COMApartment
    class Ole:
        def __init__(self):
            self.initialized = []
            self.uninitialized = 0

        def CoInitializeEx(self, pointer, mode):
            self.initialized.append((pointer, mode))
            return result

        def CoUninitialize(self):
            self.uninitialized += 1

    api = Ole()
    with pytest.raises(RuntimeError, match="body failure"):
        with _COMApartment(_api=api):
            raise RuntimeError("body failure")
    assert api.initialized == [(None, 2)]
    assert api.uninitialized == int(owned)


def test_com_initialization_failure_does_not_uninitialize():
    from web_tools.todesk_connect import _COMApartment
    class Ole:
        def CoInitializeEx(self, pointer, mode):
            return -2147467259

        def CoUninitialize(self):
            pytest.fail("Failed initialization owns no COM count")

    with pytest.raises(ToDeskConnectError) as caught:
        with _COMApartment(_api=Ole()):
            pytest.fail("COM failure must prevent UI Automation")
    assert caught.value.code == "saved_device_com_unavailable"


@pytest.mark.parametrize("failure", [False, True])
def test_real_connector_scope_releases_uia_before_com_uninitialize(monkeypatch, failure):
    from web_tools import todesk_connect
    events = []
    class Apartment:
        def __enter__(self):
            events.append("initialize")

        def __exit__(self, *args):
            events.append("uninitialize")

    class ScopedUIA(UIA):
        def __init__(self):
            assert events == ["initialize"]
            super().__init__()
            if failure:
                self.nodes[6]["device_code"] = "111222333"

        def close(self):
            events.append("release_uia")

    monkeypatch.setattr(todesk_connect, "_COMApartment", Apartment)
    monkeypatch.setattr(todesk_connect, "_UIA", ScopedUIA)
    connector = ToDeskSavedDeviceConnector(EXE, {"DEMO-NODE": {"device_id": "282828282"}}, _windows=Windows())
    if failure:
        with pytest.raises(ToDeskConnectError):
            connector.connect("DEMO-NODE")
    else:
        assert connector.connect("DEMO-NODE")["connection_request_invoked"]
    assert events == ["initialize", "release_uia", "uninitialize"]


def test_bound_click_sends_only_three_exact_root_messages_with_guards(setup):
    from types import SimpleNamespace
    from web_tools.todesk_connect import _WindowScope
    _, windows, uia = setup
    record = {**windows.main, "client_width": 800, "client_height": 600}
    rect = uia.nodes[7]["rect"]
    events = []
    class API:
        def map_point(self, source, target, x, y):
            assert source == 0 and target == 123 and (x, y) == (1252, 587)
            return 52, 187

        def send(self, hwnd, message, buttons, point, timeout):
            events.append((hwnd, message, buttons, point, timeout))

        def __getattr__(self, name):
            pytest.fail("Unexpected API call: " + name)

    scope = _WindowScope.__new__(_WindowScope)
    scope.native = SimpleNamespace(_api=API())
    guards = []
    scope.verify = lambda pinned: guards.append("identity") or record
    def target():
        guards.append("bounds")
        return rect
    scope.click_control(record, rect, target)
    assert [(item[0], item[1], item[2]) for item in events] == [(123, 0x0200, 0), (123, 0x0201, 1), (123, 0x0202, 0)]
    assert all(item[3:] == (52 | (187 << 16), 500) for item in events)
    assert guards == ["identity", "bounds", "identity", "bounds"] * 3


def test_bound_click_changed_rect_before_dispatch_sends_nothing(setup):
    from types import SimpleNamespace
    from web_tools.todesk_connect import _WindowScope
    _, windows, uia = setup
    scope = _WindowScope.__new__(_WindowScope)
    scope.native = SimpleNamespace(_api=None)
    scope.verify = lambda pinned: {**windows.main, "client_width": 800, "client_height": 600}
    with pytest.raises(ToDeskConnectError) as caught:
        scope.click_control(windows.main, uia.nodes[7]["rect"], lambda: None)
    assert caught.value.code == "saved_device_target_changed" and not caught.value.may_have_run


def test_uia_reads_only_explicit_sibling_device_code_without_root_geometry():
    from types import SimpleNamespace
    from web_tools.todesk_connect import _UIA
    reads = []
    class Element:
        def __init__(self, name, kind, children=(), password=False, value=None):
            self.name, self.CurrentControlType = name, kind
            self.children = list(children)
            self.CurrentIsPassword = password
            self.CurrentProcessId = 45
            self.CurrentIsEnabled = True
            self.CurrentBoundingRectangle = None
            self.value = value
            self.parent = None
            for child in self.children:
                child.parent = self

        @property
        def CurrentName(self):
            assert not self.CurrentIsPassword
            return self.name

        def GetCurrentPropertyValue(self, property_id):
            return False

        def GetCurrentPattern(self, pattern_id):
            assert self.value is not None and pattern_id == 10002
            reads.append(self.name)
            return SimpleNamespace(QueryInterface=lambda _: SimpleNamespace(CurrentValue=self.value))

    code = Element("", 50004, value="282828282")
    group = Element("", 50026, [Element("设备代码", 50020), code])
    root = Element("ToDesk", 50032, [group, Element("unlabeled edit", 50004),
                                   Element(PRIVATE, 50004, password=True)])
    class Walker:
        def GetFirstChildElement(self, element):
            return element.children[0] if element.children else None

        def GetNextSiblingElement(self, element):
            siblings = element.parent.children
            index = siblings.index(element) + 1
            return siblings[index] if index < len(siblings) else None

    uia = _UIA.__new__(_UIA)
    uia.elements = {}
    uia.module = SimpleNamespace(IUIAutomationValuePattern=object())
    uia.client = SimpleNamespace(ElementFromHandle=lambda hwnd: root, ControlViewWalker=Walker())
    nodes = uia.snapshot({"hwnd": 123, "pid": 45})
    assert reads == [""]
    assert [node["device_code"] for node in nodes if "device_code" in node] == ["282828282"]
    assert nodes[0]["rect"] is None and PRIVATE not in json.dumps(nodes)


def test_150_percent_dpi_uses_one_physical_context_for_uia_and_messages(setup):
    from contextlib import contextmanager
    connector, windows, uia = setup
    state = {"physical": False, "entries": 0}
    @contextmanager
    def physical_pixels():
        assert not state["physical"]
        state["physical"] = True
        state["entries"] += 1
        try:
            yield
        finally:
            state["physical"] = False
    windows.physical_pixels = physical_pixels
    snapshot = uia.snapshot
    def physical_snapshot(record):
        assert state["physical"]
        nodes = snapshot(record)
        # Measured logical rect at 150%: [1207,548,1297,626]. UIA must
        # instead yield the physical rectangle in the shared thread context.
        nodes[7]["rect"] = {"left": 1810, "top": 822, "right": 1945, "bottom": 939}
        return nodes
    uia.snapshot = physical_snapshot
    def physical_bounds(token, record):
        assert state["physical"]
        return {"left": 1810, "top": 822, "right": 1945, "bottom": 939}
    uia.bounds = physical_bounds
    perform = uia.perform
    def physical_perform(token, method, record):
        assert state["physical"] and uia.bounds(token, record)["left"] == 1810
        return perform(token, method, record)
    uia.perform = physical_perform
    assert connector.connect("DEMO-NODE")["connection_request_invoked"]
    assert state == {"physical": False, "entries": 1}


@pytest.mark.parametrize("label,kind,allowed", [("远程控制", 50007, True), ("文件传输", 50007, False), ("远程控制", 50000, False)])
def test_real_uia_legacy_api_is_limited_to_exact_remote_control_listitem(label, kind, allowed):
    from types import SimpleNamespace
    from web_tools.todesk_connect import _UIA
    invoked = []
    class Pattern:
        @property
        def CurrentDefaultAction(self):
            pytest.fail("Empty action labels must not gate this authorized tile")

        def QueryInterface(self, interface):
            return self

        def DoDefaultAction(self):
            invoked.append(True)
    root = SimpleNamespace(CurrentNativeWindowHandle=123)
    text = SimpleNamespace(CurrentName=label, CurrentControlType=50020,
                           CurrentProcessId=45, CurrentIsPassword=False)
    tile = SimpleNamespace(CurrentNativeWindowHandle=0, CurrentProcessId=45,
                           CurrentIsPassword=False, CurrentControlType=kind,
                           CurrentIsEnabled=True, GetCurrentPattern=lambda pattern_id: Pattern())
    walker = SimpleNamespace(GetParentElement=lambda element: root,
                             GetFirstChildElement=lambda element: text,
                             GetNextSiblingElement=lambda element: None)
    uia = _UIA.__new__(_UIA)
    uia.elements = {8: (tile, 123, 45)}
    uia.client = SimpleNamespace(ControlViewWalker=walker)
    uia.module = SimpleNamespace(IUIAutomationLegacyIAccessiblePattern=object())
    if allowed:
        uia.perform(8, "legacy_remote_control", {"hwnd": 123, "pid": 45})
        assert invoked == [True]
    else:
        with pytest.raises(ToDeskConnectError):
            uia.perform(8, "legacy_remote_control", {"hwnd": 123, "pid": 45})
        assert not invoked
