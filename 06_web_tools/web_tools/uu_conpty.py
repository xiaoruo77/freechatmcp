from __future__ import annotations

"""A windowless, bounded Windows pseudoconsole transport.

This module does not know about UU accounts, devices, or commands.  It starts
only the supplied argument vector (without a shell), keeps terminal VT output
in memory, and owns the process tree that it creates.  Callers must clean up
any *remote* persistent terminal separately before closing this local channel.
"""

import codecs
import math
import os
import subprocess
import threading
import time
from collections.abc import Sequence


_BUFFER_LIMIT = 2 * 1024 * 1024


class PseudoConsoleError(OSError):
    """A native transport error whose message contains no command arguments."""

    def __init__(self, operation: str, code: int):
        self.operation = operation
        super().__init__(code, f"ConPTY {operation} failed (Windows error {code})")
        self.winerror = code


class _OutputBuffer:
    """One-reader UTF-8 decoder and a byte-bounded, consumable text buffer."""

    def __init__(self, limit: int = _BUFFER_LIMIT):
        self.condition = threading.Condition()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._limit = limit
        self._text = ""
        self._bytes = 0
        self.dropped_bytes = 0
        self.eof = False
        self.error: PseudoConsoleError | None = None

    def feed(self, data: bytes, *, final: bool = False) -> None:
        decoded = self._decoder.decode(data, final=final)
        with self.condition:
            if decoded:
                self._text += decoded
                self._bytes += len(decoded.encode("utf-8"))
                if self._bytes > self._limit:
                    encoded = self._text.encode("utf-8")
                    # Do not expose a partial code point at the retained edge.
                    self._text = encoded[-self._limit :].decode("utf-8", errors="ignore")
                    retained = len(self._text.encode("utf-8"))
                    self.dropped_bytes += self._bytes - retained
                    self._bytes = retained
            self.condition.notify_all()

    def finish(self, error: PseudoConsoleError | None = None) -> None:
        self.feed(b"", final=True)
        with self.condition:
            self.error = error
            self.eof = True
            self.condition.notify_all()

    def take(self, marker: str | None = None) -> str | None:
        with self.condition:
            if marker is None:
                end = len(self._text)
            else:
                found = self._text.find(marker)
                if found < 0:
                    return None
                end = found + len(marker)
            result, self._text = self._text[:end], self._text[end:]
            self._bytes -= len(result.encode("utf-8"))
            return result

    @property
    def buffered_bytes(self) -> int:
        with self.condition:
            return self._bytes


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _SIZE_T = ctypes.c_size_t
    _ULONG_PTR = ctypes.c_size_t

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", wintypes.LPVOID)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _BasicJobLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", _SIZE_T),
            ("MaximumWorkingSetSize", _SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", _ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedJobLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicJobLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", _SIZE_T),
            ("JobMemoryLimit", _SIZE_T),
            ("PeakProcessMemoryUsed", _SIZE_T),
            ("PeakJobMemoryUsed", _SIZE_T),
        ]

    def _signature(name: str, arguments: list, result):
        function = getattr(_kernel32, name)
        function.argtypes = arguments
        function.restype = result
        return function

    _CloseHandle = _signature("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    _CreatePipe = _signature("CreatePipe", [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL)
    _CreatePseudoConsole = getattr(_kernel32, "CreatePseudoConsole", None)
    if _CreatePseudoConsole is not None:
        _CreatePseudoConsole.argtypes = [_Coord, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        _CreatePseudoConsole.restype = ctypes.c_long
    _ClosePseudoConsole = getattr(_kernel32, "ClosePseudoConsole", None)
    if _ClosePseudoConsole is not None:
        _ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        _ClosePseudoConsole.restype = None
    _InitializeAttributeList = _signature("InitializeProcThreadAttributeList", [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SIZE_T)], wintypes.BOOL)
    _UpdateAttribute = _signature("UpdateProcThreadAttribute", [wintypes.LPVOID, wintypes.DWORD, _ULONG_PTR, wintypes.LPVOID, _SIZE_T, wintypes.LPVOID, wintypes.LPVOID], wintypes.BOOL)
    _DeleteAttributeList = _signature("DeleteProcThreadAttributeList", [wintypes.LPVOID], None)
    _CreateProcess = _signature("CreateProcessW", [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation)], wintypes.BOOL)
    _ResumeThread = _signature("ResumeThread", [wintypes.HANDLE], wintypes.DWORD)
    _GetExitCode = _signature("GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    _Wait = _signature("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD)
    _TerminateProcess = _signature("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
    _CreateJob = _signature("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE)
    _SetJobInfo = _signature("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL)
    _AssignJob = _signature("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL)
    _TerminateJob = _signature("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
    _PeekPipe = _signature("PeekNamedPipe", [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL)
    _ReadFile = _signature("ReadFile", [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL)
    _WriteFile = _signature("WriteFile", [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL)
    _CancelIo = _signature("CancelIoEx", [wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL)


class PseudoConsoleProcess:
    """Start an argument vector attached to an invisible Windows ConPTY.

    ``write`` sends UTF-8 terminal input. ``read_available`` consumes all
    currently buffered text, while ``read_until`` consumes through a marker
    and retains subsequent text. VT sequences are deliberately preserved.
    Only the most recent 2 MiB of UTF-8 output is retained if the caller falls
    behind; ``dropped_bytes`` makes truncation observable. No output is logged.

    ``close`` is idempotent, closes the console, and terminates the owned local
    process tree. It does not kill unrelated clients or remote terminal names.
    Use a context manager or an explicit ``finally`` block for timely cleanup.
    """

    def __init__(self, argv: Sequence[str], cwd: str | os.PathLike | None = None, columns: int = 240, rows: int = 60):
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            raise ValueError("argv must be a nonempty sequence of strings")
        arguments = list(argv)
        if any(not isinstance(value, str) or "\0" in value for value in arguments) or not arguments[0]:
            raise ValueError("argv must contain strings without NUL characters")
        if not isinstance(columns, int) or not isinstance(rows, int) or not (1 <= columns <= 32767 and 1 <= rows <= 32767):
            raise ValueError("columns and rows must be integers between 1 and 32767")
        directory = None if cwd is None else os.fspath(cwd)
        if directory is not None and (not isinstance(directory, str) or "\0" in directory):
            raise ValueError("cwd must be a path without NUL characters")
        if os.name != "nt" or _CreatePseudoConsole is None:
            raise OSError("Windows 10 version 1809 or newer is required for ConPTY")

        self.pid = 0
        self._input = self._output = self._console = self._process = self._job = None
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._buffer = _OutputBuffer()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closing = self._closed = False
        self._returncode: int | None = None
        try:
            self._start(arguments, directory, columns, rows)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(success, operation: str) -> None:
        if not success:
            raise PseudoConsoleError(operation, ctypes.get_last_error())

    def _start(self, arguments: list[str], cwd: str | None, columns: int, rows: int) -> None:
        input_read, output_write = wintypes.HANDLE(), wintypes.HANDLE()
        attribute_list = None
        attributes_initialized = False
        thread_handle = None
        try:
            input_write, output_read = wintypes.HANDLE(), wintypes.HANDLE()
            self._check(_CreatePipe(ctypes.byref(input_read), ctypes.byref(input_write), None, 0), "create_input_pipe")
            self._input = input_write.value
            self._check(_CreatePipe(ctypes.byref(output_read), ctypes.byref(output_write), None, 0), "create_output_pipe")
            self._output = output_read.value
            console = wintypes.HANDLE()
            result = _CreatePseudoConsole(_Coord(columns, rows), input_read, output_write, 0, ctypes.byref(console))
            if result < 0:
                raise PseudoConsoleError("create_pseudoconsole", result & 0xFFFFFFFF)
            self._console = console.value
            # Drain even when subsequent process creation fails. ConHost can
            # emit initialization/final frames before any child is attached.
            self._reader = threading.Thread(target=self._read_output, name=f"ConPTY-reader-pending-{id(self)}", daemon=True)
            self._reader.start()

            self._job = _CreateJob(None, None)
            self._check(self._job, "create_job")
            limits = _ExtendedJobLimits()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            self._check(_SetJobInfo(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "set_job_limits")

            size = _SIZE_T()
            _InitializeAttributeList(None, 1, 0, ctypes.byref(size))
            if not size.value:
                raise PseudoConsoleError("size_attribute_list", ctypes.get_last_error())
            attribute_storage = ctypes.create_string_buffer(size.value)
            attribute_list = ctypes.cast(attribute_storage, wintypes.LPVOID)
            self._check(_InitializeAttributeList(attribute_list, 1, 0, ctypes.byref(size)), "initialize_attribute_list")
            attributes_initialized = True
            self._check(_UpdateAttribute(attribute_list, 0, 0x00020016, self._console, ctypes.sizeof(wintypes.HANDLE), None, None), "set_pseudoconsole_attribute")
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Explicit null standard handles let console initialization replace
            # them with ConPTY handles. Otherwise CreateProcess can duplicate
            # the host's redirected stdout/stdin even with inheritance FALSE.
            startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.lpAttributeList = attribute_list
            process = _ProcessInformation()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            # Suspend until the exact child is assigned to our private kill job.
            flags = 0x00080000 | 0x00000400 | 0x00000004
            self._check(_CreateProcess(None, command_line, None, None, False, flags, None, cwd, ctypes.byref(startup), ctypes.byref(process)), "create_process")
            self._process = process.hProcess
            thread_handle = process.hThread
            self.pid = process.dwProcessId
            self._check(_AssignJob(self._job, self._process), "assign_job")
            self._reader.name = f"ConPTY-reader-{self.pid}"
            if _ResumeThread(thread_handle) == 0xFFFFFFFF:
                raise PseudoConsoleError("resume_process", ctypes.get_last_error())
        finally:
            if thread_handle:
                _CloseHandle(thread_handle)
            if attributes_initialized:
                _DeleteAttributeList(attribute_list)
            if input_read.value:
                # Keep the pipe ends alive until CreateProcess has connected
                # to the pseudoconsole; close our copies only afterwards.
                _CloseHandle(input_read)
            if output_write.value:
                _CloseHandle(output_write)

    def _read_output(self) -> None:
        error = None
        try:
            data = ctypes.create_string_buffer(65536)
            while not self._reader_stop.is_set():
                available = wintypes.DWORD()
                if not _PeekPipe(self._output, None, 0, None, ctypes.byref(available), None):
                    code = ctypes.get_last_error()
                    if code not in (6, 109, 232, 995):
                        error = PseudoConsoleError("peek_output", code)
                    break
                if not available.value:
                    self._reader_stop.wait(0.01)
                    continue
                received = wintypes.DWORD()
                if not _ReadFile(self._output, data, min(available.value, len(data)), ctypes.byref(received), None):
                    code = ctypes.get_last_error()
                    if code not in (6, 109, 232, 995):
                        error = PseudoConsoleError("read_output", code)
                    break
                if received.value:
                    self._buffer.feed(data.raw[: received.value])
        finally:
            self._buffer.finish(error)

    def write(self, value: str) -> None:
        """Write terminal text; callers supply their own line ending (usually CR)."""
        if not isinstance(value, str):
            raise TypeError("terminal input must be text")
        data = value.encode("utf-8")
        with self._write_lock:
            if self._closing or self._closed:
                raise ValueError("ConPTY is closed")
            offset = 0
            while offset < len(data):
                if self._closing:
                    raise ValueError("ConPTY is closing")
                chunk = data[offset : offset + 4096]
                written = wintypes.DWORD()
                self._check(_WriteFile(self._input, chunk, len(chunk), ctypes.byref(written), None), "write_input")
                if not written.value:
                    raise PseudoConsoleError("write_input", 109)
                offset += written.value

    def write_paced(self, value: str) -> None:
        """Feed UU's console bridge in bounded bursts instead of paste floods."""
        if not isinstance(value,str):raise TypeError('terminal input must be text')
        data=value.encode('utf-8')
        with self._write_lock:
            if self._closing or self._closed:raise ValueError('ConPTY is closed')
            offset=0
            while offset<len(data):
                if self._closing:raise ValueError('ConPTY is closing')
                chunk=data[offset:offset+64]
                written=wintypes.DWORD()
                self._check(_WriteFile(self._input,chunk,len(chunk),ctypes.byref(written),None),'write_input')
                if not written.value:raise PseudoConsoleError('write_input',109)
                offset+=written.value
                if offset<len(data):time.sleep(.01)

    def read_available(self) -> str:
        """Consume currently buffered UTF-8 text, preserving terminal VT escapes."""
        return self._buffer.take() or ""

    def read_until(self, marker: str, timeout: float = 10.0) -> str:
        """Consume through ``marker``; timeout/EOF leave unread text buffered."""
        if not isinstance(marker, str) or not marker:
            raise ValueError("marker must be nonempty text")
        if len(marker.encode("utf-8")) > _BUFFER_LIMIT:
            raise ValueError("marker exceeds the output buffer limit")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        with self._buffer.condition:
            while True:
                result = self._buffer.take(marker)
                if result is not None:
                    return result
                if self._buffer.error is not None:
                    raise self._buffer.error
                if self._buffer.eof:
                    raise EOFError("ConPTY output ended before the requested marker")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ConPTY marker was not received before timeout")
                self._buffer.condition.wait(remaining)

    @property
    def buffered_bytes(self) -> int:
        return self._buffer.buffered_bytes

    @property
    def dropped_bytes(self) -> int:
        with self._buffer.condition:
            return self._buffer.dropped_bytes

    def poll(self) -> int | None:
        """Return the local root process's exit code, or None while it is running."""
        with self._state_lock:
            if self._returncode is not None or not self._process:
                return self._returncode
            wait_result = _Wait(self._process, 0)
            if wait_result == 0x00000102:
                return None
            if wait_result != 0:
                raise PseudoConsoleError("poll_process", ctypes.get_last_error())
            code = wintypes.DWORD()
            self._check(_GetExitCode(self._process, ctypes.byref(code)), "get_exit_code")
            self._returncode = code.value
            return self._returncode

    def close(self, timeout: float = 5.0) -> None:
        """Tear down the owned local console/tree with bounded process/thread waits."""
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        with self._close_lock:
            if self._closed:
                return
            self._closing = True
            deadline = time.monotonic() + max(0.1, timeout)
            try:
                if self._input:
                    _CancelIo(self._input, None)
                if self._process:
                    # The job contains only our suspended-then-assigned child and
                    # its descendants, never another pre-existing UU process.
                    if self._job:
                        _TerminateJob(self._job, 1)
                    wait_ms = min(500, max(0, int((deadline - time.monotonic()) * 1000)))
                    if _Wait(self._process, wait_ms) == 0x00000102:
                        _TerminateProcess(self._process, 1)
                        wait_ms = min(500, max(0, int((deadline - time.monotonic()) * 1000)))
                        _Wait(self._process, wait_ms)
                    self.poll()
                if self._job:
                    _CloseHandle(self._job)
                    self._job = None
                if self._console:
                    # Keep the output reader alive while this drains ConHost:
                    # closing a full output pipe without a reader can deadlock.
                    _ClosePseudoConsole(self._console)
                    self._console = None
                if self._input:
                    _CancelIo(self._input, None)
                    acquired = self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
                    try:
                        _CloseHandle(self._input)
                        self._input = None
                    finally:
                        if acquired:
                            self._write_lock.release()
                if self._reader:
                    # Give the draining thread a chance to observe pipe EOF.
                    self._reader.join(min(0.2, max(0.0, deadline - time.monotonic())))
                    self._reader_stop.set()
                    self._reader.join(max(0.0, deadline - time.monotonic()))
                    if self._reader.is_alive():
                        raise PseudoConsoleError("stop_reader", 258)
                else:
                    self._buffer.finish()
            finally:
                self._reader_stop.set()
                # Handles are never closed under a reader that is still using
                # them. The reader uses PeekNamedPipe, not an indefinite read.
                if self._output and (not self._reader or not self._reader.is_alive()):
                    _CloseHandle(self._output)
                    self._output = None
                with self._state_lock:
                    if self._process:
                        _CloseHandle(self._process)
                        self._process = None
                self._closed = not self._reader or not self._reader.is_alive()

    def __enter__(self) -> PseudoConsoleProcess:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

