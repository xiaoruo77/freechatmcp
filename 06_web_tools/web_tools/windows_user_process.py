from __future__ import annotations

"""Run a bounded process in the currently logged-in Windows user's session.

Windows services installed with the default LocalSystem account cannot start
WSL (WSL returns WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED).  The MCP API intentionally
stays a LocalSystem service for local binding and filesystem access, but WSL
commands are launched with the active user's primary token through the Win32
API.  No credentials or tokens are persisted by this module.
"""

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(slots=True)
class UserProcessResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


def is_local_system() -> bool:
    """Return whether the current process is running as a service identity."""

    if os.name != "nt":
        return False
    identities = {(os.environ.get("USERNAME") or "").strip().upper()}
    try:
        identities.add(_windows_identity_name().strip().upper())
    except OSError:
        pass
    return bool(identities & {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "LOCALSERVICE", "NETWORKSERVICE"})


def should_use_active_user_context() -> bool:
    """Return whether a child should be launched in the active user's session."""

    forced = (os.environ.get("WEB_AGENT_WSL_USER_CONTEXT") or "").strip().lower() in {"1", "true", "yes", "on"}
    return os.name == "nt" and (is_local_system() or forced)


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)
    _wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

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

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(_SecurityAttributes), wintypes.DWORD]
    _kernel32.CreatePipe.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _kernel32.SetHandleInformation.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _advapi32.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.DuplicateTokenEx.restype = wintypes.BOOL
    _advapi32.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetUserNameW.restype = wintypes.BOOL
    _advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    ]
    _advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

    _userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    _userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    _userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    _userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

    _kernel32.WTSGetActiveConsoleSessionId.argtypes = []
    _kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    _wtsapi32.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    _wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL

    _TOKEN_ALL_ACCESS = 0x000F01FF
    _SECURITY_IMPERSONATION = 2
    _TOKEN_PRIMARY = 1
    _MAXIMUM_ALLOWED = 0x02000000
    _HANDLE_FLAG_INHERIT = 0x00000001
    _STARTF_USESTDHANDLES = 0x00000100
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _CREATE_NO_WINDOW = 0x08000000
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _INVALID_SESSION_ID = 0xFFFFFFFF


    def _windows_identity_name() -> str:
        size = wintypes.DWORD(256)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise _win32_error("GetUserNameW")
        return buffer.value


# The WSL bridge is used for real molecular-dynamics preparation and can
# legitimately run for several minutes.  The old 120-second ceiling silently
# killed those jobs even when the runner payload requested a longer timeout;
# that made the web agent see a false failure halfway through EM/NVT/NPT.
# Keep the safety bound aligned with the runner's 24-hour maximum instead of
# imposing a much smaller hidden limit here.
MAX_PROCESS_TIMEOUT_SECONDS = 24 * 60 * 60


def _bounded_timeout(timeout: int | float) -> float:
    return max(1.0, min(float(timeout), float(MAX_PROCESS_TIMEOUT_SECONDS)))


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _subprocess_fallback(command: Sequence[str], cwd: Path, timeout: int | float) -> UserProcessResult:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_bounded_timeout(timeout),
            check=False,
        )
        return UserProcessResult(completed.returncode, _decode(completed.stdout), _decode(completed.stderr))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return UserProcessResult(-1, _decode(stdout), _decode(stderr), timed_out=True)


if os.name == "nt":

    def _win32_error(operation: str) -> OSError:
        error = ctypes.get_last_error()
        return OSError(error, f"{operation} failed: {ctypes.FormatError(error)}")


    def _close_handle(handle: wintypes.HANDLE | None) -> None:
        if handle is not None and getattr(handle, "value", handle):
            _kernel32.CloseHandle(handle)


    def _read_pipe(handle: wintypes.HANDLE, target: list[bytes]) -> None:
        fd = msvcrt.open_osfhandle(int(handle.value), os.O_RDONLY)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            target.append(b"".join(chunks))
        finally:
            os.close(fd)


    def _active_user_token() -> wintypes.HANDLE:
        session_id = _kernel32.WTSGetActiveConsoleSessionId()
        if session_id == _INVALID_SESSION_ID:
            raise RuntimeError("No active Windows user session is available for WSL")
        impersonation_token = wintypes.HANDLE()
        if not _wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(impersonation_token)):
            raise _win32_error("WTSQueryUserToken")
        primary_token = wintypes.HANDLE()
        try:
            if not _advapi32.DuplicateTokenEx(
                impersonation_token,
                _TOKEN_ALL_ACCESS or _MAXIMUM_ALLOWED,
                None,
                _SECURITY_IMPERSONATION,
                _TOKEN_PRIMARY,
                ctypes.byref(primary_token),
            ):
                raise _win32_error("DuplicateTokenEx")
            return primary_token
        finally:
            _close_handle(impersonation_token)


    def _executable(command: Sequence[str]) -> str:
        first = str(command[0])
        candidate = Path(first)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)
        found = shutil.which(first)
        if found:
            return found
        if first.lower() in {"wsl", "wsl.exe"}:
            windir = os.environ.get("WINDIR", r"C:\Windows")
            fallback = Path(windir) / "System32" / "wsl.exe"
            if fallback.exists():
                return str(fallback)
        raise FileNotFoundError(f"Executable not found: {first}")


    def _run_as_active_user_windows(
        command: Sequence[str],
        cwd: Path,
        timeout: int | float,
        cancel_event: threading.Event | None = None,
    ) -> UserProcessResult:
        token: wintypes.HANDLE | None = None
        environment = ctypes.c_void_p()
        stdin_handle: wintypes.HANDLE | None = None
        stdout_read: wintypes.HANDLE | None = None
        stdout_write: wintypes.HANDLE | None = None
        stderr_read: wintypes.HANDLE | None = None
        stderr_write: wintypes.HANDLE | None = None
        process_info = _ProcessInformation()
        stdout_claimed = False
        stderr_claimed = False
        stdout_data: list[bytes] = []
        stderr_data: list[bytes] = []
        threads: list[threading.Thread] = []
        timed_out = False
        cancelled = False

        try:
            token = _active_user_token()
            security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
            stdout_read, stdout_write = wintypes.HANDLE(), wintypes.HANDLE()
            stderr_read, stderr_write = wintypes.HANDLE(), wintypes.HANDLE()
            if not _kernel32.CreatePipe(ctypes.byref(stdout_read), ctypes.byref(stdout_write), ctypes.byref(security), 0):
                raise _win32_error("CreatePipe(stdout)")
            if not _kernel32.CreatePipe(ctypes.byref(stderr_read), ctypes.byref(stderr_write), ctypes.byref(security), 0):
                raise _win32_error("CreatePipe(stderr)")
            if not _kernel32.SetHandleInformation(stdout_read, _HANDLE_FLAG_INHERIT, 0):
                raise _win32_error("SetHandleInformation(stdout)")
            if not _kernel32.SetHandleInformation(stderr_read, _HANDLE_FLAG_INHERIT, 0):
                raise _win32_error("SetHandleInformation(stderr)")
            stdin_handle = _kernel32.CreateFileW(
                "NUL",
                _GENERIC_READ,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                ctypes.byref(security),
                _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if not stdin_handle or stdin_handle == wintypes.HANDLE(-1).value:
                raise _win32_error("CreateFile(NUL)")

            if not _userenv.CreateEnvironmentBlock(ctypes.byref(environment), token, False):
                raise _win32_error("CreateEnvironmentBlock")

            executable = _executable(command)
            command_line = subprocess.list2cmdline([executable, *[str(item) for item in command[1:]]])
            command_line_buffer = ctypes.create_unicode_buffer(command_line)
            startup = _StartupInfo()
            startup.cb = ctypes.sizeof(_StartupInfo)
            startup.dwFlags = _STARTF_USESTDHANDLES
            startup.hStdInput = stdin_handle
            startup.hStdOutput = stdout_write
            startup.hStdError = stderr_write
            if not _advapi32.CreateProcessAsUserW(
                token,
                executable,
                command_line_buffer,
                None,
                None,
                True,
                _CREATE_NEW_PROCESS_GROUP | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
                environment,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                raise _win32_error("CreateProcessAsUserW")

            _close_handle(stdin_handle)
            stdin_handle = None
            _close_handle(stdout_write)
            stdout_write = None
            _close_handle(stderr_write)
            stderr_write = None

            out_thread = threading.Thread(target=_read_pipe, args=(stdout_read, stdout_data), daemon=True)
            err_thread = threading.Thread(target=_read_pipe, args=(stderr_read, stderr_data), daemon=True)
            out_thread.start()
            err_thread.start()
            stdout_claimed = True
            stderr_claimed = True
            threads.extend([out_thread, err_thread])

            deadline = time.monotonic() + _bounded_timeout(timeout)
            while True:
                wait_result = _kernel32.WaitForSingleObject(process_info.hProcess, 200)
                if wait_result == _WAIT_OBJECT_0:
                    break
                if wait_result != _WAIT_TIMEOUT:
                    raise _win32_error("WaitForSingleObject")
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _kernel32.TerminateProcess(process_info.hProcess, 1)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kernel32.TerminateProcess(process_info.hProcess, 1)
                    break
            _kernel32.WaitForSingleObject(process_info.hProcess, 5000)
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
                raise _win32_error("GetExitCodeProcess")
            for thread in threads:
                thread.join(timeout=5)
            return UserProcessResult(int(exit_code.value), _decode(stdout_data[0] if stdout_data else b""), _decode(stderr_data[0] if stderr_data else b""), timed_out, cancelled)
        finally:
            for thread in threads:
                if thread.is_alive():
                    thread.join(timeout=1)
            _close_handle(process_info.hThread)
            _close_handle(process_info.hProcess)
            _close_handle(stdin_handle)
            if not stdout_claimed:
                _close_handle(stdout_read)
            if stdout_write is not None:
                _close_handle(stdout_write)
            if not stderr_claimed:
                _close_handle(stderr_read)
            if stderr_write is not None:
                _close_handle(stderr_write)
            if environment.value:
                _userenv.DestroyEnvironmentBlock(environment)
            _close_handle(token)


def run_command_as_active_user(
    command: Sequence[str],
    cwd: Path,
    timeout: int | float,
    cancel_event: threading.Event | None = None,
) -> UserProcessResult:
    """Run a command with the active user's token when the caller is LocalSystem."""

    if os.name != "nt":
        return _subprocess_fallback(command, cwd, timeout)
    return _run_as_active_user_windows(command, cwd, timeout, cancel_event)