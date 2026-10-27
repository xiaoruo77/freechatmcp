from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import win32event
import win32service
import win32serviceutil


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


class ManagedProcessService(win32serviceutil.ServiceFramework):
    _command: list[str] = []
    _environment: dict[str, str] = {}
    _cwd: Path = ROOT
    # Subclasses can opt into an external health check.  A healthy child is
    # left alone; repeated failures restart only the child process, keeping the
    # Windows service itself available to SCM.
    _health_check_interval_seconds: float = 0.0
    _health_check_failures_before_restart: int = 3
    _health_check_timeout_seconds: float = 5.0
    _health_log_path: Path | None = None
    # A network-dependent child can start before the network stack, DNS, or
    # the local origin is ready. Keep the Windows service alive and retry the
    # child with bounded backoff instead of returning normally from SvcDoRun.
    _child_restart_delay_seconds: float = 10.0
    _child_restart_max_delay_seconds: float = 60.0

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.process: subprocess.Popen | None = None
        self._child_env: dict[str, str] | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        self._stop_child()

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        env = os.environ.copy()
        env.update(self._environment)
        env["WEB_AGENT_HOME"] = str(ROOT)
        env.setdefault("WEB_AGENT_WORKSPACE", str(ROOT))
        self._child_env = env
        self._start_child()
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        # Give a newly started child time to establish its transport before
        # the first watchdog probe.  This prevents startup from being treated
        # as a failed connection and restarted in a loop.
        last_health_check = time.monotonic()
        health_failures = 0
        restart_delay = self._child_restart_delay_seconds
        while True:
            result = win32event.WaitForSingleObject(self.stop_event, 1000)
            if result == win32event.WAIT_OBJECT_0:
                break
            if self.process is None or self.process.poll() is not None:
                exit_code = None if self.process is None else self.process.returncode
                self._write_health_log(f"child_exited exit_code={exit_code}")
                if win32event.WaitForSingleObject(
                    self.stop_event, int(restart_delay * 1000)
                ) == win32event.WAIT_OBJECT_0:
                    break
                try:
                    self._start_child()
                except Exception as exc:  # pragma: no cover - service-only path
                    self._write_health_log(
                        f"child_restart_failed error={type(exc).__name__}: {exc}"
                    )
                    restart_delay = min(
                        restart_delay * 2, self._child_restart_max_delay_seconds
                    )
                    continue
                self._write_health_log("child_restarted_after_exit")
                restart_delay = self._child_restart_delay_seconds
                last_health_check = time.monotonic()
                health_failures = 0
                continue
            interval = self._health_check_interval_seconds
            if interval > 0 and time.monotonic() - last_health_check >= interval:
                last_health_check = time.monotonic()
                try:
                    healthy = self._health_check()
                    error_text = ""
                except Exception as exc:  # pragma: no cover - service-only path
                    healthy = False
                    error_text = f"{type(exc).__name__}: {exc}"
                if healthy:
                    if health_failures:
                        self._write_health_log("health_check_recovered")
                    health_failures = 0
                else:
                    health_failures += 1
                    self._write_health_log(
                        "health_check_failed"
                        + (f" error={error_text}" if error_text else "")
                        + f" consecutive={health_failures}"
                    )
                    if health_failures >= self._health_check_failures_before_restart:
                        self._write_health_log("restarting_child_after_health_failures")
                        self._stop_child()
                        if win32event.WaitForSingleObject(self.stop_event, 0) == win32event.WAIT_OBJECT_0:
                            break
                        try:
                            self._start_child()
                        except Exception as exc:  # pragma: no cover - service-only path
                            self._write_health_log(
                                f"child_restart_failed error={type(exc).__name__}: {exc}"
                            )
                            restart_delay = min(
                                restart_delay * 2, self._child_restart_max_delay_seconds
                            )
                            continue
                        restart_delay = self._child_restart_delay_seconds
                        health_failures = 0
                        last_health_check = time.monotonic()
        self._stop_child()

    def _start_child(self):
        if self._child_env is None:
            raise RuntimeError("service child environment is not initialized")
        self.process = subprocess.Popen(
            self._command,
            cwd=str(self._cwd),
            env=self._child_env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def _health_check(self) -> bool:
        """Return whether the managed child is serving its configured path."""
        return True

    def _write_health_log(self, message: str):
        path = self._health_log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
        except OSError:
            # A logging failure must never take down the service watchdog.
            pass

    def _stop_child(self):
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.terminate()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass