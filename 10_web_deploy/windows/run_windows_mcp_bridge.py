"""User-session supervisor for the private Windows-MCP HTTP bridge.

Run with the project Python/pythonw after the isolated 3.14 runtime is installed.
No model API is used. The child listens only on 127.0.0.1:48888, and its Bearer
credential is read from a fixed private file into the child environment only.
This module has no startup side effects when imported by tests.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import psutil


HOST, PORT = "127.0.0.1", 48888
TOOLS = ("Snapshot", "Screenshot", "ControlStatus", "Click", "Type", "Shortcut", "Scroll", "App")
MAX_RESTARTS = 5
STABLE_SECONDS = 120.0
READY_TIMEOUT = 45.0
CONFIG_TEXT = '''# No credentials are stored in this dedicated configuration.
[server]
transport = "streamable-http"
host = "127.0.0.1"
port = 48888
allow_insecure_remote = false

[security]
ip_allowlist = ["127.0.0.1/32"]
cors_origins = []
'''


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def runtime(self):
        return self.root / "10_web_deploy" / "windows" / "windows_mcp_runtime"

    @property
    def state_dir(self):
        return self.runtime / "supervisor"

    @property
    def python(self):
        return self.runtime / "venv" / "Scripts" / "python.exe"

    @property
    def backend(self):
        return self.root / "10_web_deploy" / "windows" / "run_windows_mcp_backend.py"

    @property
    def token(self):
        return self.root / "08_web_data" / "windows_mcp.token"

    @property
    def config(self):
        return self.state_dir / "windows-mcp.toml"

    @property
    def pidfile(self):
        return self.state_dir / "pid.json"

    @property
    def stopfile(self):
        return self.state_dir / "stop-request.json"

    @property
    def lockfile(self):
        return self.state_dir / "instance.lock"

    @property
    def logfile(self):
        return self.root / "09_web_logs" / "windows_mcp_bridge.jsonl"

    @property
    def private_diagnostics(self):
        return self.state_dir / "private_diagnostics"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 32768:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_token(path: Path) -> str:
    try:
        if path.stat().st_size > 4096:
            raise LauncherError("desktop_token_invalid")
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise LauncherError("desktop_token_unavailable") from exc
    if len(token) < 20 or any(character.isspace() or ord(character) < 32 for character in token):
        raise LauncherError("desktop_token_invalid")
    return token


def session_id(pid: int) -> int:
    if os.name != "nt":
        raise LauncherError("windows_required")
    value = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(value)):
        raise LauncherError("desktop_session_unavailable")
    return int(value.value)


def session_active(identifier: int) -> bool:
    if os.name != "nt" or identifier == 0:
        return False
    api = ctypes.WinDLL("wtsapi32", use_last_error=True)
    api.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    api.WTSQuerySessionInformationW.restype = wintypes.BOOL
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    data, size = ctypes.c_void_p(), wintypes.DWORD()
    if not api.WTSQuerySessionInformationW(None, identifier, 8, ctypes.byref(data), ctypes.byref(size)):
        return False
    try:
        return size.value >= ctypes.sizeof(ctypes.c_int) and ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value == 0
    finally:
        api.WTSFreeMemory(data)


def require_user_session() -> int:
    identifier = session_id(os.getpid())
    if identifier == 0 or not session_active(identifier):
        raise LauncherError("interactive_user_session_required")
    identity = psutil.Process().username().rsplit("\\", 1)[-1].upper()
    if identity in {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "LOCALSERVICE", "NETWORKSERVICE"}:
        raise LauncherError("interactive_user_session_required")
    return identifier


def process_identity(pid: int, process_factory=psutil.Process, session_reader=session_id) -> dict:
    process = process_factory(pid)
    argv = process.cmdline()
    return {"pid": pid, "create_time": process.create_time(), "exe": os.path.normcase(os.path.realpath(process.exe())),
            "command_hash": hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest(),
            "session_id": session_reader(pid)}


def identity_matches(record: Any, identity_reader=process_identity) -> bool:
    if not isinstance(record, dict) or type(record.get("pid")) is not int:
        return False
    try:
        actual = identity_reader(record["pid"])
        return all(actual.get(key) == record.get(key) for key in ("pid", "create_time", "exe", "command_hash", "session_id"))
    except (psutil.Error, OSError, LauncherError, ValueError, TypeError):
        return False


@contextmanager
def instance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LauncherError("desktop_supervisor_already_running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_runtime(paths: Paths):
    try:
        version = (paths.runtime / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
        metadata = (paths.runtime / "venv" / "Lib" / "site-packages" / "windows_mcp-0.8.8.dist-info" / "METADATA")
        if not paths.python.is_file() or not paths.backend.is_file() or not re.search(r"(?m)^version(?:_info)?\s*=\s*3\.14(?:\.|\s*$)", version) or not metadata.is_file():
            raise LauncherError("desktop_isolated_runtime_not_ready")
    except OSError as exc:
        raise LauncherError("desktop_isolated_runtime_not_ready") from exc
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    if paths.config.exists():
        if paths.config.read_text(encoding="utf-8") != CONFIG_TEXT:
            raise LauncherError("desktop_dedicated_config_changed")
    else:
        paths.config.write_text(CONFIG_TEXT, encoding="utf-8")
    ensure_private_diagnostics(paths.private_diagnostics)


def ensure_private_diagnostics(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
        return
    options = hidden_process_options()
    options.update(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, **options)
    try:
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        if not re.fullmatch(r"S-1-[0-9-]+", sid):
            raise ValueError("invalid SID")
    except (ValueError, IndexError, StopIteration) as exc:
        raise LauncherError("desktop_private_diagnostic_acl_failed") from exc
    subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   check=True, **hidden_process_options())


def child_command(paths: Paths) -> list[str]:
    return [str(paths.python), "-I", "-X", "utf8", str(paths.backend), "serve", "--config", str(paths.config),
            "--transport", "streamable-http", "--host", HOST, "--port", str(PORT),
            "--ip-allowlist", "127.0.0.1/32", "--tools", ",".join(TOOLS)]


def child_environment(token: str, source: dict | None = None, diagnostics_path: Path | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
               "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "TEMP", "TMP", "SESSIONNAME"}
    environment = {key: str(value) for key, value in source.items() if key.upper() in allowed}
    environment.update({"ANONYMIZED_TELEMETRY": "false", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1",
                        "WINDOWS_MCP_AUTH_KEY": token})
    if diagnostics_path is not None:
        environment["WEB_AGENT_WINDOWS_MCP_APP_TRACE_DIR"] = str(diagnostics_path)
    return environment


def hidden_process_options() -> dict:
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return options


class SafeEventLog:
    """Write only fixed operational metadata, never upstream output or secrets."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("windows-mcp-launcher-" + uuid.uuid4().hex)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)

    def __call__(self, event: str, **fields):
        safe = {"timestamp_utc": utc_now(), "event": event if re.fullmatch(r"[a-z_]{1,64}", event) else "launcher_event"}
        for key in ("pid", "child_pid", "restart_count", "delay_seconds", "exit_code", "session_id"):
            if key in fields and isinstance(fields[key], (int, float)):
                safe[key] = fields[key]
        for key in ("reason", "exception_type"):
            if key in fields and re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(fields[key])):
                safe[key] = fields[key]
        self.logger.info(json.dumps(safe))

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()


class KillOnCloseJob:
    """Keep backend descendants tied to this user supervisor across logoff/crash."""
    def __init__(self, child):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_user_time", ctypes.c_longlong), ("job_user_time", ctypes.c_longlong), ("flags", wintypes.DWORD),
                        ("minimum_working_set", ctypes.c_size_t), ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority_class", wintypes.DWORD), ("scheduling_class", wintypes.DWORD)]
        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IOCounters), ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise LauncherError("desktop_job_unavailable")
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(child._handle))):
            self.close()
            raise LauncherError("desktop_job_unavailable")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def port_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, PORT))
        return True
    except OSError:
        return False


def child_listener_pid(pid: int, process_factory=psutil.Process) -> int | None:
    """Find the bound server in this Popen tree, including uv's redirector.

    On Windows a uv-created venv python.exe can remain as a waiting launcher
    while the real interpreter descendant owns the socket. A global port
    probe alone would also accept an unrelated process racing for the port.
    """
    try:
        root = process_factory(pid)
        processes = [root, *root.children(recursive=True)]
        for process in processes:
            try:
                if any(connection.status == "LISTEN" and connection.laddr.ip == HOST and connection.laddr.port == PORT
                       for connection in process.net_connections(kind="tcp")):
                    return process.pid
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        pass
    return None


def child_listening(pid: int) -> bool:
    return child_listener_pid(pid) is not None


def restart_delay(failures: int) -> float:
    return min(30.0, 2.0 ** min(max(failures - 1, 0), 5))


def status(paths: Paths, matcher=identity_matches) -> dict:
    record = read_json(paths.pidfile)
    owner_valid = matcher(record.get("owner"))
    child_valid = matcher(record.get("child")) if owner_valid else False
    listener_valid = matcher(record.get("listener")) if child_valid else False
    return {"running": owner_valid, "backend_running": child_valid, "status": record.get("status", "stopped") if owner_valid else "stopped",
            "endpoint": f"http://{HOST}:{PORT}/mcp", "generation": record.get("generation"),
            "owner": record.get("owner"), "child": record.get("child") if child_valid else None,
            "listener_running": listener_valid, "listener": record.get("listener") if listener_valid else None,
            "last_reason": record.get("reason"), "updated_at": record.get("updated_at"), "pidfile": str(paths.pidfile), "logfile": str(paths.logfile)}


def request_stop(paths: Paths, matcher=identity_matches) -> dict:
    record = read_json(paths.pidfile)
    if not matcher(record.get("owner")) or not isinstance(record.get("generation"), str):
        return {"stop_requested": False, "reason": "no_verified_supervisor"}
    atomic_json(paths.stopfile, {"generation": record["generation"], "owner": record["owner"], "requested_at": utc_now()})
    return {"stop_requested": True, "generation": record["generation"]}


class Supervisor:
    def __init__(self, paths: Paths, *, logger, popen=subprocess.Popen, session_check=require_user_session,
                 runtime_check=ensure_runtime, identity_reader=process_identity, matcher=identity_matches,
                 free_port=port_available, ready=child_listening, listener_reader=child_listener_pid,
                 job_factory=KillOnCloseJob, clock=time.monotonic):
        self.paths, self.log, self.popen = paths, logger, popen
        self.session_check, self.runtime_check, self.identity_reader, self.matcher = session_check, runtime_check, identity_reader, matcher
        self.free_port, self.ready, self.listener_reader, self.job_factory, self.clock = free_port, ready, listener_reader, job_factory, clock
        self.stopping = threading.Event()
        self.generation = uuid.uuid4().hex
        self.record = {}
        self.child = self.job = None
        self.child_identity_pending = False

    def publish(self, state: str, reason: str | None = None):
        self.record.update(status=state, reason=reason, updated_at=utc_now())
        atomic_json(self.paths.pidfile, self.record)

    def should_stop(self) -> str | None:
        if self.stopping.is_set():
            return "stop_signal"
        request = read_json(self.paths.stopfile)
        if request.get("generation") == self.generation and request.get("owner") == self.record.get("owner"):
            return "stop_requested"
        try:
            if self.session_check() != self.record["owner"]["session_id"]:
                return "user_session_ended"
        except (LauncherError, OSError, psutil.Error):
            return "user_session_ended"
        return None

    def wait(self, duration: float) -> str | None:
        end = self.clock() + duration
        while self.clock() < end:
            reason = self.should_stop()
            if reason:
                return reason
            self.stopping.wait(min(.5, max(0, end - self.clock())))
        return self.should_stop()

    def stop_child(self):
        try:
            if self.child is not None and self.child.poll() is None:
                # A new Popen's original Windows process handle remains safe if
                # identity inspection itself failed before a record was made.
                # No Popen object is ever reconstructed from a persisted PID.
                if self.child_identity_pending or self.matcher(self.record.get("child")):
                    self.child.terminate()
                    try:
                        self.child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        if self.child_identity_pending or self.matcher(self.record.get("child")):
                            self.child.kill()
                            self.child.wait(timeout=5)
                else:
                    self.log("child_identity_changed", reason="termination_refused")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log("child_stop_failed", exception_type=type(exc).__name__)
        finally:
            if self.job is not None:
                # This handle owns only children assigned at creation, never a
                # PID looked up from a stale file.
                self.job.close()
            self.child = self.job = None
            self.child_identity_pending = False

    def run(self) -> int:
        try:
            identifier = self.session_check()
            self.runtime_check(self.paths)
            with instance_lock(self.paths.lockfile):
                self.record = {"generation": self.generation, "owner": self.identity_reader(os.getpid()), "child": None, "listener": None,
                               "endpoint": f"http://{HOST}:{PORT}/mcp", "started_at": utc_now()}
                if identifier != self.record["owner"]["session_id"]:
                    raise LauncherError("desktop_session_changed")
                self.publish("starting")
                return self.run_locked()
        except LauncherError as exc:
            self.log("supervisor_refused", reason=str(exc))
            return 2
        except (OSError, UnicodeError, psutil.Error) as exc:
            self.log("supervisor_start_failed", exception_type=type(exc).__name__)
            return 2

    def run_locked(self) -> int:
        failures = 0
        reason = "supervisor_exited"
        try:
            while True:
                reason = self.should_stop()
                if reason:
                    return 0
                if not self.free_port():
                    reason = "desktop_port_in_use"
                    self.log("supervisor_refused", reason=reason)
                    return 2
                try:
                    token = read_token(self.paths.token)
                    environment = child_environment(token, diagnostics_path=self.paths.private_diagnostics)
                    self.child = self.popen(child_command(self.paths), env=environment, cwd=str(self.paths.root), **hidden_process_options())
                    self.child_identity_pending = True
                    del token, environment
                    self.record["child"] = self.identity_reader(self.child.pid)
                    self.child_identity_pending = False
                    self.job = self.job_factory(self.child)
                    if self.record["child"]["session_id"] != self.record["owner"]["session_id"]:
                        raise LauncherError("desktop_child_session_mismatch")
                    self.publish("starting_backend")
                    self.log("backend_started", child_pid=self.child.pid, session_id=self.record["child"]["session_id"])
                    started = self.clock()
                    listening = False
                    while self.child.poll() is None:
                        reason = self.should_stop()
                        if reason:
                            return 0
                        if not listening and self.ready(self.child.pid):
                            listener_pid = self.listener_reader(self.child.pid)
                            if listener_pid is not None:
                                listener_identity = self.identity_reader(listener_pid)
                                if listener_identity["session_id"] != self.record["owner"]["session_id"]:
                                    raise LauncherError("desktop_listener_session_mismatch")
                                self.record["listener"] = listener_identity
                                listening = True
                                self.publish("running")
                                self.log("backend_listening", child_pid=listener_pid, session_id=listener_identity["session_id"])
                        if not listening and self.clock() - started >= READY_TIMEOUT:
                            reason = "desktop_backend_start_timeout"
                            break
                        self.stopping.wait(.5)
                    if self.clock() - started >= STABLE_SECONDS:
                        failures = 0
                    exit_code = self.child.poll()
                    self.log("backend_exited", exit_code=exit_code if exit_code is not None else -1)
                    reason = reason if reason == "desktop_backend_start_timeout" else "desktop_backend_exited"
                except LauncherError as exc:
                    reason = str(exc)
                    self.log("backend_refused", reason=reason)
                    if reason in {"desktop_token_unavailable", "desktop_token_invalid"}:
                        return 2
                except (OSError, psutil.Error) as exc:
                    reason = "desktop_backend_start_failed"
                    self.log("backend_start_failed", reason=reason, exception_type=type(exc).__name__)
                finally:
                    self.stop_child()
                    self.record["child"] = None
                    self.record["listener"] = None
                failures += 1
                if failures > MAX_RESTARTS:
                    reason = "desktop_restart_limit_reached"
                    self.log("restart_limit_reached", restart_count=failures)
                    return 1
                delay = restart_delay(failures)
                self.publish("backoff", reason)
                self.log("backend_restart_scheduled", restart_count=failures, delay_seconds=delay)
                stop_reason = self.wait(delay)
                if stop_reason:
                    reason = stop_reason
                    return 0
        finally:
            self.stop_child()
            self.record["child"] = None
            self.record["listener"] = None
            self.publish("stopped", reason)
            self.log("supervisor_stopped", reason=reason)


def public_config(paths: Paths) -> dict:
    return {"endpoint": f"http://{HOST}:{PORT}/mcp", "transport": "streamable-http", "allowed_tools": list(TOOLS),
            "token_file": str(paths.token), "authentication": "Bearer from child environment; never command line",
            "python": str(paths.python), "backend_bootstrap": str(paths.backend), "config_file": str(paths.config), "pidfile": str(paths.pidfile), "logfile": str(paths.logfile),
            "max_restarts": MAX_RESTARTS, "max_backoff_seconds": 30, "session": "active logged-in user; Session 0 rejected"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", action="store_true")
    actions.add_argument("--show-config", action="store_true")
    args = parser.parse_args(argv)
    paths = Paths(Path(__file__).resolve().parents[2])
    if args.status:
        print(json.dumps(status(paths), ensure_ascii=False))
        return 0
    if args.stop:
        result = request_stop(paths)
        print(json.dumps(result))
        return 0 if result["stop_requested"] else 1
    if args.show_config:
        print(json.dumps(public_config(paths), ensure_ascii=False, indent=2))
        return 0
    logger = SafeEventLog(paths.logfile)
    supervisor = Supervisor(paths, logger=logger)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: supervisor.stopping.set())
    try:
        return supervisor.run()
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())