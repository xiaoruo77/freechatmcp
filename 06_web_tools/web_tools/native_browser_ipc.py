"""Authenticated, bounded local attachment to an already running Xiaoruo v72.

No process, browser context, TCP listener, or remote debugging endpoint is
created. Discovery credentials remain private and are never included in a
descriptor or exception. Each workflow releases the browser's single pipe.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import ctypes
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any
import uuid
import weakref

from .browser_process_identity import XIAORUO_EXE

PROTOCOL = "xiaoruo-browser-control/1.0"
MCP_PROTOCOL = "2025-11-25"
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
_PIPE = re.compile(r"\\\\\.\\pipe\\xiaoruo-browser-([1-9][0-9]*)-([0-9a-fA-F-]{36})\Z")
_FILE = re.compile(r"browser-([1-9][0-9]*)-([0-9]+)\.json\Z")
_TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class NativeIPCError(RuntimeError):
    """Only static, credential-free messages cross the adapter boundary."""

    def __init__(self, code: str, message: str, *, task_id: str | None = None,
                 unknown: bool = False):
        super().__init__(message)
        self.code, self.task_id, self.unknown = code, task_id, unknown

    def as_result(self) -> dict[str, Any]:
        value = {"ok": False, "status": "unknown" if self.unknown else "failed",
                 "error": {"code": self.code, "message": str(self)}, "retryable": False}
        if self.task_id:
            value["task_id"] = self.task_id
        return value


def _timeout() -> NativeIPCError:
    return NativeIPCError("native_ipc_timeout", "The local browser IPC deadline expired")


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise _timeout()
    return value


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise NativeIPCError("invalid_argument", "IPC timeout must be from 0 to 300 seconds")
    return time.monotonic() + timeout


def _path(value: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(value))).casefold()


def _no_reparse(path: Path) -> None:
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("discovery reparse point")


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", info.st_ctime_ns),
            info.st_mtime_ns, info.st_size)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-JSON numeric constant")


def _read_discovery(path: Path) -> tuple[dict, tuple, float]:
    _no_reparse(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
        raise ValueError("invalid discovery file")
    # OPEN_REPARSE_POINT prevents following a substituted Windows symlink.
    if os.name == "nt":
        import msvcrt
        api = _kernel()
        handle = api.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ValueError("unreadable discovery")
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(before) != _identity(opened):
            raise ValueError("discovery changed")
        raw = stream.read(65537)
    _no_reparse(path)
    if len(raw) > 65536 or _identity(before) != _identity(path.lstat()):
        raise ValueError("discovery changed")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid discovery")
    return value, _identity(before), getattr(before, "st_birthtime", before.st_ctime)


@dataclass(frozen=True, slots=True)
class NativeBrowserInstance:
    pid: int
    instance_id: str
    executable: str
    permissions: tuple[str, ...]
    process_created: float
    _pipe_name: str = field(repr=False)
    _auth_token: str = field(repr=False)
    _discovery_path: Path = field(repr=False)
    _file_identity: tuple = field(repr=False)
    _trusted_executable: str = field(repr=False)
    _process_api: Any = field(repr=False, compare=False)

    def public_descriptor(self) -> dict[str, Any]:
        return {"instance_id": self.instance_id, "process_id": self.pid,
                "browser_id": "native_" + self.instance_id,
                "connection_generation": hashlib.sha256(f"{self.pid}|{self.instance_id}|{self.process_created}".encode()).hexdigest(),
                "browser_kind": "xiaoruo_browser", "manual_instance": True,
                "executable": self.executable, "protocol": PROTOCOL,
                "transport": "native_named_pipe", "permissions": list(self.permissions),
                "identity_source": "verified_native_ipc_discovery"}

    def _validate(self) -> None:
        try:
            data, identity, _ = _read_discovery(self._discovery_path)
            process = self._process_api.Process(self.pid)
            if (identity != self._file_identity or not process.is_running()
                    or process.create_time() != self.process_created
                    or _path(process.exe()) != self._trusted_executable
                    or data.get("pipeName") != self._pipe_name
                    or data.get("authToken") != self._auth_token
                    or data.get("pid") != self.pid or data.get("instanceId") != self.instance_id):
                raise ValueError("identity changed")
        except Exception:
            raise NativeIPCError("native_instance_stale", "Browser instance identity changed; discover it again") from None

    def _redact(self, value):
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()
                    if key.casefold() not in {"authtoken", "pipename"}}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(re.escape(self._auth_token), "[redacted]", value, flags=re.IGNORECASE)
            for private in (self._pipe_name, json.dumps(self._pipe_name)[1:-1]):
                value = re.sub(re.escape(private), "[redacted]", value, flags=re.IGNORECASE)
            return value
        return value


class NativeBrowserDiscovery:
    def __init__(self, discovery_root: str | Path | None = None,
                 trusted_executable: str | Path | None = None, *, psutil_api=None):
        local = os.environ.get("LOCALAPPDATA")
        configured = discovery_root or os.environ.get("WEB_AGENT_XIAORUO_IPC_DISCOVERY_ROOT")
        self.root = Path(configured) if configured else (Path(local) / "xiaoruo-browser/ipc/sessions" if local else None)
        self.trusted_executable = Path(trusted_executable or os.environ.get("WEB_AGENT_XIAORUO_BROWSER_EXECUTABLE") or XIAORUO_EXE)
        if psutil_api is None:
            import psutil as psutil_api
        self._process_api = psutil_api

    def discover(self) -> list[NativeBrowserInstance]:
        if self.root is None or not self.root.is_absolute() or not self.trusted_executable.is_absolute():
            return []
        try:
            _no_reparse(self.root)
            files = list(self.root.glob("browser-*.json"))[:256]
        except (OSError, ValueError):
            return []
        found = []
        for path in files:
            try:
                data, identity, birth = _read_discovery(path)
                pid = data.get("pid")
                match, pipe = _FILE.fullmatch(path.name), _PIPE.fullmatch(data.get("pipeName", ""))
                if (data.get("schema") != "xiaoruo-browser-control-discovery/1"
                        or data.get("product") != "xiaoruo-browser" or data.get("protocol") != PROTOCOL
                        or data.get("mcpProtocol") not in {MCP_PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05"}
                        or type(pid) is not int or pid <= 0 or not match or int(match[1]) != pid
                        or not pipe or int(pipe[1]) != pid
                        or not isinstance(data.get("authToken"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", data["authToken"])
                        or not isinstance(data.get("instanceId"), str) or not re.fullmatch(r"[0-9a-fA-F]{32}", data["instanceId"])
                        or not isinstance(data.get("executable"), str) or not Path(data["executable"]).is_absolute()
                        or not isinstance(data.get("permissions"), list)
                        or any(not isinstance(item, str) for item in data["permissions"])):
                    continue
                uuid.UUID(pipe[2])
                process = self._process_api.Process(pid)
                created = process.create_time()
                expected = _path(self.trusted_executable)
                if (not process.is_running() or _path(process.exe()) != expected
                        or _path(data["executable"]) != expected or birth + 2 < created
                        or any(str(arg).casefold().startswith("--mcp-stdio") for arg in process.cmdline())):
                    continue
                found.append(NativeBrowserInstance(pid, data["instanceId"], str(self.trusted_executable),
                    tuple(data["permissions"]), created, data["pipeName"], data["authToken"], path,
                    identity, expected, self._process_api))
            except Exception:
                # Missing, stale, hostile, or access-denied discovery is never
                # reported with file contents or a guessed process identity.
                continue
        return sorted(found, key=lambda item: (item.pid, item.instance_id))


def _kernel():
    if os.name != "nt":
        raise NativeIPCError("native_ipc_unsupported", "Native browser IPC requires Windows")
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateEventW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    api.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    api.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    api.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.WriteFile.argtypes = list(api.ReadFile.argtypes)
    return api


class _WinPipeTransport:
    """Win32 overlapped I/O; no blocking file read can outlive the deadline."""

    def __init__(self, instance: NativeBrowserInstance, deadline: float):
        from ctypes import wintypes
        self.api, self.handle, self.buffer = _kernel(), None, bytearray()
        self._pending = []
        while True:
            _remaining(deadline)
            handle = self.api.CreateFileW(instance._pipe_name, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                break
            error = ctypes.get_last_error()
            if error not in {2, 231}:
                raise NativeIPCError("native_ipc_connect", "Cannot connect to the local browser pipe")
            self.api.WaitNamedPipeW(instance._pipe_name, max(1, min(50, int(_remaining(deadline) * 1000))))
            # A missing pipe makes WaitNamedPipe return immediately.
            if error == 2:
                time.sleep(min(.025, _remaining(deadline)))
        try:
            actual = wintypes.ULONG()
            if not self.api.GetNamedPipeServerProcessId(self.handle, ctypes.byref(actual)) or actual.value != instance.pid:
                raise NativeIPCError("native_ipc_identity", "Named pipe server does not match the selected browser")
            instance._validate()
        except BaseException:
            self.close()
            raise

    def cancel(self):
        if self.handle:
            self.api.CancelIoEx(self.handle, None)

    def close(self):
        if self.handle:
            self.cancel()
            self.api.CloseHandle(self.handle)
            self.handle = None
        for event, *_ in self._pending:
            self.api.CloseHandle(event)
        self._pending.clear()

    def _io(self, payload, size: int, deadline: float, write: bool):
        from ctypes import wintypes
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        if not self.handle:
            raise NativeIPCError("native_ipc_eof", "Local browser pipe is closed")
        wait_ms = max(1, int(_remaining(deadline) * 1000))
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise NativeIPCError("native_ipc_io", "Cannot create a local IPC wait event")
        ov, count = Overlapped(), wintypes.DWORD()
        ov.hEvent = event
        memory = ctypes.create_string_buffer(payload) if write else ctypes.create_string_buffer(size)
        retained = False
        try:
            operation = self.api.WriteFile if write else self.api.ReadFile
            done = operation(self.handle, memory, size, ctypes.byref(count), ctypes.byref(ov))
            if not done:
                error = ctypes.get_last_error()
                if error != 997:
                    raise NativeIPCError("native_ipc_eof" if error in {109, 232, 233} else "native_ipc_io",
                                         "Local browser pipe closed or failed")
                wait = self.api.WaitForSingleObject(event, wait_ms)
                if wait != 0:
                    self.api.CancelIoEx(self.handle, ctypes.byref(ov))
                    if self.api.WaitForSingleObject(event, 1000) != 0:
                        self._pending.append((event, ov, memory))
                        retained = True
                    raise _timeout()
                if not self.api.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(count), False):
                    error = ctypes.get_last_error()
                    raise NativeIPCError("native_ipc_eof" if error in {109, 232, 233} else "native_ipc_io",
                                         "Local browser pipe closed or failed")
            if count.value == 0:
                raise NativeIPCError("native_ipc_eof", "Local browser pipe reached EOF")
            return count.value if write else memory.raw[:count.value]
        finally:
            if not retained:
                self.api.CloseHandle(event)

    def write(self, data: bytes, deadline: float):
        offset = 0
        while offset < len(data):
            offset += self._io(data[offset:], len(data) - offset, deadline, True)

    def readline(self, deadline: float) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_RESPONSE:
                    raise NativeIPCError("native_ipc_limit", "Local browser response exceeds the size limit")
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return line
            if len(self.buffer) > MAX_RESPONSE:
                raise NativeIPCError("native_ipc_limit", "Local browser response exceeds the size limit")
            self.buffer.extend(self._io(None, min(65536, MAX_RESPONSE + 1 - len(self.buffer)), deadline, False))


class _FileLease:
    """A non-blocking OS file lock, shared by clients in separate processes."""

    def __init__(self, key: str):
        self.key, self.file = key, None

    def acquire(self, deadline):
        root = Path(tempfile.gettempdir()) / "xiaoruo-native-ipc-client-locks"
        root.mkdir(exist_ok=True)
        _no_reparse(root)
        path = root / (self.key + ".lock")
        if path.exists():
            _no_reparse(path)
        self.file = open(path, "a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"\0")
            self.file.flush()
        try:
            while True:
                try:
                    self.file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NativeIPCError("native_ipc_busy", "Another local client is using this browser") from None
                    time.sleep(min(.025, remaining))
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def close(self):
        if self.file:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


class _Session:
    def __init__(self, instance, transport, deadline):
        self.instance, self.transport, self.deadline, self.counter = instance, transport, deadline, 0
        self.write_started = False

    async def _blocking(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.transport.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(task)
            raise

    async def rpc(self, method, params=None, *, notification=False):
        self.write_started = False
        request = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        if not notification:
            self.counter += 1
            request["id"] = self.counter
        try:
            raw = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        except (ValueError, TypeError, UnicodeError):
            raise NativeIPCError("invalid_argument", "IPC arguments must be valid JSON") from None
        if len(raw) > MAX_REQUEST:
            raise NativeIPCError("native_ipc_limit", "Local browser request exceeds 1 MiB")
        self.write_started = True
        await self._blocking(self.transport.write, raw, self.deadline)
        if notification:
            return None
        while True:
            raw = await self._blocking(self.transport.readline, self.deadline)
            if len(raw) > MAX_RESPONSE:
                raise NativeIPCError("native_ipc_limit", "Local browser response exceeds the size limit")
            try:
                response = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise NativeIPCError("native_ipc_protocol", "Local browser returned invalid UTF-8 JSON") from None
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise NativeIPCError("native_ipc_protocol", "Local browser returned an invalid RPC envelope")
            if "id" not in response and isinstance(response.get("method"), str):
                _remaining(self.deadline)
                continue
            if type(response.get("id")) is not int or response["id"] != self.counter:
                raise NativeIPCError("native_ipc_protocol", "Local browser returned a mismatched RPC ID")
            if "error" in response:
                code = response["error"].get("code") if isinstance(response["error"], dict) else None
                raise NativeIPCError("native_auth_failed" if code == -32001 else "native_ipc_rpc_error",
                                     "Local browser rejected the IPC request")
            if "result" not in response:
                raise NativeIPCError("native_ipc_protocol", "Local browser returned no RPC result")
            return self.instance._redact(response["result"])

    async def initialize(self):
        auth = await self.rpc("xiaoruo/auth", {"token": self.instance._auth_token})
        if not isinstance(auth, dict) or auth.get("authenticated") is not True or auth.get("protocol") != PROTOCOL:
            raise NativeIPCError("native_auth_failed", "Local browser did not confirm IPC authentication")
        initialized = await self.rpc("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                    "clientInfo": {"name": "xiaoruo1-native-attachment", "version": "1.0"}})
        if not isinstance(initialized, dict) or initialized.get("protocolVersion") not in {MCP_PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05"}:
            raise NativeIPCError("native_ipc_protocol", "Local browser MCP protocol is incompatible")
        await self.rpc("notifications/initialized", notification=True)

    async def call_tool(self, name: str, arguments: dict | None = None):
        if not isinstance(name, str) or not isinstance(arguments if arguments is not None else {}, dict):
            raise NativeIPCError("invalid_argument", "A native tool name and argument object are required")
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise NativeIPCError("native_ipc_protocol", "Local browser returned an invalid tool result")
        structured = result.get("structuredContent")
        images, texts, parsed_texts = [], [], []
        for entry in result.get("content", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "image" and entry.get("mimeType") == "image/png" and isinstance(entry.get("data"), str):
                images.append({"type": "image", "mimeType": "image/png", "data": entry["data"]})
            if entry.get("type") == "text" and isinstance(entry.get("text"), str):
                text = entry["text"]
                try:
                    value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                    value = self.instance._redact(value)
                    parsed_texts.append(value)
                    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                except (ValueError, RecursionError):
                    pass
                texts.append(text)
        if not isinstance(structured, dict):
            structured = next((value for value in parsed_texts if isinstance(value, dict)), {})
        return {"structured": structured if isinstance(structured, dict) else {}, "images": images,
                "texts": texts, "is_error": result.get("isError") is True}


class NativeBrowserClient:
    def __init__(self, instance: NativeBrowserInstance, *, transport_factory=None, lease_factory=None):
        self.instance = instance
        self._transport_factory = transport_factory or _WinPipeTransport
        self._lease_factory = lease_factory or _FileLease

    @asynccontextmanager
    async def session(self, timeout: float = 30):
        deadline = _deadline(timeout)
        key = hashlib.sha256(f"{self.instance.pid}|{self.instance.instance_id}|{self.instance.process_created}".encode()).hexdigest()
        locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
        lock = locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), _remaining(deadline))
        except asyncio.TimeoutError:
            raise NativeIPCError("native_ipc_busy", "Another local client is using this browser") from None
        lease, transport = self._lease_factory(key), None
        try:
            self.instance._validate()
            # Shield acquisition so cancellation cannot leak a lock or pipe
            # returned by an otherwise still-running executor operation.
            acquire = asyncio.create_task(asyncio.to_thread(lease.acquire, deadline))
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(acquire)
                raise
            opening = asyncio.create_task(asyncio.to_thread(self._transport_factory, self.instance, deadline))
            try:
                transport = await asyncio.shield(opening)
            except asyncio.CancelledError:
                with suppress(Exception, asyncio.CancelledError):
                    transport = await asyncio.shield(opening)
                raise
            session = _Session(self.instance, transport, deadline)
            await session.initialize()
            yield session
        finally:
            if transport is not None:
                transport.close()
            lease.close()
            lock.release()

    async def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 30):
        async with self.session(timeout) as session:
            return await session.call_tool(name, arguments)

    async def tools(self, timeout: float = 30):
        async with self.session(timeout) as session:
            return await session.rpc("tools/list", {})

    async def instance_info(self, timeout: float = 30):
        return (await self.call_tool("xiaoruo_browser_instance", timeout=timeout))["structured"]

    async def tabs(self, timeout: float = 30):
        return (await self.call_tool("xiaoruo_browser_tabs", timeout=timeout))["structured"]

    async def capabilities(self, timeout: float = 30):
        return (await self.call_tool("xiaoruo_browser_capabilities", timeout=timeout))["structured"]

    async def execute(self, action: str, arguments: dict | None = None, timeout: float = 30):
        if not isinstance(action, str) or not action or not isinstance(arguments if arguments is not None else {}, dict):
            return NativeIPCError("invalid_argument", "A native action and argument object are required").as_result()
        task_id, submitted, session = None, False, None
        try:
            async with self.session(timeout) as session:
                args = dict(arguments or {})
                args["action"] = action
                args.setdefault("timeout_ms", max(1000, min(120000, int(_remaining(session.deadline) * 1000))))
                session.write_started = False
                response = await session.call_tool("xiaoruo_browser_start_task", args)
                submitted = True
                structured = response["structured"]
                task = structured.get("task")
                if response["is_error"] or not isinstance(task, dict):
                    return {"ok": False, "status": "failed", "error": {"code": "native_task_rejected",
                            "message": "Local browser rejected the task"}, "structured": structured,
                            "images": response["images"], "retryable": False}
                task_id = task.get("id") or task.get("taskId") or task.get("task_id")
                if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
                    raise NativeIPCError("native_ipc_protocol", "Local browser returned no valid task ID", unknown=True)
                while task.get("state") not in _TERMINAL:
                    if task.get("state") not in {"accepted", "running"}:
                        raise NativeIPCError("native_ipc_protocol", "Local browser returned an unknown task state", task_id=task_id, unknown=True)
                    await asyncio.sleep(min(.1, _remaining(session.deadline)))
                    response = await session.call_tool("xiaoruo_browser_task_status", {"task_id": task_id})
                    structured = response["structured"]
                    task = structured.get("task")
                    if response["is_error"] or not isinstance(task, dict) or (task.get("id") or task.get("taskId") or task.get("task_id")) != task_id:
                        raise NativeIPCError("native_ipc_protocol", "Local browser task status did not match the accepted task", task_id=task_id, unknown=True)
                ok = task["state"] == "succeeded" and not task.get("errorCode") and not task.get("error")
                value = {"ok": ok, "status": "succeeded" if ok else ("failed" if task["state"] == "succeeded" else task["state"]), "task_id": task_id,
                         "native_task": task, "structured": structured, "images": response["images"],
                         "retryable": False}
                if not ok:
                    value["error"] = {"code": task.get("errorCode") or "native_task_failed",
                                      "message": task.get("error") or "Local browser task did not succeed"}
                return value
        except NativeIPCError as error:
            if submitted or (session is not None and session.write_started):
                error.unknown = True
                error.task_id = task_id or error.task_id
            return error.as_result()