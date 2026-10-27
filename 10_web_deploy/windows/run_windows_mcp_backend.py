"""Pinned Windows-MCP bootstrap with a synchronous user-desktop preflight.

The upstream CLI, auth, schemas, input hooks, overlay and ControlToolGate remain
unchanged. This adds an outer tools/call guard before that control gate takes a
lease, closing the small window before the upstream asynchronous watchdog sees
a lock screen, secure UAC desktop or disconnected user session.
"""
from __future__ import annotations

import faulthandler
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
import uuid


_DESKTOP_ERRORS = frozenset({"windows_required", "interactive_session_required", "desktop_session_inactive",
                             "desktop_locked_or_secure", "desktop_session_unavailable"})
_TRACE_LOCK = threading.Lock()


async def trace_app_call(context, call_next, directory: Path | None):
    """One bounded App call trace: timing and Python frames, never locals/UI."""
    if directory is None or getattr(context.message, "name", None) != "App" or not _TRACE_LOCK.acquire(blocking=False):
        return await call_next(context)
    identifier = str(time.time_ns()) + "-" + uuid.uuid4().hex[:8]
    frames = directory / (identifier + ".stack.private.log")
    metadata = directory / (identifier + ".json")
    started = time.monotonic()
    record = {"operation": "App", "started_unix_ns": time.time_ns(), "pid": os.getpid(), "thread_id": threading.get_native_id(),
              "stack_timeout_seconds": 12, "state": "running", "no_locals_or_arguments_logged": True}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        old = sorted(path for path in directory.glob("*.json") if re.fullmatch(r"[0-9]{10,24}-[0-9a-f]{8}", path.stem))
        for path in old[:-11]:
            # Fixed generated names in the verified private directory only.
            for obsolete in (path, path.with_name(path.stem + ".stack.private.log")):
                if obsolete.parent.resolve() == directory.resolve() and obsolete.is_file():
                    obsolete.unlink()
        metadata.write_text(json.dumps(record, indent=2), encoding="utf-8")
        with frames.open("x", encoding="utf-8") as handle:
            faulthandler.dump_traceback_later(12, repeat=False, file=handle)
            try:
                result = await call_next(context)
                record["state"] = "returned"
                return result
            except BaseException as exc:
                record["state"] = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
                record["exception_type"] = type(exc).__name__
                raise
            finally:
                faulthandler.cancel_dump_traceback_later()
                record["duration_seconds"] = round(time.monotonic() - started, 3)
                metadata.write_text(json.dumps(record, indent=2), encoding="utf-8")
    finally:
        _TRACE_LOCK.release()


def make_desktop_guard(middleware_base: type, tool_error: type[Exception], desktop_reader, trace_directory: Path | None = None):
    """Dependency injection keeps unit tests free from Win32/GUI initialization."""
    class DesktopSessionGuard(middleware_base):
        async def on_call_tool(self, context: Any, call_next):
            try:
                state = desktop_reader()
            except Exception:
                state = {"available": False, "error_code": "desktop_session_unavailable"}
            if not isinstance(state, dict) or state.get("available") is not True:
                code = state.get("error_code") if isinstance(state, dict) else None
                if code not in _DESKTOP_ERRORS:
                    code = "desktop_session_unavailable"
                raise tool_error(json.dumps({"code": code, "message": "An unlocked interactive user desktop is required"}))
            return await trace_app_call(context, call_next, trace_directory)

    return DesktopSessionGuard()


def install_guarded_factory(upstream, guard_factory):
    """Install before upstream constructs its singleton or adds ControlToolGate."""
    if getattr(upstream.FastMCP, "_xiaoruo_session_guarded", False):
        return
    if getattr(upstream, "_mcp", None) is not None:
        raise RuntimeError("desktop_backend_already_initialized")
    original = upstream.FastMCP

    class DesktopGuardedFastMCP(original):
        _xiaoruo_session_guarded = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Upstream _build_mcp adds its original ControlToolGate after this.
            # FastMCP executes middleware in registration order.
            self.add_middleware(guard_factory())

    upstream.FastMCP = DesktopGuardedFastMCP


def main(argv: list[str] | None = None):
    root = Path(__file__).resolve().parents[2]
    # Import exactly the project's Win32 desktop probe into the isolated runtime.
    # No service settings, token, database or model client are loaded here.
    sys.path.insert(0, str(root / "06_web_tools"))
    from web_tools.windows_mcp import interactive_desktop_state
    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware
    import windows_mcp.__main__ as upstream

    diagnostic_path = os.environ.get("WEB_AGENT_WINDOWS_MCP_APP_TRACE_DIR")
    trace_directory = Path(diagnostic_path).resolve() if diagnostic_path else None
    if trace_directory is not None:
        expected = (root / "10_web_deploy/windows/windows_mcp_runtime/supervisor/private_diagnostics").resolve()
        if trace_directory != expected:
            raise RuntimeError("desktop_diagnostic_directory_invalid")
    install_guarded_factory(upstream, lambda: make_desktop_guard(Middleware, ToolError, interactive_desktop_state, trace_directory))
    upstream.main(args=argv, prog_name="windows-mcp")


if __name__ == "__main__":
    main()