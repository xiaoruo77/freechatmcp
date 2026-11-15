"""Connect one selected saved device using a UIA-verified fixed button target.

No password field, global input, clipboard, focus-changing API, or auth dialog
is used. The receipt means a request was invoked; the caller must independently
find and verify the resulting ToDesk viewer before claiming a connection.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import ctypes
from ctypes import wintypes
import ntpath
import re
import math


class ToDeskConnectError(RuntimeError):
    def __init__(self, code, *, may_have_run=False, diagnostics=None):
        super().__init__("The saved-device connection could not be verified.")
        self.code = code
        self.may_have_run = may_have_run
        self.diagnostics = diagnostics or {}


class _COMApartment:
    """Balance only this invocation's successful COM initialization count."""
    def __init__(self, _api=None):
        self.owned = False
        if _api is None:
            try:
                _api = ctypes.WinDLL("ole32", use_last_error=True)
                _api.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
                _api.CoInitializeEx.restype = ctypes.c_long
                _api.CoUninitialize.argtypes = []
                _api.CoUninitialize.restype = None
            except Exception:
                raise ToDeskConnectError("saved_device_com_unavailable") from None
        self.api = _api

    def __enter__(self):
        try:
            result = int(self.api.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
        except Exception:
            raise ToDeskConnectError("saved_device_com_unavailable") from None
        # Normalize injected unsigned HRESULTs as well as native signed longs.
        result &= 0xffffffff
        if result in (0, 1):  # S_OK and S_FALSE each own one init count.
            self.owned = True
        elif result != 0x80010106:  # RPC_E_CHANGED_MODE: use the existing MTA.
            raise ToDeskConnectError("saved_device_com_unavailable")
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.owned:
            self.owned = False
            self.api.CoUninitialize()


class _WindowScope:
    def __init__(self, executable, allowed_devices):
        from .todesk_native import NativeToDesk
        self.native = NativeToDesk(executable, allowed_devices)
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.argtypes = []
        user.GetForegroundWindow.restype = wintypes.HWND
        self.user = user

    def inventory(self):
        return self.native.inventory()

    def verify(self, record):
        identity = (record["hwnd"], record["pid"], record["create_time"])
        for current in self.native.inventory():
            if (current["hwnd"], current["pid"], current["create_time"]) == identity:
                if (current["title"] == record["title"] and current["class_name"] == record["class_name"]
                        and current.get("viewer") is False):
                    return current
        raise ToDeskConnectError("saved_device_main_identity_changed")

    def foreground(self):
        return int(self.user.GetForegroundWindow() or 0)

    def minimized(self, record):
        return self.native._api.is_iconic(record["hwnd"])

    def physical_pixels(self):
        return self.native._api.physical_pixels()

    def restore(self, record):
        self.verify(record)
        self.native._api.restore_background(record["hwnd"])
        self.verify(record)

    def click_control(self, record, rect, verify_target):
        """Deliver one fixed saved-device control click to the exact main HWND."""
        if (record.get("title") != "ToDesk" or record.get("viewer") is not False
                or record.get("class_name") != "H-SMILE-FRAME" or not record.get("visible", True)):
            raise ToDeskConnectError("saved_device_target_stale")
        attempted = False
        try:
            for message, buttons in ((0x0200, 0), (0x0201, 1), (0x0202, 0)):
                current = self.verify(record)
                if verify_target() != rect:
                    raise ToDeskConnectError("saved_device_target_changed")
                sx = round((rect["left"] + rect["right"]) / 2)
                sy = round((rect["top"] + rect["bottom"]) / 2)
                x, y = self.native._api.map_point(0, record["hwnd"], sx, sy)
                if not (0 <= x < current["client_width"] and 0 <= y < current["client_height"]
                        and x <= 32767 and y <= 32767):
                    raise ToDeskConnectError("saved_device_target_changed")
                attempted = True
                self.native._api.send(record["hwnd"], message, buttons,
                                      (x & 0xffff) | ((y & 0xffff) << 16), 500)
                self.verify(record)
                if verify_target() != rect:
                    raise ToDeskConnectError("saved_device_target_changed")
        except Exception as exc:
            if attempted:
                raise ToDeskConnectError("saved_device_delivery_unknown", may_have_run=True) from None
            if isinstance(exc, ToDeskConnectError):
                raise
            raise ToDeskConnectError("saved_device_target_stale") from None


def _rect(value):
    try:
        result = {key: float(getattr(value, key)) for key in ("left", "top", "right", "bottom")}
        return result if (all(math.isfinite(v) for v in result.values()) and
                          result["right"] > result["left"] and result["bottom"] > result["top"]) else None
    except Exception:
        return None


class _UIA:
    """COM references are scoped to this connector invocation and owning thread."""
    def __init__(self):
        try:
            import comtypes.client
            self.module = comtypes.client.GetModule("UIAutomationCore.dll")
            self.client = comtypes.client.CreateObject(
                "{FF48DBA4-60EF-4201-AA87-54103EEF594E}", interface=self.module.IUIAutomation)
        except Exception:
            raise ToDeskConnectError("saved_device_uia_unavailable") from None
        self.elements = {}

    def close(self):
        # Release UIA references on their creating thread before COM teardown.
        self.elements.clear()
        self.client = None
        self.module = None

    def snapshot(self, record):
        self.elements.clear()
        root = self.client.ElementFromHandle(record["hwnd"])
        walker = self.client.ControlViewWalker
        pending = [(root, None, 0)]
        nodes = []
        while pending:
            if len(nodes) >= 1000:
                raise ToDeskConnectError("saved_device_tree_too_large")
            element, parent, depth = pending.pop()
            try:
                # Never inspect password Names, Values or their descendants.
                if bool(element.CurrentIsPassword):
                    continue
                if int(element.CurrentProcessId) != record["pid"]:
                    continue
                name = str(element.CurrentName)
                if len(name) > 1000:
                    name = ""
                token = len(nodes) + 1
                rect = _rect(element.CurrentBoundingRectangle)
                kind = int(element.CurrentControlType)
                patterns = []
                for prop, label in ((30031, "invoke"), (30036, "selection"), (30090, "legacy")):
                    value = element.GetCurrentPropertyValue(prop)
                    if getattr(value, "value", value) in (True, 1):
                        patterns.append(label)
                node = {"token": token, "parent": parent, "name": name,
                        "control_type": kind, "rect": rect, "patterns": patterns,
                        "enabled": bool(element.CurrentIsEnabled)}
                self.elements[token] = (element, record["hwnd"], record["pid"])
                # An empty Legacy default action is not permission to guess.
                if "legacy" in patterns:
                    try:
                        legacy = element.GetCurrentPattern(10018).QueryInterface(self.module.IUIAutomationLegacyIAccessiblePattern)
                        node["legacy_default_action"] = str(legacy.CurrentDefaultAction)[:100]
                    except Exception:
                        pass
                nodes.append(node)
                if depth < 25:
                    child = walker.GetFirstChildElement(element)
                    siblings = []
                    while child and len(siblings) < 1000:
                        siblings.append((child, token, depth + 1))
                        child = walker.GetNextSiblingElement(child)
                    pending.extend(reversed(siblings))
            except ToDeskConnectError:
                raise
            except Exception:
                raise ToDeskConnectError("saved_device_tree_unavailable") from None
        # Read values only after locating the explicit sibling device-code
        # label. Hidden/minimized roots can lack geometry; labels remain exact.
        code_parents = {node["parent"] for node in nodes
                        if node["name"] == "设备代码" and node["control_type"] == 50020
                        and node["parent"] is not None}
        for node in nodes:
            if node["control_type"] != 50004 or node["parent"] not in code_parents:
                continue
            element = self.elements[node["token"]][0]
            try:
                if bool(element.CurrentIsPassword) or int(element.CurrentProcessId) != record["pid"]:
                    continue
                pattern = element.GetCurrentPattern(10002).QueryInterface(self.module.IUIAutomationValuePattern)
                value = str(pattern.CurrentValue)
                if re.fullmatch(r"[0-9][0-9 -]{4,30}[0-9]", value):
                    node["device_code"] = value.replace(" ", "").replace("-", "")
            except Exception:
                continue
        return nodes

    def perform(self, token, method, record):
        if method != "legacy_remote_control":
            raise ToDeskConnectError("saved_device_pattern_unsupported")
        stored = self.elements.get(token)
        if not stored or stored[1:] != (record["hwnd"], record["pid"]):
            raise ToDeskConnectError("saved_device_target_stale")
        element = stored[0]
        ancestor = element
        for _ in range(64):
            if not ancestor:
                break
            if int(ancestor.CurrentNativeWindowHandle or 0) == record["hwnd"]:
                break
            ancestor = self.client.ControlViewWalker.GetParentElement(ancestor)
        else:
            ancestor = None
        if not ancestor or int(element.CurrentProcessId) != record["pid"] or bool(element.CurrentIsPassword):
            raise ToDeskConnectError("saved_device_target_stale")
        if int(element.CurrentControlType) != 50007 or not bool(element.CurrentIsEnabled):
            raise ToDeskConnectError("saved_device_target_stale")
        child = self.client.ControlViewWalker.GetFirstChildElement(element)
        labels = 0
        for _ in range(64):
            if not child:
                break
            if (not bool(child.CurrentIsPassword) and int(child.CurrentProcessId) == record["pid"]
                    and int(child.CurrentControlType) == 50020 and str(child.CurrentName) == "远程控制"):
                labels += 1
            child = self.client.ControlViewWalker.GetNextSiblingElement(child)
        if labels != 1 or child:
            raise ToDeskConnectError("saved_device_target_stale")
        pattern = element.GetCurrentPattern(10018).QueryInterface(self.module.IUIAutomationLegacyIAccessiblePattern)
        # The exact authorized tile is established by its bound label and
        # detail tree. An empty provider action label does not disable this API.
        try:
            pattern.DoDefaultAction()
        except Exception:
            raise ToDeskConnectError("saved_device_delivery_unknown", may_have_run=True) from None

    def bounds(self, token, record):
        stored = self.elements.get(token)
        if not stored or stored[1:] != (record["hwnd"], record["pid"]):
            raise ToDeskConnectError("saved_device_target_stale")
        element = stored[0]
        ancestor = element
        for _ in range(64):
            if not ancestor:
                break
            if int(ancestor.CurrentNativeWindowHandle or 0) == record["hwnd"]:
                rect = _rect(element.CurrentBoundingRectangle)
                if int(element.CurrentProcessId) == record["pid"] and not bool(element.CurrentIsPassword) and rect:
                    return rect
                break
            ancestor = self.client.ControlViewWalker.GetParentElement(ancestor)
        raise ToDeskConnectError("saved_device_target_stale")


class ToDeskSavedDeviceConnector:
    def __init__(self, executable, allowed_devices, *, _windows=None, _uia=None):
        self.executable = str(executable)
        self.allowed_devices = deepcopy(allowed_devices) if isinstance(allowed_devices, dict) else {}
        self._windows = _windows
        self._uia = _uia

    @staticmethod
    def _code(config):
        value = config.get("device_id") if isinstance(config, dict) else None
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{6,20}", value) is None:
            raise ToDeskConnectError("saved_device_config_invalid")
        return value

    @staticmethod
    def _target(nodes, expected_code, device_name):
        codes = [node for node in nodes if node.get("device_code") is not None]
        matches = [node for node in codes if node["device_code"] == expected_code]
        if not matches:
            raise ToDeskConnectError("saved_device_selected_code_mismatch")
        if len(matches) != 1 or len(codes) != 1:
            raise ToDeskConnectError("saved_device_code_ambiguous")
        by_token = {node["token"]: node for node in nodes}
        labels = [node for node in nodes if node.get("name") == "远程控制" and node.get("control_type") == 50020]
        if len(labels) != 1:
            raise ToDeskConnectError("saved_device_control_ambiguous" if labels else "saved_device_control_unavailable")
        def ancestors(node):
            result = []
            seen = set()
            while node.get("parent") in by_token and node["parent"] not in seen and len(result) < 64:
                seen.add(node["parent"])
                node = by_token[node["parent"]]
                result.append(node["token"])
            return result
        code_ancestors = ancestors(matches[0])
        control_ancestors = set(ancestors(labels[0]))
        common = next((by_token[token] for token in code_ancestors if token in control_ancestors), None)
        if common is None or common.get("control_type") != 50026 or common.get("parent") is None:
            raise ToDeskConnectError("saved_device_detail_unverified")
        detail_names = [node for node in nodes if node.get("name") == device_name
                        and common["token"] in ancestors(node)]
        if not detail_names:
            raise ToDeskConnectError("saved_device_detail_unverified")
        target = labels[0]
        if target.get("parent") in by_token and by_token[target["parent"]].get("control_type") == 50007:
            target = by_token[target["parent"]]
        if not target.get("enabled"):
            raise ToDeskConnectError("saved_device_control_disabled")
        if target.get("control_type") != 50007 or "legacy" not in target.get("patterns", []):
            raise ToDeskConnectError("saved_device_pattern_unsupported")
        rect = target.get("rect")
        if (not isinstance(rect, dict) or set(rect) != {"left", "top", "right", "bottom"}
                or any(type(value) not in {int, float} or not math.isfinite(value) for value in rect.values())
                or rect["right"] <= rect["left"] or rect["bottom"] <= rect["top"]):
            raise ToDeskConnectError("saved_device_target_stale")
        return target, "legacy_remote_control"

    def connect(self, device_name):
        if not isinstance(device_name, str) or device_name not in self.allowed_devices:
            raise ToDeskConnectError("device_not_allowed")
        expected_code = self._code(self.allowed_devices[device_name])
        if not ntpath.isabs(self.executable) or ntpath.basename(self.executable).casefold() != "todesk.exe":
            raise ToDeskConnectError("saved_device_config_invalid")
        windows = self._windows or _WindowScope(self.executable, self.allowed_devices)
        if self._uia is not None:
            return self._connect_selected(device_name, expected_code, windows, self._uia)
        with _COMApartment():
            uia = None
            try:
                uia = _UIA()
                return self._connect_selected(device_name, expected_code, windows, uia)
            finally:
                if uia is not None:
                    uia.close()
                    uia = None

    def _connect_selected(self, device_name, expected_code, windows, uia):
        context = getattr(windows, "physical_pixels", None)
        with context() if callable(context) else nullcontext():
            return self._connect_selected_physical(device_name, expected_code, windows, uia)

    def _connect_selected_physical(self, device_name, expected_code, windows, uia):
        candidates = [record for record in windows.inventory()
                      if record.get("title") == "ToDesk" and record.get("viewer") is False
                      and record.get("class_name", "H-SMILE-FRAME") == "H-SMILE-FRAME"]
        if not candidates:
            raise ToDeskConnectError("saved_device_main_unavailable")
        before = windows.foreground()
        restored = False
        invoked = False
        try:
            matching = []
            last_error = None
            for candidate in candidates:
                try:
                    current = windows.verify(deepcopy(candidate))
                    self._target(uia.snapshot(current), expected_code, device_name)
                    matching.append(current)
                except ToDeskConnectError as exc:
                    last_error = exc
            if len(matching) != 1:
                if len(matching) > 1:
                    raise ToDeskConnectError("saved_device_main_ambiguous")
                raise last_error or ToDeskConnectError("saved_device_main_unavailable")
            record = matching[0]
            if not record.get("visible", True) or windows.minimized(record):
                windows.restore(record)
                restored = True
            record = windows.verify(record)
            target, method = self._target(uia.snapshot(record), expected_code, device_name)
            windows.verify(record)
            invoked = True
            uia.perform(target["token"], method, record)
            after = windows.foreground()
            return {"ok": True, "connection_request_invoked": True,
                    "connection_verified": False, "device_name": device_name,
                    "request_method": method, "main_hwnd": record["hwnd"],
                    "restored_in_background": restored, "foreground_before": before,
                    "foreground_after": after, "foreground_changed": after != before,
                    "host_input_used": False, "foreground_input_used": False}
        except Exception as exc:
            after = windows.foreground()
            diagnostics = {"connection_request_invoked": invoked,
                           "restored_in_background": restored,
                           "foreground_before": before, "foreground_after": after,
                           "foreground_changed": after != before}
            if isinstance(exc, ToDeskConnectError):
                exc.diagnostics = diagnostics
                raise
            raise ToDeskConnectError("saved_device_delivery_unknown" if invoked else "saved_device_unavailable",
                                     may_have_run=invoked, diagnostics=diagnostics) from None
