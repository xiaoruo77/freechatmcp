from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from web_config.settings import Settings
from web_mcp.paths import resolve_workspace_path

from .profiles import (
    RunnerCommand,
    cmd_command,
    md_simulation_command,
    production_md_command,
    powershell_command,
    project_build_command,
    python_command,
    terminal_command,
    wsl_command,
)
from .windows_user_process import run_command_as_active_user, should_use_active_user_context


@dataclass(slots=True)
class RunnerResult:
    status: str
    return_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    details: dict[str, Any] = field(default_factory=dict)


PROFILE_BUILDERS: dict[str, Callable[[dict, Settings, str], RunnerCommand]] = {
    "python_script": python_command,
    "wsl_workflow": wsl_command,
    "powershell_workflow": powershell_command,
    "cmd_workflow": cmd_command,
    "terminal_workflow": terminal_command,
    "project_build": project_build_command,
    "md_simulation": md_simulation_command,
    "production_md": production_md_command,
}

# Models frequently use the human-facing shell names when constructing a
# workflow, even though the public runner profiles use the explicit names
# above.  Keep the strict profile allow-list, but normalize the harmless
# aliases at the worker boundary so a valid workflow is not rejected before
# the first step runs.
PROFILE_ALIASES: dict[str, str] = {
    "py": "python_script",
    "python": "python_script",
    "wsl": "wsl_workflow",
    "linux": "wsl_workflow",
    "powershell": "powershell_workflow",
    "pwsh": "powershell_workflow",
    "cmd": "cmd_workflow",
    "command_prompt": "cmd_workflow",
    "terminal": "terminal_workflow",
    "shell": "terminal_workflow",
    "build": "project_build",
    "compile": "project_build",
    "md": "md_simulation",
    "gromacs": "md_simulation",
    "simulation": "md_simulation",
    "molecular_dynamics": "md_simulation",
    "production": "production_md",
    "production_md": "production_md",
    "final_md": "production_md",
}


def normalize_runner_profile(profile: str) -> str:
    normalized = str(profile or "").strip().lower()
    normalized = re.sub(r"[\s\-/\\–—−]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return PROFILE_ALIASES.get(normalized, normalized)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _normalize_check_type(value: Any, raw_check: dict[str, Any]) -> str:
    """Accept common model spellings while returning one safe internal type."""
    token = str(value or "").strip().lower()
    token = re.sub(r"[\s\-/\\–—−]+", "_", token)
    token = re.sub(r"_+", "_", token).strip("_")
    aliases = {
        "exists": None,
        "path_exists": None,
        "file": "file_exists",
        "exists_file": "file_exists",
        "is_file": "file_exists",
        "nonempty": "file_nonempty",
        "non_empty": "file_nonempty",
        "file_is_nonempty": "file_nonempty",
        "contains": "file_contains",
        "text_contains": "file_contains",
        "dir": "directory_exists",
        "folder": "directory_exists",
        "directory": "directory_exists",
        "exists_directory": "directory_exists",
        "is_directory": "directory_exists",
    }
    if token in {"exists", "path_exists"}:
        hint = str(raw_check.get("kind", raw_check.get("path_type", ""))).strip().lower()
        if hint in {"dir", "directory", "folder"}:
            return "directory_exists"
        path = str(raw_check.get("path", ""))
        if path.rstrip().endswith(("/", "\\")):
            return "directory_exists"
        return "file_exists"
    return aliases.get(token, token)

WORKFLOW_MAX_STEPS = 50
WORKFLOW_MAX_CHECKS = 50
WORKFLOW_MAX_ATTEMPTS = 3
WORKFLOW_MAX_RETRIES_PER_STEP = 3


class ProfileRunner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build(self, profile: str, payload: dict, task_id: str) -> RunnerCommand:
        profile = normalize_runner_profile(profile)
        builder = PROFILE_BUILDERS.get(profile)
        if builder is None:
            raise ValueError(f"Unknown runner profile: {profile}")
        return builder(payload, self.settings, task_id)

    async def run(
        self,
        profile: str,
        payload: dict,
        task_id: str,
        is_cancelled: Callable[[], Awaitable[bool]],
        on_output: Callable[[str, str], Awaitable[None]],
    ) -> RunnerResult:
        command = self.build(profile, payload, task_id)
        started = asyncio.get_running_loop().time()
        await on_output("INFO", command.description)
        await on_output("INFO", "Runner profile started")
        # ``terminal_workflow(shell="wsl")`` builds the same wsl.exe command
        # as the dedicated WSL profile.  It must use the active user's token
        # as well; otherwise a LocalSystem service gets
        # WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED.
        executable_name = Path(str(command.command[0])).name.lower() if command.command else ""
        is_wsl_command = executable_name in {"wsl", "wsl.exe"}
        if is_wsl_command and should_use_active_user_context():
            return await self._run_wsl_as_active_user(command, started, is_cancelled, on_output)
        creation_flags = getattr(asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = await asyncio.create_subprocess_exec(
            *command.command,
            cwd=str(command.cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creation_flags,
        )
        communicate = asyncio.create_task(process.communicate())
        timed_out = False
        cancelled = False
        while not communicate.done():
            done, _ = await asyncio.wait({communicate}, timeout=1)
            if done:
                break
            if await is_cancelled():
                cancelled = True
                await self._stop_process(process)
                break
            if asyncio.get_running_loop().time() - started > command.timeout:
                timed_out = True
                await self._stop_process(process)
                break
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(communicate, timeout=10)
        except asyncio.TimeoutError:
            await self._stop_process(process)
            stdout_bytes, stderr_bytes = await communicate
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if stdout:
            await on_output("INFO", f"stdout captured in output file ({len(stdout.encode('utf-8'))} bytes)")
        if stderr:
            await on_output("WARN", f"stderr captured in output file ({len(stderr.encode('utf-8'))} bytes)")
        duration = round(asyncio.get_running_loop().time() - started, 3)
        if cancelled:
            status = "cancelled"
        elif timed_out:
            status = "failed"
            stderr = (stderr + "\nTask timed out").strip()
        elif process.returncode == 0:
            status = "success"
        else:
            status = "failed"
        return RunnerResult(status, process.returncode, stdout, stderr, duration)

    def _normalize_workflow(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > WORKFLOW_MAX_STEPS:
            raise ValueError(f"workflow steps must be a non-empty list with at most {WORKFLOW_MAX_STEPS} items")

        steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(f"workflow step {index} must be an object")
            original_profile = str(raw_step.get("profile", "")).strip()
            profile = normalize_runner_profile(original_profile)
            if profile not in PROFILE_BUILDERS:
                available = ", ".join(sorted((*PROFILE_BUILDERS, *PROFILE_ALIASES)))
                raise ValueError(f"workflow step {index} has unsupported profile {original_profile!r}; use: {available}")
            step_payload = raw_step.get("payload", {})
            if not isinstance(step_payload, dict):
                raise ValueError(f"workflow step {index} payload must be an object")
            try:
                retries = int(raw_step.get("retries", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"workflow step {index} retries must be an integer") from exc
            if not 0 <= retries <= WORKFLOW_MAX_RETRIES_PER_STEP:
                raise ValueError(f"workflow step {index} retries must be between 0 and {WORKFLOW_MAX_RETRIES_PER_STEP}")
            label = str(raw_step.get("label", f"step-{index}")).strip() or f"step-{index}"
            if len(label) > 200:
                raise ValueError(f"workflow step {index} label is limited to 200 characters")
            steps.append({"profile": profile, "payload": dict(step_payload), "retries": retries, "label": label})

        raw_checks = payload.get("final_checks", [])
        if not isinstance(raw_checks, list) or not raw_checks or len(raw_checks) > WORKFLOW_MAX_CHECKS:
            raise ValueError(f"final_checks must be a non-empty list with at most {WORKFLOW_MAX_CHECKS} items")
        checks: list[dict[str, Any]] = []
        allowed_check_types = {"file_exists", "file_nonempty", "file_contains", "directory_exists"}
        for index, raw_check in enumerate(raw_checks, start=1):
            if not isinstance(raw_check, dict):
                raise ValueError(f"final check {index} must be an object")
            check_type = _normalize_check_type(raw_check.get("type", ""), raw_check)
            if check_type not in allowed_check_types:
                raise ValueError(f"final check {index} type must be one of: {', '.join(sorted(allowed_check_types))}")
            path_value = raw_check.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                raise ValueError(f"final check {index} requires a non-empty path")
            # Validate the workspace boundary before execution. The file may be
            # created by a preceding step, so it intentionally need not exist yet.
            resolve_workspace_path(path_value, self.settings.allowed_workspaces, must_exist=False)
            check = {
                "type": check_type,
                "path": path_value,
                # Postconditions default to fresh output checks.  This is the
                # important distinction between "the old file is still here"
                # and "this workflow produced the requested result".  A
                # caller can explicitly set fresh=false for an intentional
                # prerequisite check.
                "fresh": _normalize_bool(raw_check.get("fresh", raw_check.get("must_be_fresh", payload.get("fresh_outputs", True))), True),
            }
            if check_type == "file_contains":
                text = raw_check.get("text")
                if not isinstance(text, str) or not text:
                    raise ValueError(f"final check {index} file_contains requires non-empty text")
                if len(text) > 100_000:
                    raise ValueError(f"final check {index} text is limited to 100000 characters")
                check["text"] = text
            checks.append(check)

        try:
            max_attempts = int(payload.get("max_attempts", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow max_attempts must be an integer") from exc
        if not 1 <= max_attempts <= WORKFLOW_MAX_ATTEMPTS:
            raise ValueError(f"workflow max_attempts must be between 1 and {WORKFLOW_MAX_ATTEMPTS}")
        try:
            overall_timeout = int(payload.get("overall_timeout", self.settings.default_task_timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow overall_timeout must be an integer") from exc
        overall_timeout = max(1, min(overall_timeout, 24 * 60 * 60))
        return steps, checks, max_attempts, overall_timeout

    @staticmethod
    def _path_signature(path: Path) -> tuple[Any, ...]:
        """Return a cheap mutation signature without hashing large MD files."""
        try:
            stat = path.stat()
        except OSError:
            return ("missing",)
        if path.is_file():
            return ("file", stat.st_mtime_ns, stat.st_size)
        if path.is_dir():
            count = 0
            total_size = 0
            newest = stat.st_mtime_ns
            try:
                for child in path.rglob("*"):
                    if not child.is_file():
                        continue
                    count += 1
                    child_stat = child.stat()
                    total_size += child_stat.st_size
                    newest = max(newest, child_stat.st_mtime_ns)
                    if count >= 100_000:
                        break
            except OSError:
                pass
            return ("directory", stat.st_mtime_ns, count, total_size, newest)
        return ("other", stat.st_mtime_ns, stat.st_size)

    def _snapshot_workflow_checks(self, checks: list[dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
        return {
            str(path): self._path_signature(path)
            for path in {
                resolve_workspace_path(check["path"], self.settings.allowed_workspaces, must_exist=False)
                for check in checks
            }
        }

    def _evaluate_workflow_checks(
        self,
        checks: list[dict[str, Any]],
        baseline: dict[str, tuple[Any, ...]] | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        for check in checks:
            path = resolve_workspace_path(check["path"], self.settings.allowed_workspaces, must_exist=False)
            check_type = check["type"]
            passed = False
            detail = ""
            try:
                if check_type == "file_exists":
                    passed = path.is_file()
                    detail = "file exists" if passed else "file does not exist"
                elif check_type == "file_nonempty":
                    passed = path.is_file() and path.stat().st_size > 0
                    detail = "file is non-empty" if passed else "file is missing or empty"
                elif check_type == "directory_exists":
                    passed = path.is_dir()
                    detail = "directory exists" if passed else "directory does not exist"
                elif check_type == "file_contains":
                    if not path.is_file():
                        detail = "file does not exist"
                    else:
                        content = path.read_text(encoding="utf-8", errors="replace")
                        passed = check["text"] in content
                        detail = "text found" if passed else "text not found"
            except OSError as exc:
                detail = f"check failed to read path: {exc}"
            if passed and check.get("fresh", True):
                current_signature = self._path_signature(path)
                before_signature = (baseline or {}).get(str(path), ("missing",))
                if current_signature == before_signature:
                    passed = False
                    detail = f"{detail}; unchanged from before this workflow"
            results.append({"type": check_type, "path": str(path), "passed": passed, "detail": detail, "fresh": bool(check.get("fresh", True))})
        return all(item["passed"] for item in results), results

    async def run_workflow(
        self,
        payload: dict[str, Any],
        task_id: str,
        is_cancelled: Callable[[], Awaitable[bool]],
        on_output: Callable[[str, str], Awaitable[None]],
    ) -> RunnerResult:
        """Run a bounded, persistent multi-step workflow inside one Worker task.

        The Worker owns the loop, so the workflow continues even if the MCP
        request or ChatGPT response is closed. Each step uses the existing
        runner profiles and the workflow is successful only after final checks
        pass.
        """
        started = asyncio.get_running_loop().time()
        try:
            steps, checks, max_attempts, overall_timeout = self._normalize_workflow(payload)
        except Exception as exc:
            return RunnerResult("failed", None, "", f"Invalid workflow: {exc}", 0.0, {"validation_error": str(exc)})

        deadline = started + overall_timeout
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        step_results: list[dict[str, Any]] = []
        final_check_results: list[dict[str, Any]] = []
        last_return_code: int | None = None
        last_failure = ""

        for workflow_attempt in range(1, max_attempts + 1):
            attempt_failed = False
            check_baseline = self._snapshot_workflow_checks(checks)
            await on_output("INFO", f"Workflow attempt {workflow_attempt}/{max_attempts} started")
            for step_index, step in enumerate(steps, start=1):
                step_succeeded = False
                step_attempts = step["retries"] + 1
                for step_attempt in range(1, step_attempts + 1):
                    if await is_cancelled():
                        duration = round(asyncio.get_running_loop().time() - started, 3)
                        return RunnerResult(
                            "cancelled",
                            last_return_code,
                            "\n".join(stdout_parts),
                            "\n".join(stderr_parts),
                            duration,
                            {"workflow_attempts": workflow_attempt, "steps": step_results, "final_checks": final_check_results},
                        )
                    remaining = int(deadline - asyncio.get_running_loop().time())
                    if remaining <= 0:
                        last_failure = f"Workflow exceeded overall timeout of {overall_timeout} seconds"
                        attempt_failed = True
                        break

                    label = step["label"]
                    await on_output("INFO", f"[{label}] running profile {step['profile']} (attempt {step_attempt}/{step_attempts})")
                    step_payload = dict(step["payload"])
                    try:
                        requested_timeout = int(step_payload.get("timeout", remaining))
                    except (TypeError, ValueError):
                        requested_timeout = remaining
                    step_payload["timeout"] = max(1, min(requested_timeout, remaining))
                    step_task_id = f"{task_id}-step-{step_index}-attempt-{step_attempt}"
                    try:
                        result = await self.run(step["profile"], step_payload, step_task_id, is_cancelled, on_output)
                    except Exception as exc:
                        result = RunnerResult("failed", None, "", f"{type(exc).__name__}: {exc}", 0.0)

                    if result.stdout:
                        stdout_parts.append(f"[{label}]\n{result.stdout}")
                    if result.stderr:
                        stderr_parts.append(f"[{label}]\n{result.stderr}")
                    last_return_code = result.return_code
                    step_results.append(
                        {
                            "workflow_attempt": workflow_attempt,
                            "step": step_index,
                            "label": label,
                            "profile": step["profile"],
                            "attempt": step_attempt,
                            "status": result.status,
                            "return_code": result.return_code,
                            "duration_seconds": result.duration_seconds,
                        }
                    )
                    if result.status == "success":
                        step_succeeded = True
                        await on_output("INFO", f"[{label}] completed successfully")
                        break
                    if result.status == "cancelled":
                        duration = round(asyncio.get_running_loop().time() - started, 3)
                        return RunnerResult(
                            "cancelled",
                            result.return_code,
                            "\n".join(stdout_parts),
                            "\n".join(stderr_parts),
                            duration,
                            {"workflow_attempts": workflow_attempt, "steps": step_results, "final_checks": final_check_results},
                        )
                    last_failure = result.stderr.strip() or f"Step {label} failed"
                    if step_attempt < step_attempts:
                        await on_output("WARN", f"[{label}] failed; retrying")

                if not step_succeeded:
                    attempt_failed = True
                    break

            if attempt_failed:
                if workflow_attempt < max_attempts:
                    await on_output("WARN", f"Workflow attempt {workflow_attempt} failed; restarting the bounded workflow")
                    continue
                break

            checks_passed, final_check_results = self._evaluate_workflow_checks(checks, check_baseline)
            if checks_passed:
                duration = round(asyncio.get_running_loop().time() - started, 3)
                await on_output("INFO", "Workflow completed and all final checks passed")
                return RunnerResult(
                    "success",
                    0,
                    "\n".join(stdout_parts),
                    "\n".join(stderr_parts),
                    duration,
                    {"workflow_attempts": workflow_attempt, "steps": step_results, "final_checks": final_check_results},
                )

            failed_checks = ", ".join(item["path"] for item in final_check_results if not item["passed"])
            last_failure = f"Final checks failed: {failed_checks or 'unknown check'}"
            if workflow_attempt < max_attempts:
                await on_output("WARN", f"{last_failure}; restarting the bounded workflow")
                continue
            break

        duration = round(asyncio.get_running_loop().time() - started, 3)
        return RunnerResult(
            "failed",
            last_return_code,
            "\n".join(stdout_parts),
            "\n".join(stderr_parts + ([last_failure] if last_failure else [])),
            duration,
            {"workflow_attempts": max_attempts, "steps": step_results, "final_checks": final_check_results},
        )

    async def _run_wsl_as_active_user(
        self,
        command: RunnerCommand,
        started: float,
        is_cancelled: Callable[[], Awaitable[bool]],
        on_output: Callable[[str, str], Awaitable[None]],
    ) -> RunnerResult:
        cancel_event = threading.Event()
        operation = asyncio.create_task(
            asyncio.to_thread(
                run_command_as_active_user,
                command.command,
                command.cwd,
                command.timeout,
                cancel_event,
            )
        )
        cancelled = False
        timed_out = False
        while not operation.done():
            await asyncio.sleep(0.25)
            if await is_cancelled():
                cancelled = True
                cancel_event.set()
                break
            if asyncio.get_running_loop().time() - started > command.timeout:
                timed_out = True
                cancel_event.set()
                break
        result = await operation
        stdout = result.stdout
        stderr = result.stderr
        if stdout:
            await on_output("INFO", f"stdout captured in output file ({len(stdout.encode('utf-8'))} bytes)")
        if stderr:
            await on_output("WARN", f"stderr captured in output file ({len(stderr.encode('utf-8'))} bytes)")
        duration = round(asyncio.get_running_loop().time() - started, 3)
        if cancelled or result.cancelled:
            status = "cancelled"
        elif timed_out or result.timed_out:
            status = "failed"
            stderr = (stderr + "\nTask timed out").strip()
        elif result.return_code == 0:
            status = "success"
        else:
            status = "failed"
        return RunnerResult(status, result.return_code, stdout, stderr, duration)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=3)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def save_outputs(self, task_id: str, result: RunnerResult) -> list[str]:
        root = self.settings.task_output_root / task_id
        root.mkdir(parents=True, exist_ok=True)
        stdout_path = root / "stdout.txt"
        stderr_path = root / "stderr.txt"
        metadata_path = root / "result.json"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "status": result.status,
                    "return_code": result.return_code,
                    "duration_seconds": result.duration_seconds,
                    "details": result.details,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return [str(stdout_path), str(stderr_path), str(metadata_path)]