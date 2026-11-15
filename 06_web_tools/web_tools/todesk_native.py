"""Bounded messages to a verified, already connected ToDesk viewer.

This backend never activates a window or synthesizes host input. Delivery only
means the local window accepted a message: the caller must capture and inspect
the remote result. Ambiguous delivery is never retried by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import ctypes
from ctypes import wintypes
import math
import ntpath
import os
import time
from collections.abc import Iterable, Mapping


WM_CLOSE = 0x0010
WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS = 0x0006, 0x0007, 0x0008
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E


class NativeToDeskError(ValueError):
    def __init__(self, code: str, message: str, *, may_have_run: bool = False):
        super().__init__(message)
        self.code = code
        self.may_have_run = may_have_run


def _path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value)))


def _integer(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or int(value) != value
            or not low <= value <= high):
        raise NativeToDeskError("invalid_input", f"{name} must be an integer from {low} to {high}")
    return int(value)


def _point(x, y):
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


class _Win32:
    """Small injectable seam; constructing this object does not enumerate UI."""

    def __init__(self):
        if os.name != "nt":
            raise NativeToDeskError("windows_required", "ToDesk native control requires Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "OpenDesktopW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseDesktop": ([wintypes.HANDLE], wintypes.BOOL),
            "EnumDesktopWindows": ([wintypes.HANDLE, self.callback, wintypes.LPARAM], wintypes.BOOL),
            "EnumChildWindows": ([wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsChild": ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)], wintypes.BOOL),
            "ChildWindowFromPointEx": ([wintypes.HWND, wintypes.POINT, wintypes.UINT], wintypes.HWND),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "MapWindowPoints": ([wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT], ctypes.c_int),
            "SendMessageTimeoutW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], wintypes.LPARAM),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            "MapVirtualKeyW": ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            "SetThreadDpiAwarenessContext": ([wintypes.HANDLE], wintypes.HANDLE),
        }
        for name, (args, restype) in signatures.items():
            try:
                fn = getattr(self.user, name)
            except AttributeError:
                raise NativeToDeskError("native_api_unavailable", f"Required Windows API is unavailable: {name}") from None
            fn.argtypes, fn.restype = args, restype

    @contextmanager
    def physical_pixels(self):
        old = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not old:
            raise NativeToDeskError("dpi_context_unavailable", "Cannot obtain physical client pixels")
        try:
            yield
        finally:
            if not self.user.SetThreadDpiAwarenessContext(old):
                raise NativeToDeskError("dpi_context_restore_failed", "Cannot restore this thread's DPI context")

    def desktop_windows(self, desktop):
        handle = self.user.OpenDesktopW(desktop, 0, False, 0x0041)
        if not handle:
            raise NativeToDeskError("desktop_unavailable", "Cannot enumerate the Default desktop")
        found = []
        callback = self.callback(lambda hwnd, _: found.append(int(hwnd)) or True)
        try:
            if not self.user.EnumDesktopWindows(handle, callback, 0):
                raise NativeToDeskError("desktop_enumeration_failed", "Cannot enumerate the Default desktop")
            return found
        finally:
            self.user.CloseDesktop(handle)

    def children(self, hwnd):
        found = []
        callback = self.callback(lambda child, _: found.append(int(child)) or True)
        self.user.EnumChildWindows(hwnd, callback, 0)
        return found

    def exists(self, hwnd):
        return bool(self.user.IsWindow(hwnd))

    def visible(self, hwnd):
        return bool(self.user.IsWindowVisible(hwnd))

    def is_child(self, root, hwnd):
        return bool(self.user.IsChild(root, hwnd))

    def is_iconic(self, hwnd):
        return bool(self.user.IsIconic(hwnd))

    def restore_background(self, hwnd):
        # SW_SHOWNOACTIVATE restores the saved placement without activation.
        # Keep that placement, move only this viewer to the bottom of Z order.
        self.user.ShowWindow(hwnd, 4)
        if not self.user.SetWindowPos(hwnd, wintypes.HWND(1), 0, 0, 0, 0, 0x0253):
            raise NativeToDeskError("viewer_restore_unknown", "Cannot confirm the background viewer placement", may_have_run=True)

    def root(self, hwnd):
        return int(self.user.GetAncestor(hwnd, 2) or 0)

    def foreground(self):
        return int(self.user.GetForegroundWindow() or 0)

    def pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise NativeToDeskError("window_unavailable", "Window identity is unavailable")
        return int(pid.value)

    def focus_window(self, hwnd):
        pid = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        info = _GUIThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        if not thread or not self.user.GetGUIThreadInfo(thread, ctypes.byref(info)):
            raise NativeToDeskError("input_focus_unavailable", "Cannot inspect the viewer's existing keyboard focus")
        return int(info.hwndFocus or 0)

    def hit_test(self, root, x, y):
        with self.physical_pixels():
            current = root
            for _ in range(32):
                cx, cy = self.map_point(root, current, x, y)
                # Read-only traversal, skipping invisible/disabled/transparent
                # children. This can legitimately identify the viewer root.
                child = int(self.user.ChildWindowFromPointEx(current, wintypes.POINT(cx, cy), 7) or 0)
                if not child:
                    raise NativeToDeskError("input_target_unavailable", "The viewer point has no input target")
                if child == current:
                    return current
                if not self.is_child(root, child):
                    raise NativeToDeskError("input_target_unavailable", "Hit testing left the verified viewer")
                current = child
        raise NativeToDeskError("ambiguous_input_target", "The viewer input hierarchy is too deep")

    def process(self, pid):
        import psutil
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                return {"exe": process.exe(), "create_time": process.create_time()}
        except (psutil.Error, OSError):
            raise NativeToDeskError("process_unavailable", "ToDesk process identity is unavailable") from None

    def title(self, hwnd):
        text = ctypes.create_unicode_buffer(self.user.GetWindowTextLengthW(hwnd) + 1)
        self.user.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def class_name(self, hwnd):
        text = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, text, len(text)):
            raise NativeToDeskError("window_unavailable", "Window class is unavailable")
        return text.value

    def client_size(self, hwnd):
        with self.physical_pixels():
            rect = wintypes.RECT()
            if not self.user.GetClientRect(hwnd, ctypes.byref(rect)):
                raise NativeToDeskError("window_unavailable", "Client geometry is unavailable")
            return rect.right - rect.left, rect.bottom - rect.top

    def map_point(self, source, target, x, y):
        with self.physical_pixels():
            point = wintypes.POINT(x, y)
            ctypes.set_last_error(0)
            result = self.user.MapWindowPoints(source, target, ctypes.byref(point), 1)
            if not result and ctypes.get_last_error():
                raise NativeToDeskError("coordinate_mapping_failed", "Cannot map client coordinates")
            return int(point.x), int(point.y)

    def scan_code(self, vk):
        return int(self.user.MapVirtualKeyW(vk, 0)) & 0xFF

    def send(self, hwnd, message, wparam, lparam, timeout_ms):
        # ToDesk's Sciter input handling runs through its owning message pump.
        # Queue only explicit input messages to this already verified HWND;
        # do not bypass that pump with synchronous input dispatch.
        queued_input = {WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK,
                        WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK,
                        WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK,
                        WM_MOUSEWHEEL, WM_MOUSEHWHEEL, WM_KEYDOWN, WM_KEYUP, WM_CHAR}
        if message in queued_input:
            if not self.user.PostMessageW(hwnd, message, wparam, lparam):
                raise NativeToDeskError("message_queue_failed", "The target window did not accept the queued input message")
            if message in {WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK,
                           WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK,
                           WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK,
                           WM_KEYDOWN, WM_KEYUP}:
                time.sleep(0.02)
            return 0
        result = ctypes.c_size_t()
        # SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT. No retry/fallback.
        if not self.user.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x23,
                                             timeout_ms, ctypes.byref(result)):
            raise NativeToDeskError("message_delivery_unknown", "Message delivery was not confirmed", may_have_run=True)
        return result.value


_KEYS = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21,
    "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24, "LEFT": 0x25,
    "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "INSERT": 0x2D, "DELETE": 0x2E,
}
_EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
_MODIFIERS = {"CTRL", "CONTROL", "ALT", "SHIFT", "WIN", "WINDOWS", "META", "CMD"}


class NativeToDesk:
    """Attach to allowlisted viewers and send bounded, capture-verified actions.

    Coordinates are physical pixels relative to the top-level client. Payloads:
    click(x,y,button='left',count=1), type(text), key(keys),
    scroll(x,y,vertical=0,horizontal=0), drag(x,y,to_x,to_y,duration_ms=250).
    Scroll values use signed Win32 wheel units (multiples of 120). Modifier
    chords are explicitly unsupported on the shared Default desktop.
    """

    def __init__(self, executable: str, allowed_devices: Iterable[str] | Mapping, *, _api=None,
                 _connector=None, _clock=None, _sleep=None, message_only_activation=False):
        if not isinstance(executable, str) or not ntpath.isabs(executable) or ntpath.basename(executable).lower() != "todesk.exe":
            raise NativeToDeskError("invalid_executable", "An absolute ToDesk.exe path is required")
        if isinstance(allowed_devices, str):
            raise NativeToDeskError("invalid_devices", "allowed_devices must contain device names")
        names = list(allowed_devices)
        if not names or any(not isinstance(name, str) or not name.strip() or name.lower() in {"todesk", "todesk.exe"} for name in names):
            raise NativeToDeskError("invalid_devices", "Explicit remote device names are required")
        if len({name.casefold() for name in names}) != len(names):
            raise NativeToDeskError("invalid_devices", "Device names must be unique")
        self.executable = executable
        self.allowed_devices = deepcopy(dict(allowed_devices)) if isinstance(allowed_devices, Mapping) else tuple(names)
        self._devices = {name.casefold(): name for name in names}
        self._api = _api if _api is not None else _Win32()
        self._connector = _connector
        self._clock = _clock or time.monotonic
        self._sleep = _sleep or time.sleep
        if not isinstance(message_only_activation, bool):
            raise NativeToDeskError("invalid_input", "message_only_activation must be a boolean")
        self.message_only_activation = message_only_activation

    def _read(self, hwnd):
        if not self._api.exists(hwnd) or self._api.root(hwnd) != hwnd:
            raise NativeToDeskError("window_unavailable", "The top-level ToDesk window is unavailable")
        pid = self._api.pid(hwnd)
        process = self._api.process(pid)
        if _path(process["exe"]) != _path(self.executable):
            raise NativeToDeskError("executable_mismatch", "The window does not belong to the configured ToDesk executable")
        width, height = self._api.client_size(hwnd)
        title = self._api.title(hwnd)
        return {"hwnd": hwnd, "pid": pid, "create_time": process["create_time"],
                "exe": process["exe"], "executable": process["exe"], "desktop": "Default", "title": title,
                "visible": bool(self._api.visible(hwnd)) if hasattr(self._api, "visible") else True,
                "class_name": self._api.class_name(hwnd), "client_width": width, "client_height": height,
                "viewer": title.casefold() in self._devices}

    def inventory(self) -> list[dict]:
        result = []
        for hwnd in self._api.desktop_windows("Default"):
            try:
                result.append(self._read(hwnd))
            except NativeToDeskError:
                continue
        return result

    def verify(self, record: dict) -> dict:
        if not isinstance(record, dict) or record.get("desktop") != "Default":
            raise NativeToDeskError("desktop_mismatch", "An explicitly recorded Default-desktop viewer is required")
        hwnd = _integer(record.get("hwnd"), "hwnd", 1, 2**64 - 1)
        _integer(record.get("pid"), "pid", 1, 2**32 - 1)
        ctime = record.get("create_time")
        if isinstance(ctime, bool) or not isinstance(ctime, (float, int)) or not math.isfinite(ctime) or ctime <= 0:
            raise NativeToDeskError("invalid_identity", "A process creation time is required")
        if (_path(record.get("exe", "")) != _path(self.executable)
                or _path(record.get("executable", record.get("exe", ""))) != _path(self.executable)):
            raise NativeToDeskError("executable_mismatch", "Recorded ToDesk executable does not match")
        if hwnd not in self._api.desktop_windows("Default"):
            raise NativeToDeskError("desktop_mismatch", "The viewer is no longer on the Default desktop")
        current = self._read(hwnd)
        if current["pid"] != record["pid"] or current["create_time"] != record["create_time"]:
            raise NativeToDeskError("process_identity_mismatch", "The viewer process identity changed")
        if not record.get("viewer") or not current["viewer"] or current["title"] != record.get("title"):
            raise NativeToDeskError("viewer_mismatch", "An exact allowlisted remote viewer is required")
        if "class_name" in record and current["class_name"] != record["class_name"]:
            raise NativeToDeskError("window_identity_mismatch", "The viewer window class changed")
        if not 1 <= current["client_width"] <= 32767 or not 1 <= current["client_height"] <= 32767:
            raise NativeToDeskError("invalid_geometry", "Viewer client dimensions are outside supported limits")
        return current

    def connect(self, device_name: str) -> dict:
        if not isinstance(device_name, str) or device_name.casefold() not in self._devices:
            raise NativeToDeskError("device_not_allowed", "This remote device is not configured")
        matches = [item for item in self.inventory() if item["viewer"] and item["title"].casefold() == device_name.casefold()]
        if len(matches) > 1:
            raise NativeToDeskError("ambiguous_viewer", "More than one connected viewer matches this device")
        if not matches:
            connector = self._connector
            if connector is None:
                from .todesk_connect import ToDeskSavedDeviceConnector
                connector = ToDeskSavedDeviceConnector(self.executable, self.allowed_devices)
            try:
                receipt = connector.connect(self._devices[device_name.casefold()])
            except Exception as exc:
                error = NativeToDeskError(getattr(exc, "code", "connection_request_unavailable"),
                                         "The saved-device connection request could not be confirmed",
                                         may_have_run=bool(getattr(exc, "may_have_run", False)))
                error.diagnostics = getattr(exc, "diagnostics", {})
                raise error from None
            if not isinstance(receipt, dict) or receipt.get("ok") is False or receipt.get("connection_request_invoked") is not True:
                raise NativeToDeskError("connection_request_unavailable", "No connected viewer exists; the saved-device request was not invoked")
            if receipt.get("foreground_changed") is True:
                raise NativeToDeskError("connection_foreground_changed", "The saved-device request changed host foreground; connection was not accepted", may_have_run=True)
            deadline = self._clock() + 9.0
            while True:
                matches = [item for item in self.inventory() if item["viewer"] and item["title"].casefold() == device_name.casefold()]
                if len(matches) > 1:
                    raise NativeToDeskError("ambiguous_viewer", "More than one viewer appeared after the saved-device request", may_have_run=True)
                if matches:
                    try:
                        connected = self.ensure_renderable(matches[0])
                    except NativeToDeskError as exc:
                        exc.may_have_run = True
                        raise
                    return {**connected, "attached_existing": False, "connection_request_invoked": True,
                            "connection_verified": True, "host_input_used": False}
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise NativeToDeskError("connection_not_established", "The saved-device request did not produce a verified viewer", may_have_run=True)
                self._sleep(min(0.2, remaining))
        return {**self.ensure_renderable(matches[0]), "attached_existing": True, "host_input_used": False}

    def ensure_renderable(self, record: dict) -> dict:
        current = self.verify(record)
        if self._api.is_iconic(current["hwnd"]):
            self.verify(current)
            self._api.restore_background(current["hwnd"])
            current = self.verify(current)
            if self._api.is_iconic(current["hwnd"]):
                raise NativeToDeskError("viewer_restore_unknown", "The verified viewer is still minimized", may_have_run=True)
        return current

    def _target(self, record, *, point=None, keyboard=False):
        """Use read-only hit/focus evidence for the known Sciter viewer."""
        root = record["hwnd"]
        candidates = []
        sciter_classes = set()
        for hwnd in self._api.children(root):
            if not self._api.exists(hwnd) or not self._api.is_child(root, hwnd) or self._api.pid(hwnd) != record["pid"]:
                continue
            name = self._api.class_name(hwnd).casefold()
            sciter_classes.add(name)
            if name != "scitervideoclass" and not any(token in name for token in ("canvas", "renderwidget", "renderwindow", "remoteview", "remotedesktop")):
                continue
            width, height = self._api.client_size(hwnd)
            if width > 0 and height > 0:
                candidates.append((width * height, hwnd, name))
        if record["class_name"] == "H-SMILE-FRAME" and {"scitervideoclass", "sciteruiclass"} <= sciter_classes:
            target = (self._api.focus_window(root) if keyboard else
                      self._api.hit_test(root, *point) if point is not None else None)
            if target == root:
                return root
            if (target and self._api.exists(target) and self._api.is_child(root, target)
                    and self._api.pid(target) == record["pid"]
                    and self._api.class_name(target).casefold() in {"scitervideoclass", "sciteruiclass", "scitermouseclass"}):
                return target
            raise NativeToDeskError("input_target_unavailable", "The known ToDesk viewer has no verified input target")
        if not candidates:
            raise NativeToDeskError("input_target_unavailable", "No recognized remote canvas exists in the verified viewer")
        candidates.sort(reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise NativeToDeskError("ambiguous_input_target", "The remote canvas is ambiguous")
        return candidates[0][1]

    def _action_target(self, record, action, args):
        if action in {"click", "scroll", "drag"}:
            point = (_integer(args.get("x"), "x", 0, record["client_width"] - 1),
                     _integer(args.get("y"), "y", 0, record["client_height"] - 1))
            target = self._target(record, point=point)
            if action == "drag":
                end = (_integer(args.get("to_x"), "to_x", 0, record["client_width"] - 1),
                       _integer(args.get("to_y"), "to_y", 0, record["client_height"] - 1))
                if self._target(record, point=end) != target:
                    raise NativeToDeskError("input_target_changed", "Drag endpoints target different viewer controls")
            return target
        return self._target(record, keyboard=True)

    def _coordinates(self, record, target, x, y, *, screen=False):
        x = _integer(x, "x", 0, record["client_width"] - 1)
        y = _integer(y, "y", 0, record["client_height"] - 1)
        tx, ty = self._api.map_point(record["hwnd"], target, x, y)
        width, height = self._api.client_size(target)
        if not 0 <= tx < width or not 0 <= ty < height:
            raise NativeToDeskError("point_outside_canvas", "Client coordinates lie outside the remote canvas")
        if screen:
            tx, ty = self._api.map_point(target, 0, tx, ty)
        if not -32768 <= tx <= 32767 or not -32768 <= ty <= 32767:
            raise NativeToDeskError("invalid_coordinates", "Mapped message coordinates exceed signed 16-bit limits")
        return tx, ty

    def _target_signature(self, record, target):
        return (self._api.pid(target), self._api.class_name(target), self._api.client_size(target),
                self._api.map_point(record["hwnd"], target, 0, 0))

    def _plan(self, record, action, args):
        if action == "close":
            return record["hwnd"], [(WM_CLOSE, 0, 0, 0)]
        target = self._action_target(record, action, args)
        plan = []
        if action == "type":
            text = args.get("text")
            if not isinstance(text, str) or "\x00" in text:
                raise NativeToDeskError("invalid_input", "text must be a string without NUL")
            try:
                encoded = text.encode("utf-16-le", errors="strict")
            except UnicodeEncodeError:
                raise NativeToDeskError("invalid_input", "text contains an unpaired surrogate") from None
            if len(encoded) > 32768:
                raise NativeToDeskError("invalid_input", "text exceeds 16384 UTF-16 code units")
            plan = [(WM_CHAR, int.from_bytes(encoded[i:i + 2], "little"), 1, 0) for i in range(0, len(encoded), 2)]
        elif action == "key":
            key = args.get("keys")
            if not isinstance(key, str):
                raise NativeToDeskError("invalid_input", "keys must be a virtual-key name string")
            name = key.upper().replace(" ", "")
            if "+" in name or name in _MODIFIERS:
                raise NativeToDeskError("modifier_chord_unsupported", "Background modifier chords cannot be guaranteed without changing another input queue; no messages were sent")
            vk = _KEYS.get(name)
            if vk is None and len(name) == 1 and ("A" <= name <= "Z" or "0" <= name <= "9"):
                vk = ord(name)
            if vk is None and name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
                vk = 0x70 + int(name[1:]) - 1
            if vk is None:
                raise NativeToDeskError("unsupported_key", "This virtual key is unsupported")
            lparam = 1 | (self._api.scan_code(vk) << 16) | ((vk in _EXTENDED_KEYS) << 24)
            plan = [(WM_KEYDOWN, vk, lparam, 0), (WM_KEYUP, vk, lparam | 0xC0000000, 0)]
        elif action == "click":
            x, y = self._coordinates(record, target, args.get("x"), args.get("y"))
            buttons = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, 1),
                       "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, 2),
                       "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, 16)}
            button = args.get("button", "left")
            if not isinstance(button, str) or button.lower() not in buttons:
                raise NativeToDeskError("invalid_input", "button must be left, right or middle")
            count = _integer(args.get("count", 1), "count", 1, 2)
            down, up, double, mask = buttons[button.lower()]
            point = _point(x, y)
            plan = [(WM_MOUSEMOVE, 0, point, 0), (down, mask, point, 0), (up, 0, point, 0)]
            if count == 2:
                plan += [(double, mask, point, 0), (up, 0, point, 0)]
        elif action == "scroll":
            vertical = _integer(args.get("vertical", 0), "vertical", -12000, 12000)
            horizontal = _integer(args.get("horizontal", 0), "horizontal", -12000, 12000)
            if not (vertical or horizontal) or vertical % 120 or horizontal % 120:
                raise NativeToDeskError("invalid_input", "Wheel values must be multiples of 120 with at least one nonzero axis")
            x, y = self._coordinates(record, target, args.get("x"), args.get("y"), screen=True)
            plan = [(message, (delta & 0xFFFF) << 16, _point(x, y), 0)
                    for message, delta in ((WM_MOUSEWHEEL, vertical), (WM_MOUSEHWHEEL, horizontal)) if delta]
        elif action == "drag":
            start = self._coordinates(record, target, args.get("x"), args.get("y"))
            end = self._coordinates(record, target, args.get("to_x"), args.get("to_y"))
            duration = _integer(args.get("duration_ms", 250), "duration_ms", 0, 5000)
            steps = max(1, min(64, duration // 16))
            plan = [(WM_MOUSEMOVE, 0, _point(*start), 0), (WM_LBUTTONDOWN, 1, _point(*start), 0)]
            for step in range(1, steps + 1):
                x = round(start[0] + (end[0] - start[0]) * step / steps)
                y = round(start[1] + (end[1] - start[1]) * step / steps)
                plan.append((WM_MOUSEMOVE, 1, _point(x, y), duration / steps / 1000))
            plan.append((WM_LBUTTONUP, 0, _point(*end), 0))
        else:
            raise NativeToDeskError("unsupported_action", "Unsupported native ToDesk action")
        return target, plan

    def _closed(self, record):
        if not self._api.exists(record["hwnd"]):
            return True
        try:
            pid = self._api.pid(record["hwnd"])
            return pid != record["pid"] or self._api.process(pid)["create_time"] != record["create_time"]
        except NativeToDeskError:
            return False

    def perform(self, record: dict, action: str, args: dict | None = None) -> dict:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise NativeToDeskError("invalid_input", "Action arguments must be an object")
        # No dispatch is needed to prove an owned handle disappeared. A reused
        # handle is never messaged; the original PID/creation-time pin is kept.
        if action == "close" and isinstance(record, dict):
            identity_valid = (record.get("desktop") == "Default" and record.get("viewer") is True
                              and isinstance(record.get("title"), str) and record["title"].casefold() in self._devices
                              and _path(record.get("exe", "")) == _path(self.executable)
                              and _path(record.get("executable", record.get("exe", ""))) == _path(self.executable))
            if identity_valid:
                _integer(record.get("hwnd"), "hwnd", 1, 2**64 - 1)
                _integer(record.get("pid"), "pid", 1, 2**32 - 1)
                ctime = record.get("create_time")
                if not isinstance(ctime, bool) and isinstance(ctime, (int, float)) and math.isfinite(ctime) and ctime > 0 and self._closed(record):
                    return {"ok": True, "status": "viewer_closed", "closed": True, "messages_sent": 0,
                            "remote_effect_verified": False, "host_input_used": False}
        current = self.verify(record)
        target, plan = self._plan(current, action, args)
        if self.message_only_activation and action != "close" and plan:
            return self._perform_with_activation(current, action, args, target, plan)
        return self._deliver_plan(current, action, args, target, plan)

    def _perform_with_activation(self, current, action, args, target, plan):
        """Opt-in notifications only; never changes the OS foreground/focus.

        A client may react to notifications by activating itself, so observe
        and report foreground changes and abort input if setup changes it.
        """
        before = self._api.foreground()
        if not before:
            raise NativeToDeskError("foreground_unavailable", "Cannot verify the current host foreground")
        root = current["hwnd"]
        if before == root:
            result = self._deliver_plan(current, action, args, target, plan)
            after = self._api.foreground()
            return {**result, "message_only_activation_used": False,
                    "foreground_before": before, "foreground_after": after,
                    "foreground_changed": before != after}
        if self._api.pid(before) == current["pid"]:
            raise NativeToDeskError("activation_same_process_foreground_denied", "Another ToDesk window owns foreground; viewer activation notifications were not sent")
        started = False
        setup_sent = cleanup_sent = 0
        cleanup_error = None
        cleanup_skipped = False
        result = None
        try:
            for message, wparam, lparam in ((WM_ACTIVATE, 1, before), (WM_SETFOCUS, 0, 0)):
                self.verify(current)
                if self._api.foreground() != before:
                    raise NativeToDeskError("foreground_changed", "Host foreground changed before viewer input", may_have_run=started)
                started = True
                self._api.send(root, message, wparam, lparam, 500)
                setup_sent += 1
                if self._api.foreground() != before:
                    raise NativeToDeskError("foreground_changed", "Viewer notifications changed host foreground; input was not dispatched", may_have_run=True)
            result = self._deliver_plan(current, action, args, target, plan)
        except Exception as exc:
            if not started:
                raise
            result = {"ok": False, "status": "delivery_unknown", "code": getattr(exc, "code", "native_delivery_failed"),
                      "messages_sent": 0, "may_have_run": True, "retry_safe": False,
                      "remote_effect_verified": False, "requires_capture_verification": True,
                      "host_input_used": False, "target_hwnd": target}
        finally:
            if started:
                try:
                    for message, wparam in ((WM_KILLFOCUS, 0), (WM_ACTIVATE, 0)):
                        self.verify(current)
                        foreground = self._api.foreground()
                        if not foreground:
                            raise NativeToDeskError("foreground_unavailable", "Cannot inspect foreground during notification cleanup")
                        if foreground == root:
                            cleanup_skipped = True
                            break
                        self._api.send(root, message, wparam, foreground if message == WM_ACTIVATE else 0, 500)
                        cleanup_sent += 1
                except Exception as exc:
                    cleanup_error = getattr(exc, "code", "activation_cleanup_unknown")
        after = self._api.foreground()
        if cleanup_error:
            result.update(ok=False, status="delivery_unknown", code=cleanup_error,
                          may_have_run=True, retry_safe=False, activation_cleanup_unknown=True)
        return {**result, "message_only_activation_used": started,
                "activation_notifications_sent": setup_sent, "activation_cleanup_sent": cleanup_sent,
                "activation_cleanup_skipped_viewer_foreground": cleanup_skipped,
                "foreground_before": before, "foreground_after": after,
                "foreground_changed": before != after}

    def _deliver_plan(self, current, action, args, target, plan):
        target_signature = self._target_signature(current, target) if action != "close" else None
        sent = 0
        attempted = False
        try:
            for message, wparam, lparam, delay in plan:
                latest = self.verify(current)
                if (latest["client_width"], latest["client_height"]) != (current["client_width"], current["client_height"]):
                    raise NativeToDeskError("geometry_changed", "Viewer client geometry changed during the action")
                if action != "close" and (not self._api.exists(target)
                        or (target != current["hwnd"] and not self._api.is_child(current["hwnd"], target))
                        or self._api.pid(target) != current["pid"] or self._action_target(latest, action, args) != target
                        or self._target_signature(latest, target) != target_signature):
                    raise NativeToDeskError("input_target_changed", "The remote input canvas changed")
                if delay:
                    time.sleep(delay)
                    # Delay must not create a stale-target dispatch window.
                    latest = self.verify(current)
                    if ((latest["client_width"], latest["client_height"]) != (current["client_width"], current["client_height"])
                            or not self._api.exists(target) or self._api.pid(target) != current["pid"]
                            or self._action_target(latest, action, args) != target or self._target_signature(latest, target) != target_signature):
                        raise NativeToDeskError("input_target_changed", "The remote input canvas changed")
                attempted = True
                self._api.send(target, message, wparam, lparam, 500)
                sent += 1
        except Exception as exc:
            if not attempted:
                raise
            return {"ok": False, "status": "delivery_unknown", "code": getattr(exc, "code", "native_delivery_failed"),
                    "messages_sent": sent, "may_have_run": True, "retry_safe": False,
                    "remote_effect_verified": False, "requires_capture_verification": True,
                    "host_input_used": False, "target_hwnd": target}
        result = {"ok": True, "status": "messages_delivered", "messages_sent": sent,
                  "remote_effect_verified": False, "requires_capture_verification": True,
                  "host_input_used": False, "target_hwnd": target}
        if action == "close":
            result["closed"] = self._closed(current)
            result["status"] = "viewer_closed" if result["closed"] else "close_requested"
        return result
