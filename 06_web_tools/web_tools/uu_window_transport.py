"""Bounded message transport for an explicitly owned private UU window.

This does not synthesize host input. A successful call means that Windows
accepted messages, not that UU or the remote computer acted on them. The caller
must verify the remote effect with a fresh private-window capture.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import ExitStack, contextmanager, nullcontext
import math
import os
import re
import time
from typing import Callable


class WindowTransportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def dpi_context():
    """Use physical window pixels temporarily, restoring this thread's mode.

    Private PrintWindow captures and input geometry must share the same DPI
    context. This never changes process-wide DPI behavior or another thread.
    """
    if os.name != "nt":
        raise WindowTransportError("windows_required", "Windows is required")
    user = ctypes.WinDLL("user32", use_last_error=True)
    try:
        switch = user.SetThreadDpiAwarenessContext
    except AttributeError:
        raise WindowTransportError("window_dpi_unavailable", "Per-thread DPI control is unavailable") from None
    switch.argtypes = [wintypes.HANDLE]
    switch.restype = wintypes.HANDLE
    previous = switch(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    if not previous:
        raise WindowTransportError("window_dpi_unavailable", "Cannot enter the physical-pixel DPI context")
    try:
        yield
    finally:
        if not switch(previous):
            raise WindowTransportError("window_dpi_restore_failed", "Cannot restore the actor thread's DPI context")


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_ACTIVATE, WM_SETFOCUS = 0x0006, 0x0007


@contextmanager
def _private_focus_context(api, root, target, record, check):
    """Attach only two threads already confined to the same private desktop."""
    desktop = record.get("desktop") if isinstance(record, dict) else None
    if not isinstance(desktop, str) or not re.fullmatch(r"Xiaoruo1Background(?:_[A-Za-z0-9_-]{1,48})?", desktop):
        raise WindowTransportError("private_focus_desktop_required", "Native focus requires an explicit verified private desktop")
    check()
    actor = api.current_thread()
    gui = api.window_thread(target)
    def verify_desktop():
        check()
        if api.window_thread(root) != gui or api.window_thread(target) != gui:
            raise WindowTransportError("private_focus_thread_mismatch", "Root and target must share one private GUI thread")
        if api.thread_desktop(actor) != desktop:
            raise WindowTransportError("private_focus_desktop_mismatch", "Native focus actor is outside the verified private desktop")
        if api.input_desktop() == desktop:
            raise WindowTransportError("private_focus_input_desktop_denied", "Native focus is confined to a non-input desktop")
        # GetThreadDesktop on a foreign process can return a handle that is
        # not usable in this process. Open our own named desktop handle and
        # enumerate its actual top-level HWNDs instead. The live root's GUI
        # thread cannot change desktops while that HWND exists.
        if not api.window_on_desktop(root, desktop):
            raise WindowTransportError("private_focus_desktop_mismatch", "The remote GUI root is not a member of the verified private desktop")
        try:
            gui_desktop = api.thread_desktop(gui)
        except WindowTransportError as exc:
            if exc.code != "private_focus_desktop_unknown":
                raise
            gui_desktop = None
        if gui_desktop is not None and gui_desktop != desktop:
            raise WindowTransportError("private_focus_desktop_mismatch", "The GUI thread desktop disagrees with its verified root membership")
    verify_desktop()
    previous = api.focus_state(gui)
    pid = record.get("pid")
    def allowed_previous(hwnd):
        return not hwnd or (api.exists(hwnd) and api.pid(hwnd) == pid and
                            api.window_thread(hwnd) == gui)
    if not all(allowed_previous(previous.get(name)) for name in ("focus", "active")):
        raise WindowTransportError("private_focus_state_mismatch", "Previous focus is outside the owned private GUI thread")
    # Do not attach to an already unresponsive UI queue.
    api.send(target, 0, 0, 0, 300)
    attached = False
    entered = False
    try:
        verify_desktop()
        if actor != gui:
            if not api.attach(actor, gui, True):
                raise WindowTransportError("private_focus_attach_failed", "Cannot attach the two private GUI input queues")
            attached = True
        entered = True
        verify_desktop()
        api.set_active(root)
        api.set_focus(target)
        observed = api.focus_state(gui)
        if observed.get("focus") != target or observed.get("active") != root:
            raise WindowTransportError("private_focus_not_acquired", "The private GUI did not accept native focus")
        verify_desktop()
        yield
    finally:
        try:
            if entered:
                verify_desktop()
                for name, method in (("focus", api.set_focus), ("active", api.set_active)):
                    old = previous.get(name) or 0
                    if not allowed_previous(old):
                        raise WindowTransportError("private_focus_restore_failed", "Previous private focus changed during the action")
                    method(old)
                restored = api.focus_state(gui)
                if any((restored.get(k) or 0) != (previous.get(k) or 0) for k in ("focus", "active")):
                    raise WindowTransportError("private_focus_restore_failed", "Private GUI focus was not restored")
                verify_desktop()
        finally:
            if attached and not api.attach(actor, gui, False):
                raise WindowTransportError("private_focus_detach_failed", "Cannot detach the private GUI input queues")


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def _number(value, name, low, high, *, integral=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WindowTransportError("invalid_window_input", f"{name} must be a finite number")
    if not low <= value <= high or (integral and int(value) != value):
        raise WindowTransportError("invalid_window_input", f"{name} is outside supported limits")
    return int(value) if integral else value


def _point_lparam(x, y):
    if not -32768 <= x <= 32767 or not -32768 <= y <= 32767:
        raise WindowTransportError("invalid_window_input", "Message coordinates exceed signed 16-bit limits")
    return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)


class _NativeAPI:
    dpi_context = staticmethod(dpi_context)

    def __init__(self):
        if os.name != "nt":
            raise WindowTransportError("windows_required", "Windows is required")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        signatures = {
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsChild": ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetThreadDesktop": ([wintypes.DWORD], wintypes.HANDLE),
            "OpenDesktopW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetUserObjectInformationW": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "OpenInputDesktop": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseDesktop": ([wintypes.HANDLE], wintypes.BOOL),
            "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)], wintypes.BOOL),
            "AttachThreadInput": ([wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
            "SetActiveWindow": ([wintypes.HWND], wintypes.HWND),
            "SetFocus": ([wintypes.HWND], wintypes.HWND),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ScreenToClient": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "ChildWindowFromPointEx": ([wintypes.HWND, wintypes.POINT, wintypes.UINT], wintypes.HWND),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "MapVirtualKeyW": ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "SendMessageTimeoutW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.user, name)
            method.argtypes, method.restype = args, result
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumChildWindows.argtypes = [wintypes.HWND, self._callback_type, wintypes.LPARAM]
        self.user.EnumChildWindows.restype = wintypes.BOOL
        self.user.EnumDesktopWindows.argtypes = [wintypes.HANDLE, self._callback_type, wintypes.LPARAM]
        self.user.EnumDesktopWindows.restype = wintypes.BOOL

    def exists(self, hwnd):
        return bool(self.user.IsWindow(hwnd))

    def current_thread(self):
        return self.kernel.GetCurrentThreadId()

    def window_thread(self, hwnd):
        pid = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread:
            raise WindowTransportError("private_window_stale", "Private GUI thread is unavailable")
        return thread

    def _desktop_name(self, handle):
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not handle or not self.user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise WindowTransportError("private_focus_desktop_unknown", "Cannot inspect the GUI thread desktop")
        return buffer.value

    def thread_desktop(self, thread):
        return self._desktop_name(self.user.GetThreadDesktop(thread))

    def window_on_desktop(self, hwnd, expected):
        # READOBJECTS | ENUMERATE only; no write or switch-desktop access.
        handle = self.user.OpenDesktopW(expected, 0, False, 0x0041)
        if not handle:
            raise WindowTransportError("private_focus_desktop_unknown", "Cannot open the verified private desktop for membership inspection")
        try:
            if self._desktop_name(handle) != expected:
                raise WindowTransportError("private_focus_desktop_mismatch", "Opened desktop name does not match the verified private desktop")
            found = []
            @self._callback_type
            def callback(candidate, _):
                if int(candidate) == hwnd:
                    found.append(True)
                    return False
                return True
            self.user.EnumDesktopWindows(handle, callback, 0)
            return bool(found)
        finally:
            self.user.CloseDesktop(handle)

    def input_desktop(self):
        handle = self.user.OpenInputDesktop(0, False, 1)
        try:
            return self._desktop_name(handle)
        finally:
            if handle: self.user.CloseDesktop(handle)

    def focus_state(self, thread):
        info = _GUIThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self.user.GetGUIThreadInfo(thread, ctypes.byref(info)):
            raise WindowTransportError("private_focus_state_unknown", "Cannot inspect private GUI focus")
        return {"active": int(info.hwndActive or 0), "focus": int(info.hwndFocus or 0)}

    def attach(self, actor, gui, enabled):
        return bool(self.user.AttachThreadInput(actor, gui, enabled))

    def set_active(self, hwnd):
        self.user.SetActiveWindow(hwnd or None)

    def set_focus(self, hwnd):
        self.user.SetFocus(hwnd or None)

    def private_focus(self, root, target, record, check):
        return _private_focus_context(self, root, target, record, check)

    def pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise WindowTransportError("private_window_stale", "Window process is unavailable")
        return pid.value

    def is_child(self, root, child):
        return bool(self.user.IsChild(root, child))

    def rect(self, hwnd, client=False):
        rect = wintypes.RECT()
        method = self.user.GetClientRect if client else self.user.GetWindowRect
        if not method(hwnd, ctypes.byref(rect)):
            raise WindowTransportError("private_window_stale", "Window geometry is unavailable")
        return rect.left, rect.top, rect.right, rect.bottom

    def screen_to_client(self, hwnd, x, y):
        point = wintypes.POINT(int(x), int(y))
        if not self.user.ScreenToClient(hwnd, ctypes.byref(point)):
            raise WindowTransportError("private_window_stale", "Window coordinate mapping failed")
        return point.x, point.y

    def child_at(self, hwnd, x, y):
        # Skip invisible, disabled, and transparent children; never use the
        # input desktop's WindowFromPoint, which could select a user's window.
        return int(self.user.ChildWindowFromPointEx(hwnd, wintypes.POINT(x, y), 7) or hwnd)

    def describe(self, hwnd):
        cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
        self.user.GetClassNameW(hwnd, cls, len(cls))
        self.user.GetWindowTextW(hwnd, title, len(title))
        return {"hwnd": hwnd, "pid": self.pid(hwnd), "class_name": cls.value,
                "title": title.value, "rect": list(self.rect(hwnd)),
                "client_rect": list(self.rect(hwnd, True)),
                "visible": bool(self.user.IsWindowVisible(hwnd))}

    def children(self, root):
        result = []
        @self._callback_type
        def callback(hwnd, _):
            if len(result) >= 256:
                return False
            result.append(int(hwnd))
            return True
        self.user.EnumChildWindows(root, callback, 0)
        return result

    def send(self, hwnd, message, wparam, lparam, timeout):
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        # ABORTIFHUNG | BLOCK | ERRORONEXIT, bounded per message.
        accepted = self.user.SendMessageTimeoutW(hwnd, message, wparam, lparam,
                                                0x0023, timeout, ctypes.byref(result))
        if not accepted:
            raise WindowTransportError("window_message_not_delivered",
                                       f"Window message delivery failed (Windows error {ctypes.get_last_error()})")
        return result.value

    def scan(self, virtual_key):
        return self.user.MapVirtualKeyW(virtual_key, 0)

    def post(self, hwnd, message, wparam, lparam):
        ctypes.set_last_error(0)
        if not self.user.PostMessageW(hwnd, message, wparam, lparam):
            raise WindowTransportError("window_message_not_posted",
                                       f"Window message queue rejected input (Windows error {ctypes.get_last_error()})")

    def resize(self, hwnd, width, height):
        # NOMOVE | NOZORDER | NOACTIVATE. This never activates or restores a
        # different desktop's window.
        if not self.user.SetWindowPos(hwnd, None, 0, 0, width, height, 0x0016):
            raise WindowTransportError("private_window_resize_failed", "Private window resize failed")


_KEYS = {"ENTER": 0x0D, "RETURN": 0x0D, "TAB": 9, "BACKSPACE": 8,
         "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20, "DELETE": 0x2E,
         "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21,
         "PAGEDOWN": 0x22, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27,
         "DOWN": 0x28, "CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12,
         "SHIFT": 0x10}
_KEYS.update({f"F{i}": 0x6F + i for i in range(1, 25)})
_KEYS.update({chr(i): i for i in range(ord("A"), ord("Z") + 1)})
_KEYS.update({str(i): ord(str(i)) for i in range(10)})


class WindowMessageTransport:
    """Use ``perform(root_hwnd, action, args)`` with a private-root validator.

    ``validate_window(root_hwnd)`` must revalidate private desktop membership
    and process creation identity. It returns a record containing ``pid`` or
    True. Coordinates are root *window screenshot* pixels, not remote desktop
    pixels. ``target_hwnd`` can select one inspected same-PID child explicitly.
    """
    def __init__(self, validate_window: Callable, *, api=None):
        self.validate_window = validate_window
        self.api = api if api is not None else _NativeAPI()
        self._last_target = {}

    def _validate(self, root, pid=None, target=None):
        if isinstance(root, bool) or not isinstance(root, int) or root <= 0:
            raise WindowTransportError("private_window_required", "Use one explicit private window HWND")
        record = self.validate_window(root)
        if not record or not self.api.exists(root):
            raise WindowTransportError("private_window_required", "Private window ownership was not verified")
        current_pid = self.api.pid(root)
        if (pid is not None and current_pid != pid) or (isinstance(record, dict) and record.get("pid") != current_pid):
            raise WindowTransportError("private_window_stale", "Private window process changed")
        if target is not None:
            if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
                raise WindowTransportError("private_child_required", "Use an inspected child HWND")
            if not self.api.exists(target) or self.api.pid(target) != current_pid or (target != root and not self.api.is_child(root, target)):
                raise WindowTransportError("private_child_required", "Input target is not a same-process child of the private window")
        return current_pid

    def _send(self, root, pid, target, msg, wp=0, lp=0):
        self._validate(root, pid, target)
        remaining = getattr(self, "_deadline", time.monotonic()+8)-time.monotonic()
        if remaining <= 0:
            raise WindowTransportError("window_message_timeout", "Window input exceeded its action time budget")
        self.api.send(target, msg, wp, lp, max(1, min(300, int(remaining*1000))))

    def _keyboard_message(self, root, pid, target, msg, wp, lp, args):
        if args.get("delivery", "sent") == "posted":
            self._validate(root, pid, target)
            if time.monotonic() >= self._deadline:
                raise WindowTransportError("window_message_timeout", "Window input exceeded its action time budget")
            self.api.post(target, msg, wp, lp)
            self._posted_targets.add((root, pid, target))
        else:
            self._send(root, pid, target, msg, wp, lp)

    def _wait_posted(self):
        if not self._posted_targets:
            return
        # Allow the application's normal event loop to consume queued keys
        # while its private focus context is still held. A sent WM_NULL is
        # only a responsiveness probe: it is not proof of posted-queue drain.
        remaining = self._deadline-time.monotonic()
        if remaining <= 0:
            raise WindowTransportError("window_message_timeout", "Window input exceeded its action time budget")
        time.sleep(min(.05, remaining))
        deadline = min(self._deadline, time.monotonic()+.05)
        for root, pid, target in self._posted_targets:
            self._validate(root, pid, target)
            timeout = int((deadline-time.monotonic())*1000)
            if timeout <= 0:
                break
            self.api.send(target, 0, 0, 0, min(50, timeout))

    def _context(self):
        return self.api.dpi_context() if hasattr(self.api, "dpi_context") else nullcontext()

    def inspect(self, root):
        with self._context():
            return self._inspect(root)

    def _inspect(self, root):
        pid = self._validate(root)
        result = self.api.describe(root)
        children = []
        for child in self.api.children(root):
            self._validate(root, pid)
            if self.api.exists(child) and self.api.pid(child) == pid and self.api.is_child(root, child):
                item = self.api.describe(child)
                if item["visible"]:
                    children.append(item)
        return {"ok": True, "window": result, "children": children,
                "coordinates": "root_window_screenshot_pixels", "foreground_input_used": False}

    def _point(self, root, pid, args, *, x_key="x", y_key="y", chosen=None):
        self._validate(root, pid)
        left, top, right, bottom = self.api.rect(root)
        width, height = right-left, bottom-top
        if width < 1 or height < 1 or width > 8192 or height > 8192:
            raise WindowTransportError("invalid_window_geometry", "Private window dimensions are unsupported")
        x = int(_number(args.get(x_key), x_key, 0, width-1))
        y = int(_number(args.get(y_key), y_key, 0, height-1))
        sx, sy = left+x, top+y
        target = chosen if chosen is not None else args.get("target_hwnd", root)
        self._validate(root, pid, target)
        if chosen is None and "target_hwnd" not in args:
            for _ in range(16):
                cx, cy = self.api.screen_to_client(target, sx, sy)
                cl, ct, cr, cb = self.api.rect(target, True)
                if not cl <= cx < cr or not ct <= cy < cb:
                    raise WindowTransportError("nonclient_input_denied", "Input point is outside the private client content")
                child = self.api.child_at(target, cx, cy)
                if child == target:
                    break
                self._validate(root, pid, child)
                target = child
            else:
                raise WindowTransportError("private_child_required", "Child-window nesting exceeds supported depth")
        cx, cy = self.api.screen_to_client(target, sx, sy)
        cl, ct, cr, cb = self.api.rect(target, True)
        if not cl <= cx < cr or not ct <= cy < cb:
            raise WindowTransportError("nonclient_input_denied", "Input point is outside the selected private child")
        return target, cx, cy, sx, sy

    def _focus(self, root, pid, target, args):
        if args.get("focus", True):
            if args.get("focus_mode", "messages") == "native_private":
                if not hasattr(self.api, "private_focus"):
                    raise WindowTransportError("private_focus_unavailable", "Native private focus is unavailable")
                record = self.validate_window(root)
                self._focus_stack.enter_context(self.api.private_focus(root, target, record,
                                                lambda: self._validate(root, pid, target)))
            else:
                self._send(root, pid, root, WM_ACTIVATE, 1)
                self._send(root, pid, target, WM_SETFOCUS)

    def perform(self, root, action, arguments=None):
        with self._context(), ExitStack() as stack:
            self._focus_stack = stack
            self._posted_targets = set()
            try:
                return self._perform(root, action, arguments)
            finally:
                try:
                    self._wait_posted()
                finally:
                    self._focus_stack = None

    def _perform(self, root, action, arguments=None):
        args = arguments or {}
        if not isinstance(args, dict):
            raise WindowTransportError("invalid_window_input", "Arguments must be an object")
        if action == "inspect":
            if args:
                raise WindowTransportError("invalid_window_input", "Inspect does not accept input arguments")
            return self._inspect(root)
        fields = {
            "click": {"x", "y", "button", "count", "target_hwnd", "focus"},
            "type": {"text", "target_hwnd", "focus", "delivery"},
            "key": {"keys", "target_hwnd", "focus", "delivery"},
            "scroll": {"x", "y", "vertical", "horizontal", "target_hwnd", "focus"},
            "drag": {"x", "y", "to_x", "to_y", "steps", "duration_ms", "target_hwnd", "focus"},
            "resize": {"width", "height"},
        }
        for name in ("click", "type", "key", "scroll", "drag"):
            fields[name].add("focus_mode")
        if action not in fields or set(args)-fields[action]:
            raise WindowTransportError("invalid_window_input", "Unsupported action or arguments")
        if "focus" in args and not isinstance(args["focus"], bool):
            raise WindowTransportError("invalid_window_input", "focus must be a boolean")
        if args.get("focus_mode", "messages") not in {"messages", "native_private"}:
            raise WindowTransportError("invalid_window_input", "Unsupported private focus mode")
        if args.get("delivery", "sent") not in {"sent", "posted"}:
            raise WindowTransportError("invalid_window_input", "Unsupported keyboard message delivery")
        if args.get("delivery") == "posted" and not hasattr(self.api, "post"):
            raise WindowTransportError("posted_window_input_unavailable", "Posted private keyboard input is unavailable")
        pid = self._validate(root)
        self._deadline = time.monotonic()+8
        target = args.get("target_hwnd", self._last_target.get(root, (pid, root))[1])
        if action == "resize":
            width = _number(args.get("width"), "width", 128, 8192, integral=True)
            height = _number(args.get("height"), "height", 128, 8192, integral=True)
            if width*height > 32_000_000:
                raise WindowTransportError("invalid_window_input", "Resize exceeds pixel limit")
            self._validate(root, pid)
            self.api.resize(root, width, height)
            target = root
        elif action in {"click", "scroll", "drag"}:
            target, x, y, sx, sy = self._point(root, pid, args)
            lp = _point_lparam(x, y)
            if action == "click":
                count = _number(args.get("count", 1), "count", 1, 2, integral=True)
                button = args.get("button", "left")
                mapping = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, 1),
                           "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, 2),
                           "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, 0x10)}
                if button not in mapping:
                    raise WindowTransportError("invalid_window_input", "Unsupported mouse button")
                down, up, double, mask = mapping[button]
                self._focus(root, pid, target, args)
                self._send(root, pid, target, WM_MOUSEMOVE, 0, lp)
                self._send(root, pid, target, down, mask, lp)
                try:
                    self._send(root, pid, target, up, 0, lp)
                    if count == 2:
                        self._send(root, pid, target, double, mask, lp)
                        self._send(root, pid, target, up, 0, lp)
                except Exception:
                    try: self._send(root, pid, target, up, 0, lp)
                    except Exception: pass
                    raise
            elif action == "scroll":
                vertical = _number(args.get("vertical", 0), "vertical", -1200, 1200, integral=True)
                horizontal = _number(args.get("horizontal", 0), "horizontal", -1200, 1200, integral=True)
                if not vertical and not horizontal:
                    raise WindowTransportError("invalid_window_input", "At least one wheel delta is required")
                self._focus(root, pid, target, args)
                for message, delta in ((WM_MOUSEWHEEL, vertical), (WM_MOUSEHWHEEL, horizontal)):
                    if delta:
                        self._send(root, pid, target, message, (delta & 0xFFFF) << 16, _point_lparam(sx, sy))
            else:
                steps = _number(args.get("steps", 12), "steps", 2, 60, integral=True)
                duration = _number(args.get("duration_ms", 300), "duration_ms", 0, 1500, integral=True)
                end = self._point(root, pid, args, x_key="to_x", y_key="to_y", chosen=target)
                ex, ey = end[1], end[2]
                self._focus(root, pid, target, args)
                self._send(root, pid, target, WM_MOUSEMOVE, 0, lp)
                self._send(root, pid, target, WM_LBUTTONDOWN, 1, lp)
                try:
                    for i in range(1, steps+1):
                        nx, ny = round(x+(ex-x)*i/steps), round(y+(ey-y)*i/steps)
                        lp = _point_lparam(nx, ny)
                        self._send(root, pid, target, WM_MOUSEMOVE, 1, lp)
                        if duration:
                            time.sleep(duration/steps/1000)
                finally:
                    self._send(root, pid, target, WM_LBUTTONUP, 0, lp)
        else:
            self._validate(root, pid, target)
            if action == "type":
                text = args.get("text")
                if not isinstance(text, str) or not text or len(text) > 2000 or "\0" in text:
                    raise WindowTransportError("invalid_window_input", "text must contain 1–2000 non-NUL characters")
                try: encoded = text.replace("\r\n", "\n").replace("\n", "\r").encode("utf-16-le")
                except UnicodeEncodeError:
                    raise WindowTransportError("invalid_window_input", "text contains an isolated Unicode surrogate") from None
                self._focus(root, pid, target, args)
                for index in range(0, len(encoded), 2):
                    self._keyboard_message(root, pid, target, WM_CHAR,
                                           int.from_bytes(encoded[index:index+2], "little"), 1, args)
            else:
                keys = args.get("keys")
                if isinstance(keys, str): keys = keys.split("+")
                if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(k, str) for k in keys):
                    raise WindowTransportError("invalid_window_input", "keys must be one chord of 1–5 supported keys")
                names = [k.strip().upper() for k in keys]
                if len(set(names)) != len(names) or any(k not in _KEYS for k in names):
                    raise WindowTransportError("invalid_window_input", "Unsupported or repeated key in chord")
                codes = [_KEYS[k] for k in names]
                if len(set(codes)) != len(codes):
                    raise WindowTransportError("invalid_window_input", "Repeated virtual key in chord")
                self._focus(root, pid, target, args)
                held = []
                alt = 0x12 in codes
                try:
                    for code in codes:
                        scan = self.api.scan(code) & 0xFF
                        extended = code in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
                        self._keyboard_message(root, pid, target, WM_SYSKEYDOWN if alt else WM_KEYDOWN,
                                   code, 1 | (scan << 16) | ((1 << 24) if extended else 0) |
                                   ((1 << 29) if alt and held else 0), args)
                        held.append(code)
                finally:
                    for code in reversed(held):
                        scan = self.api.scan(code) & 0xFF
                        extended = code in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
                        self._keyboard_message(root, pid, target, WM_SYSKEYUP if alt else WM_KEYUP,
                                   code, 1 | (scan << 16) | 0xC0000000 |
                                   ((1 << 24) if extended else 0) | ((1 << 29) if alt else 0), args)
        self._last_target[root] = (pid, target)
        return {"ok": True, "status": "dispatched", "action": action,
                "hwnd": root, "target_hwnd": target,
                "input_delivery": "posted_window_messages" if args.get("delivery") == "posted" else "window_messages",
                "remote_effect_verified": False,
                "foreground_input_used": False}
