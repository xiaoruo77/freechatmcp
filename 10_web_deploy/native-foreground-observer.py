"""Passive, bounded Windows foreground-event evidence with no input access.

Usage (the filename can be loaded with importlib.util.spec_from_file_location)::

    with ForegroundEventObserver(browser_pid) as observer:
        observer.mark("capture_start")
        # Perform the already-authorized browser action elsewhere.
        observer.mark("capture_end")
    evidence = observer.snapshot()

``mark`` returns its boundary record. Compare those timestamps with the final
``events`` list, rather than subtracting intermediate event counts: out-of-context
WinEvent callbacks may be queued until the observer thread pumps messages.
``timestamp_ms`` uses the Windows event's occurrence time, aligned to the start
of this observer; ``received_timestamp_ms`` uses Python's monotonic clock. Both
are relative milliseconds. Windows controls the occurrence clock's resolution.

The only hook is EVENT_SYSTEM_FOREGROUND / WINEVENT_OUTOFCONTEXT. No titles,
input state, input contents, window handles, or process identifiers are returned.
The target PID and window handles remain private and are used only for equality.
Importing this module is safe on non-Windows systems; starting is unsupported.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import re
import sys
import threading
import time
from typing import Any


EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
_PM_REMOVE = 0x0001
_QS_ALLINPUT = 0x04FF
_MWMO_INPUTAVAILABLE = 0x0004
_WAKE_MESSAGE = 0x8000 + 0x231
_WAIT_FAILED = 0xFFFFFFFF
_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,80}\Z")


def _relative_event_ms(event_tick: int, current_tick: int, start_tick: int) -> int:
    """Reconstruct a DWORD event clock across its approximately 49-day wrap."""
    age = ((current_tick & 0xFFFFFFFF) - event_tick) & 0xFFFFFFFF
    return max(0, current_tick - age - start_tick)


class _WindowsApi:
    """Local ctypes bindings; constructing this object is Windows-only."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )
        self.user32.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, self.callback_type,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ]
        self.user32.SetWinEventHook.restype = wintypes.HANDLE
        self.user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        self.user32.UnhookWinEvent.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self.user32.MsgWaitForMultipleObjectsEx.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD,
        ]
        self.user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel32.GetTickCount64.argtypes = []
        self.kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class ForegroundEventObserver:
    """One-shot foreground observer; always stop it or use a context manager.

    A dedicated message-pump thread owns and releases the WinEvent hook. It
    self-stops after at most 120 configured seconds, even if the caller forgets
    cleanup. Event and boundary storage is capped; total counts keep increasing.
    """

    def __init__(
        self,
        target_pid: int,
        *,
        max_duration_seconds: float = 120.0,
        max_events: int = 2048,
        max_marks: int = 256,
    ) -> None:
        if isinstance(target_pid, bool) or not isinstance(target_pid, int) or not 0 < target_pid <= 0xFFFFFFFF:
            raise ValueError("target_pid must be a positive DWORD")
        if not math.isfinite(max_duration_seconds) or not 0.1 <= max_duration_seconds <= 120.0:
            raise ValueError("max_duration_seconds must be between 0.1 and 120")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 10000:
            raise ValueError("max_events must be between 1 and 10000")
        if isinstance(max_marks, bool) or not isinstance(max_marks, int) or not 1 <= max_marks <= 2000:
            raise ValueError("max_marks must be between 1 and 2000")
        self._target_pid = target_pid
        self._max_duration_seconds = float(max_duration_seconds)
        self._max_events = max_events
        self._max_marks = max_marks
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: _WindowsApi | None = None
        self._callback: Any = None  # Strong reference lasts beyond UnhookWinEvent.
        self._hook: Any = None
        self._thread_id = 0
        self._start_ns: int | None = None
        self._end_ns: int | None = None
        self._start_tick = 0
        self._initial_hwnd: int | None = None
        self._last_hwnd: int | None = None
        self._final_hwnd: int | None = None
        self._initial_target: bool | None = None
        self._final_target: bool | None = None
        self._hook_installed = False
        self._unhook_succeeded: bool | None = None
        self._running = False
        self._stop_reason: str | None = None
        self._error: str | None = None
        self._event_count = 0
        self._transition_count = 0
        self._target_event_count = 0
        self._mark_count = 0
        self._events: list[dict[str, Any]] = []
        self._marks: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def __enter__(self) -> ForegroundEventObserver:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    def _elapsed_ms(self, now_ns: int | None = None) -> float:
        if self._start_ns is None:
            return 0.0
        return round(max(0, (now_ns if now_ns is not None else time.monotonic_ns()) - self._start_ns) / 1_000_000, 3)

    def _foreground(self) -> tuple[int, bool]:
        assert self._api is not None
        hwnd = int(self._api.user32.GetForegroundWindow() or 0)
        return hwnd, self._is_target(hwnd)

    def _is_target(self, hwnd: int) -> bool:
        assert self._api is not None
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        self._api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) == self._target_pid

    def start(self) -> ForegroundEventObserver:
        if not self.available:
            raise RuntimeError("foreground_observer_requires_windows")
        with self._lock:
            if self._thread is not None:
                if self._running:
                    return self
                raise RuntimeError("foreground_observer_is_one_shot")
            self._start_ns = time.monotonic_ns()
            self._thread = threading.Thread(target=self._run, name="native-foreground-event-observer", daemon=True)
            self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self._ready.wait(0.1):
            if time.monotonic() >= deadline:
                self.stop()
                raise RuntimeError("foreground_observer_start_timeout")
        with self._lock:
            error, running = self._error, self._running
        if error or not running:
            self.stop()
            raise RuntimeError(error or "foreground_observer_did_not_start")
        return self

    def _on_foreground(self, _hook: Any, event: int, hwnd: Any, _object: int, _child: int, _thread: int, event_tick: int) -> None:
        # Never let exceptions escape a ctypes callback into the Windows pump.
        try:
            if event != EVENT_SYSTEM_FOREGROUND:
                return
            assert self._api is not None
            current_tick = int(self._api.kernel32.GetTickCount64())
            received_ms = self._elapsed_ms()
            token = int(hwnd or 0)
            is_target = self._is_target(token)
            with self._lock:
                self._event_count += 1
                self._target_event_count += int(is_target)
                self._transition_count += int(self._last_hwnd is not None and token != self._last_hwnd)
                self._last_hwnd = token
                if len(self._events) < self._max_events:
                    self._events.append({
                        "timestamp_ms": _relative_event_ms(int(event_tick), current_tick, self._start_tick),
                        "received_timestamp_ms": received_ms,
                        "event_is_target_browser": is_target,
                    })
        except Exception:
            with self._lock:
                self._error = "foreground_callback_failed"
            self._stop_requested.set()

    def _drain_messages(self) -> None:
        assert self._api is not None
        message = wintypes.MSG()
        # A message flood must not postpone the stop/deadline check forever.
        for _ in range(256):
            if not self._api.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, _PM_REMOVE):
                break
            if message.message != _WAKE_MESSAGE:
                self._api.user32.DispatchMessageW(ctypes.byref(message))

    def _run(self) -> None:
        try:
            self._api = _WindowsApi()
            self._thread_id = int(self._api.kernel32.GetCurrentThreadId())
            # Create the owning thread's message queue before publishing ready.
            self._drain_messages()
            self._start_tick = int(self._api.kernel32.GetTickCount64())
            # Align the event clock with this monotonic origin, before hooking.
            with self._lock:
                self._start_ns = time.monotonic_ns()
            self._callback = self._api.callback_type(self._on_foreground)
            self._hook = self._api.user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                self._callback, 0, 0, WINEVENT_OUTOFCONTEXT,
            )
            if not self._hook:
                raise RuntimeError("foreground_hook_install_failed")
            initial_hwnd, initial_target = self._foreground()
            with self._lock:
                self._initial_hwnd = self._last_hwnd = initial_hwnd
                self._initial_target = initial_target
                self._hook_installed = self._running = True
            self._ready.set()
            deadline_ns = self._start_ns + int(self._max_duration_seconds * 1_000_000_000)
            while True:
                # Drain before testing stop so callbacks queued during the final
                # action can be delivered before the owner unhooks in finally.
                self._drain_messages()
                if self._stop_requested.is_set():
                    self._stop_reason = "error" if self._error else "requested"
                    break
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    self._stop_reason = "deadline"
                    break
                wait_ms = min(100, max(1, math.ceil(remaining_ns / 1_000_000)))
                result = self._api.user32.MsgWaitForMultipleObjectsEx(
                    0, None, wait_ms, _QS_ALLINPUT, _MWMO_INPUTAVAILABLE,
                )
                if result == _WAIT_FAILED:
                    raise RuntimeError("foreground_message_wait_failed")
        except Exception as error:
            with self._lock:
                self._error = str(error) if isinstance(error, RuntimeError) else "foreground_observer_failed"
                self._stop_reason = "error"
        finally:
            try:
                if self._hook and self._api is not None:
                    # Release on the same thread that registered the hook.
                    self._unhook_succeeded = bool(self._api.user32.UnhookWinEvent(self._hook))
                    if not self._unhook_succeeded:
                        self._error = "foreground_unhook_failed"
                        self._stop_reason = "error"
                    self._hook = None
                if self._api is not None and self._initial_hwnd is not None:
                    self._final_hwnd, self._final_target = self._foreground()
            except Exception:
                with self._lock:
                    self._error = "foreground_cleanup_failed"
                    self._stop_reason = "error"
            finally:
                with self._lock:
                    self._running = False
                    self._end_ns = time.monotonic_ns()
                self._ready.set()
                self._done.set()

    def mark(self, label: str) -> dict[str, Any]:
        """Record an action boundary; labels are short ASCII action names."""
        if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
            raise ValueError("label must be 1-80 ASCII action-name characters")
        with self._lock:
            if not self._running:
                raise RuntimeError("foreground_observer_not_running")
            boundary_ns = time.monotonic_ns()
            hwnd, is_target = self._foreground()
            boundary = {
                "label": label,
                "timestamp_ms": self._elapsed_ms(boundary_ns),
                "event_count": self._event_count,
                "foreground_transition_count": self._transition_count,
                "foreground_is_target_browser": is_target,
                "foreground_equals_initial": hwnd == self._initial_hwnd,
            }
            self._mark_count += 1
            if len(self._marks) < self._max_marks:
                self._marks.append(boundary)
            return dict(boundary)

    def snapshot(self) -> dict[str, Any]:
        """Copy bounded evidence; never expose HWNDs, PIDs, or window text."""
        with self._lock:
            complete = self._done.is_set()
            return {
                "observer": "win32_event_system_foreground",
                "available": self.available,
                "started": self._thread is not None,
                "running": self._running,
                "stopped": complete,
                "hook_installed": self._hook_installed,
                "unhook_succeeded": self._unhook_succeeded,
                "stop_reason": self._stop_reason,
                "error": self._error,
                "elapsed_ms": self._elapsed_ms(self._end_ns),
                "max_duration_ms": round(self._max_duration_seconds * 1000, 3),
                "initial_foreground_is_target_browser": self._initial_target,
                "final_foreground_is_target_browser": self._final_target,
                "initial_final_foreground_equal": (
                    self._initial_hwnd == self._final_hwnd
                    if complete and self._initial_hwnd is not None else None
                ),
                "event_count": self._event_count,
                "foreground_transition_count": self._transition_count,
                "target_browser_foreground_event_count": self._target_event_count,
                "other_foreground_event_count": self._event_count - self._target_event_count,
                "events_dropped": self._event_count - len(self._events),
                "mark_count": self._mark_count,
                "marks_dropped": self._mark_count - len(self._marks),
                "events": [dict(event) for event in self._events],
                "marks": [dict(mark) for mark in self._marks],
            }

    def stop(self) -> dict[str, Any]:
        """Wake the owner, wait for its finally/unhook, and return final evidence."""
        self._stop_requested.set()
        with self._lock:
            thread, api, thread_id = self._thread, self._api, self._thread_id
        if thread is not None and thread is not threading.current_thread():
            if api is not None and thread_id:
                api.user32.PostThreadMessageW(thread_id, _WAKE_MESSAGE, 0, 0)
            # No individual wait blocks for more than 100 ms. The native pump
            # also has a 100 ms maximum wait if posting a wake message fails.
            deadline = time.monotonic() + 5.0
            while not self._done.wait(0.1):
                if time.monotonic() >= deadline:
                    raise RuntimeError("foreground_observer_stop_timeout")
            thread.join(timeout=0.1)
        return self.snapshot()
