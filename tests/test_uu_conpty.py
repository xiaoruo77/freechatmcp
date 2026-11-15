import ctypes
import os
import threading
import time

import pytest

from web_tools.uu_conpty import PseudoConsoleError, PseudoConsoleProcess, _OutputBuffer


def test_output_buffer_decodes_split_utf8_and_preserves_vt():
    buffer = _OutputBuffer()
    raw = "before\x1b[31m你好🙂\x1b[0mafter".encode("utf-8")
    for byte in raw:
        buffer.feed(bytes([byte]))
    buffer.finish()
    assert buffer.take("好🙂") == "before\x1b[31m你好🙂"
    assert buffer.take() == "\x1b[0mafter"
    assert buffer.buffered_bytes == 0


def test_output_buffer_is_byte_bounded_and_does_not_split_unicode():
    buffer = _OutputBuffer(limit=11)
    buffer.feed(("abc" + "🙂" * 4).encode("utf-8"))
    assert buffer.buffered_bytes <= 11
    assert buffer.dropped_bytes == 11
    assert buffer.take() == "🙂🙂"


def test_timeout_keeps_buffer_and_does_not_expose_marker():
    process = PseudoConsoleProcess.__new__(PseudoConsoleProcess)
    process._buffer = _OutputBuffer()
    process._buffer.feed(b"retained")
    with pytest.raises(TimeoutError) as failure:
        process.read_until("private-marker", 0)
    assert "private-marker" not in str(failure.value)
    assert process.read_available() == "retained"


def test_read_until_keeps_text_following_marker_and_reports_eof():
    process = PseudoConsoleProcess.__new__(PseudoConsoleProcess)
    process._buffer = _OutputBuffer()
    process._buffer.feed(b"first|MARK|remaining")
    process._buffer.finish()
    assert process.read_until("|MARK|", 0) == "first|MARK|"
    with pytest.raises(EOFError):
        process.read_until("absent", 0)
    assert process.read_available() == "remaining"


@pytest.mark.parametrize("arguments", [[], "cmd /c echo", [""], ["cmd", "nul\0argument"], ["cmd", 2]])
def test_rejects_invalid_argument_vectors_without_leaking_them(arguments):
    with pytest.raises(ValueError):
        PseudoConsoleProcess(arguments)


@pytest.mark.parametrize("columns,rows", [(0, 60), (240, 32768), (2.5, 60)])
def test_rejects_invalid_console_dimensions(columns, rows):
    with pytest.raises(ValueError):
        PseudoConsoleProcess(["cmd"], columns=columns, rows=rows)


def test_native_errors_do_not_include_arguments():
    error = PseudoConsoleError("create_process", 2)
    assert error.operation == "create_process"
    assert error.winerror == 2
    assert "Windows error 2" in str(error)


windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY integration")


def _cmd(*arguments):
    return [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), *arguments]


def _visible_windows_for_pid(pid):
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    @callback_type
    def collect(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    assert user32.EnumWindows(collect, 0)
    return found


@windows_only
def test_real_cmd_echo_has_output_and_no_visible_window():
    with PseudoConsoleProcess(_cmd("/d", "/q", "/c", "echo XIAORUO_CONPTY_SMOKE")) as process:
        assert process.pid > 0
        assert "XIAORUO_CONPTY_SMOKE" in process.read_until("XIAORUO_CONPTY_SMOKE", timeout=5)
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.poll() == 0
        assert not _visible_windows_for_pid(process.pid)
    assert not process._reader.is_alive()
    assert process._input is process._output is process._console is process._process is process._job is None


@windows_only
def test_interactive_cmd_write_marker_and_owned_shutdown():
    before = {thread.ident for thread in threading.enumerate()}
    process = PseudoConsoleProcess(_cmd("/d", "/q"))
    try:
        assert not _visible_windows_for_pid(process.pid)
        assert process.poll() is None
        # The complete marker does not occur in the submitted command echo.
        process.write("for %A in (XIAORUO_) do @echo %ACONPTY_READY\r\n")
        assert "XIAORUO_CONPTY_READY" in process.read_until("XIAORUO_CONPTY_READY", timeout=5)
        assert process.buffered_bytes <= 2 * 1024 * 1024
    finally:
        started = time.monotonic()
        process.close(timeout=3)
    assert time.monotonic() - started < 3
    assert process.poll() is not None
    assert not process._reader.is_alive()
    assert not [thread for thread in threading.enumerate() if thread.ident not in before and thread.name.startswith("ConPTY-reader-")]
    process.close()
    with pytest.raises(ValueError, match="closed"):
        process.write("echo should-not-run\r\n")


@windows_only
def test_failed_spawn_cleans_up_without_command_disclosure():
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(PseudoConsoleError) as failure:
        PseudoConsoleProcess([r"C:\Windows\not-a-real-executable-private-device-id.exe"])
    assert failure.value.operation == "create_process"
    assert "private-device-id" not in str(failure.value)
    assert not [thread for thread in threading.enumerate() if thread.ident not in before and thread.name.startswith("ConPTY-reader-")]


@windows_only
def test_large_cmd_output_is_continuously_drained_and_bounded():
    # More than 2 MiB of actual terminal output, without a consuming caller.
    command = "for /L %A in (1,1,18000) do @echo " + ("x" * 160)
    with PseudoConsoleProcess(_cmd("/d", "/q", "/c", command)) as process:
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() == 0
        assert process.dropped_bytes > 0
        assert process.buffered_bytes <= 2 * 1024 * 1024
        assert process.read_available()
    assert not process._reader.is_alive()
