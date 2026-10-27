from __future__ import annotations

"""Opt-in Windows-MCP client; desktop execution never provides AI inference.

The stdio session is owned by one actor task so Windows-MCP's Snapshot labels
survive across calls and AnyIO context managers are entered/exited in one task.
Nothing is installed or launched until an explicitly enabled tool is called.
"""

import asyncio
import base64
import binascii
import contextlib
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any
from urllib.parse import urlsplit
import uuid


READ_ACTIONS = frozenset({"Snapshot", "Screenshot", "ControlStatus"})
CONTROL_ACTIONS = frozenset({"Click", "Type", "Shortcut", "Scroll", "App"})
ALLOWED_ACTIONS = READ_ACTIONS | CONTROL_ACTIONS
DEFAULT_ARGS = (
    "--python", "3.14", "windows-mcp==0.8.8", "serve", "--transport", "stdio",
    "--tools", ",".join(sorted(ALLOWED_ACTIONS)),
)
# Deliberately excludes App.launch_executable and all shell/filesystem/registry tools.
_FIELDS = {
    "Snapshot": {"use_vision", "use_dom", "use_annotation", "use_ui_tree", "width_reference_line", "height_reference_line", "display", "region"},
    "Screenshot": {"use_annotation", "width_reference_line", "height_reference_line", "display", "region"},
    "ControlStatus": set(),
    "Click": {"loc", "label", "button", "clicks"},
    "Type": {"text", "loc", "label", "clear", "caret_position", "press_enter"},
    "Shortcut": {"shortcut"},
    "Scroll": {"loc", "label", "type", "direction", "wheel_times"},
    "App": {"mode", "name", "window_loc", "window_size"},
}
_MAX_TEXT = 40_000
_MAX_IMAGE_BYTES = 12 * 1024 * 1024


class WindowsMCPError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _process_session_id(pid: int) -> int:
    session_id = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
        raise OSError("Cannot inspect Windows session")
    return int(session_id.value)


def _active_session(session_id: int) -> bool:
    from ctypes import wintypes

    api = ctypes.WinDLL("wtsapi32", use_last_error=True)
    api.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    api.WTSQuerySessionInformationW.restype = wintypes.BOOL
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    value = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not api.WTSQuerySessionInformationW(None, session_id, 8, ctypes.byref(value), ctypes.byref(size)):
        return False
    try:
        return size.value >= ctypes.sizeof(ctypes.c_int) and ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value == 0
    finally:
        api.WTSFreeMemory(value)


def interactive_desktop_state() -> dict[str, Any]:
    """Read-only preflight, failing closed on Session 0, lock screen or UAC."""
    if os.name != "nt":
        return {"available": False, "error_code": "windows_required"}
    try:
        session_id = _process_session_id(os.getpid())
        if session_id == 0:
            return {"available": False, "session_id": 0, "error_code": "interactive_session_required"}
        if not _active_session(session_id):
            return {"available": False, "session_id": session_id, "error_code": "desktop_session_inactive"}
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user32.GetUserObjectInformationW.restype = wintypes.BOOL
        desktop = user32.OpenInputDesktop(0, False, 0x0101)
        if not desktop:
            return {"available": False, "session_id": session_id, "error_code": "desktop_locked_or_secure"}
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not user32.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)) or name.value.casefold() != "default":
                return {"available": False, "session_id": session_id, "error_code": "desktop_locked_or_secure"}
        finally:
            user32.CloseDesktop(desktop)
        return {"available": True, "session_id": session_id}
    except (OSError, AttributeError):
        return {"available": False, "error_code": "desktop_session_unavailable"}


def _validate_args(action: str, arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise WindowsMCPError("invalid_arguments", "Desktop arguments must be an object")
    if set(arguments) - _FIELDS[action]:
        raise WindowsMCPError("invalid_arguments", "Unsupported desktop argument")
    if action == "App" and arguments.get("mode", "launch") not in {"launch", "switch", "resize"}:
        raise WindowsMCPError("desktop_action_denied", "App supports launch, switch and resize only")
    if action == "Type" and not isinstance(arguments.get("text"), str):
        raise WindowsMCPError("invalid_arguments", "Type requires text")
    if action == "Shortcut" and not isinstance(arguments.get("shortcut"), str):
        raise WindowsMCPError("invalid_arguments", "Shortcut requires shortcut")
    if any(isinstance(value, str) and len(value) > _MAX_TEXT for value in arguments.values()):
        raise WindowsMCPError("invalid_arguments", "Desktop input exceeds size limit")
    for key, value in arguments.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise WindowsMCPError("invalid_arguments", "Desktop arguments must be finite")
        if key in {"loc", "window_loc", "window_size"} and value is not None:
            if not isinstance(value, list) or len(value) != 2 or any(type(n) is not int for n in value):
                raise WindowsMCPError("invalid_arguments", "Coordinates must contain two integers")


class WindowsMCPBridge:
    def __init__(self, settings: Any):
        self.settings = settings
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._worker: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()

    def _option(self, name: str, default: Any = None) -> Any:
        return getattr(self.settings, "windows_mcp_" + name, default)

    def _http_config(self) -> tuple[str, dict[str, str]]:
        url = str(self._option("url", ""))
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WindowsMCPError("desktop_endpoint_denied", "Windows-MCP HTTP must use an authenticated loopback endpoint")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise WindowsMCPError("desktop_endpoint_denied", "Invalid desktop endpoint port") from exc
        token_path = self._option("token_file")
        if not token_path or not Path(token_path).is_absolute():
            raise WindowsMCPError("desktop_auth_required", "An absolute private Windows-MCP token file is required")
        try:
            if Path(token_path).stat().st_size > 4096:
                raise OSError("Oversized token")
            token = Path(token_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise WindowsMCPError("desktop_auth_required", "Cannot read private Windows-MCP token file") from exc
        if len(token) < 20 or any(c in token for c in "\r\n\x00"):
            raise WindowsMCPError("desktop_auth_required", "Windows-MCP token must contain at least 20 characters")
        self._verify_http_session(port)
        return url, {"Authorization": "Bearer " + token}

    @staticmethod
    def _verify_http_session(port: int) -> None:
        if os.name != "nt":
            raise WindowsMCPError("windows_required", "Windows desktop control requires Windows")
        try:
            import psutil
            listeners = [conn for conn in psutil.net_connections(kind="tcp") if conn.status == "LISTEN" and conn.laddr.port == port]
            if not listeners:
                raise WindowsMCPError("desktop_bridge_unavailable", "No local Windows-MCP user bridge is listening", retryable=True)
            if any(conn.laddr.ip not in {"127.0.0.1", "::1"} for conn in listeners):
                raise WindowsMCPError("desktop_endpoint_denied", "Desktop bridge must bind only loopback")
            for conn in listeners:
                if not conn.pid:
                    raise WindowsMCPError("desktop_session_unavailable", "Cannot verify desktop bridge process")
                session_id = _process_session_id(conn.pid)
                if session_id == 0 or not _active_session(session_id):
                    raise WindowsMCPError("interactive_session_required", "Desktop bridge must run in an active logged-in user session")
        except WindowsMCPError:
            raise
        except (OSError, RuntimeError, psutil.Error) as exc:
            raise WindowsMCPError("desktop_session_unavailable", "Cannot verify local desktop bridge session") from exc

    @contextlib.asynccontextmanager
    async def _session(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        transport = self._option("transport", "stdio")
        timeout = max(1.0, min(float(self._option("timeout", 30)), 120.0))
        if transport == "stdio":
            state = interactive_desktop_state()
            if not state.get("available"):
                raise WindowsMCPError(state["error_code"], "Windows-MCP stdio requires an unlocked interactive user session")
            command = str(self._option("command", "uvx"))
            if not (Path(command).is_file() or shutil.which(command)):
                raise WindowsMCPError("desktop_dependency_missing", "Windows-MCP launcher is unavailable; install its separate environment first")
            args = list(self._option("args", DEFAULT_ARGS))
            # Stdio only. A launcher that opens an HTTP listener is never accepted.
            if "serve" not in args or "--transport" not in args or args[args.index("--transport") + 1:args.index("--transport") + 2] != ["stdio"]:
                raise WindowsMCPError("desktop_configuration_invalid", "Stdio launcher must explicitly select serve --transport stdio")
            env = {"ANONYMIZED_TELEMETRY": "false", "PYTHONIOENCODING": "utf-8"}
            if os.getenv("LOCALAPPDATA"):
                env["LOCALAPPDATA"] = os.environ["LOCALAPPDATA"]
            # No raw Windows-MCP stderr is copied to public/service logs.
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(StdioServerParameters(command=command, args=args, env=env), errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        yield session
        elif transport == "streamable-http":
            url, headers = self._http_config()
            async with streamablehttp_client(url, headers=headers, timeout=timeout, sse_read_timeout=timeout) as (read, write, _):
                async with ClientSession(read, write) as session:
                    yield session
        else:
            raise WindowsMCPError("desktop_configuration_invalid", "Desktop transport must be stdio or streamable-http")

    async def _serve(self) -> None:
        current = None
        try:
            async with self._session() as session:
                await session.initialize()
                advertised = await session.list_tools()
                available = {tool.name: tool.inputSchema for tool in advertised.tools if tool.name in ALLOWED_ACTIONS}
                while True:
                    current = await self._queue.get()
                    if current is None:
                        return
                    action, arguments, future = current
                    if future.cancelled():
                        continue
                    if action not in available:
                        future.set_exception(WindowsMCPError("desktop_tool_unavailable", "Requested desktop tool is not advertised by Windows-MCP"))
                        continue
                    try:
                        # MCP's dependency validates actual advertised schemas before dispatch.
                        from jsonschema import Draft202012Validator
                        Draft202012Validator(available[action]).validate(arguments)
                    except Exception:
                        future.set_exception(WindowsMCPError("invalid_arguments", "Desktop arguments do not match the live Windows-MCP schema"))
                        continue
                    try:
                        response = await session.call_tool(action, arguments)
                        result = self._normalize(action, response, arguments)
                        if not future.done():
                            future.set_result(result)
                    except WindowsMCPError as exc:
                        if not future.done():
                            future.set_exception(exc)
                    current = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # AnyIO can wrap connection errors in ExceptionGroup. Do not leak
            # credentials, screen text, or raw command/environment diagnostics.
            error = exc if isinstance(exc, WindowsMCPError) else WindowsMCPError("desktop_bridge_unavailable", "Windows-MCP disconnected or could not start", retryable=True)
            if current is not None and not current[2].done():
                current[2].set_exception(error)
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending[2].done():
                    pending[2].set_exception(error)

    def _normalize(self, action: str, response: Any, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        texts, artifacts = [], []
        for block in getattr(response, "content", []):
            kind = getattr(block, "type", None)
            if kind == "text":
                value = str(block.text)
                # FastMCP 4 serializes the upstream capture tool's list[str]
                # as JSON in one text block. Decode only that documented
                # capture shape, preserving actual UI newlines and making
                # upstream error-string detection work across SDK versions.
                if action in {"Snapshot", "Screenshot"} and len(value) <= _MAX_TEXT * 4:
                    try:
                        captured = json.loads(value)
                    except (ValueError, TypeError):
                        captured = None
                    if isinstance(captured, list) and all(isinstance(part, str) for part in captured):
                        texts.extend(part[:_MAX_TEXT] for part in captured)
                        continue
                texts.append(value[:_MAX_TEXT])
            elif kind == "image":
                if block.mimeType not in {"image/png", "image/jpeg", "image/webp"} or len(block.data) > _MAX_IMAGE_BYTES * 4 // 3 + 8:
                    raise WindowsMCPError("desktop_image_invalid", "Desktop image format or size is unsupported")
                try:
                    data = base64.b64decode(block.data, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise WindowsMCPError("desktop_image_invalid", "Desktop image encoding is invalid") from exc
                if not data or len(data) > _MAX_IMAGE_BYTES:
                    raise WindowsMCPError("desktop_image_invalid", "Desktop image is empty or too large")
                root = Path(self.settings.data_root).resolve() / "windows_mcp"
                root.mkdir(parents=True, exist_ok=True)
                suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[block.mimeType]
                path = root / (uuid.uuid4().hex + suffix)
                with path.open("xb") as handle:
                    handle.write(data)
                artifacts.append({"path": str(path), "mime_type": block.mimeType, "size_bytes": len(data), "private": True})
        text = "\n".join(texts)[:_MAX_TEXT]
        # Some upstream capture functions return error strings with isError=False.
        capture_error = any(t.lstrip().casefold().startswith(("error capturing desktop state:", "error capturing screenshot:")) for t in texts)
        app_error = action == "App" and any(any(message in t.casefold() for message in ("not found in start menu", "invalid app identifier:", "error switching app:", "no active window found", "no windows found on the desktop", " is minimized", " is maximized", "fullyqualifiederrorid")) or t.lstrip().casefold().startswith("failed to ") for t in texts)
        requested = arguments or {}
        upstream_failed = bool(getattr(response, "isError", False)) or capture_error or app_error
        # ControlToolGate errors contain ownership state, not a window title.
        # A genuine upstream failure must not be overwritten as a fuzzy-target
        # mismatch merely because the requested title is absent from its text.
        target_mismatch = not upstream_failed and action == "App" and requested.get("mode") == "switch" and requested.get("name") and str(requested["name"]).casefold() not in text.casefold()
        failed = upstream_failed or bool(target_mismatch)
        error_code = "desktop_target_mismatch" if target_mismatch else "desktop_action_failed"
        # A successful transport response is not evidence of a capture. Keep
        # genuine upstream errors above; only otherwise-successful empty
        # observations use this distinct, stable contract failure.
        capture_empty = not failed and (
            (action == "Snapshot" and not text.strip() and not artifacts)
            or (action == "Screenshot" and not artifacts)
        )
        if capture_empty:
            failed = True
            error_code = "desktop_capture_empty"
        error_message = (
            "Windows-MCP returned no desktop observation" if action == "Snapshot"
            else "Windows-MCP returned no screenshot image"
        ) if capture_empty else text or "Windows-MCP rejected the desktop action"
        data = getattr(response, "structuredContent", None)
        return {
            "ok": not failed, "status": "failed" if failed else "completed", "terminal": True,
            "operation": "windows_" + action.casefold(), "task_id": None,
            "result": {"summary": text, "data": data}, "artifacts": artifacts,
            "error": {"code": error_code, "message": error_message} if failed else None,
            "error_code": error_code if failed else None,
            "retryable": False,
            "evidence": {"tool": action, "transport": self._option("transport", "stdio"), "checks": [], "upstream_reported_success": not failed, "action_may_have_run": action in CONTROL_ACTIONS, "verification_required": action in CONTROL_ACTIONS},
        }

    async def call(self, action: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in ALLOWED_ACTIONS:
            return self._error(action, WindowsMCPError("desktop_action_denied", "Desktop action is outside the explicit allowlist"))
        if not self._option("enabled", False):
            return self._error(action, WindowsMCPError("desktop_disabled", "Windows-MCP is disabled; enable only after reviewing user-session deployment"))
        if action in CONTROL_ACTIONS and not self._option("control_enabled", False):
            return self._error(action, WindowsMCPError("desktop_control_disabled", "Windows desktop control requires explicit local enablement"))
        try:
            _validate_args(action, arguments if arguments is not None else {})
            # Check again for each operation: a persistent child can outlive a
            # lock, UAC prompt, disconnected session or the user's takeover.
            if self._option("transport", "stdio") == "stdio":
                state = interactive_desktop_state()
                if not state.get("available"):
                    raise WindowsMCPError(state["error_code"], "An unlocked interactive user desktop is required")
            else:
                self._http_config()
            # Serial ordering protects Snapshot labels and avoids concurrent input.
            async with self._lock:
                if self._worker is None or self._worker.done():
                    self._worker = asyncio.create_task(self._serve())
                future = asyncio.get_running_loop().create_future()
                await self._queue.put((action, arguments or {}, future))
                timeout = max(1.0, min(float(self._option("timeout", 30)), 120.0))
                try:
                    return await asyncio.wait_for(future, timeout=timeout)
                except TimeoutError:
                    await self.close()
                    return self._error(action, WindowsMCPError("desktop_timeout", "Desktop call timed out; inspect current state before retrying a control action", retryable=action in READ_ACTIONS))
        except WindowsMCPError as exc:
            return self._error(action, exc)

    @staticmethod
    def _error(action: str, exc: WindowsMCPError) -> dict[str, Any]:
        return {"ok": False, "status": "failed", "terminal": True, "task_id": None, "operation": "windows_" + action.casefold(), "result": {"summary": "", "data": None}, "artifacts": [], "error": {"code": exc.code, "message": str(exc)}, "error_code": exc.code, "retryable": exc.retryable and action in READ_ACTIONS, "evidence": {"checks": [], "verification_required": action in CONTROL_ACTIONS}}

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker