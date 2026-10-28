from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import platform
import re
import shlex
import shutil
import sys
import time
import uuid
import zipfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import types as mcp_types

from web_canvas.service import CanvasService
from web_config.settings import Settings
from web_logs.logger import audit
from web_memory.repository import ConversationRepository
from web_memory.task_repository import TaskRepository
from web_memory.store import Database
from web_mcp.paths import relative_display, resolve_workspace_path
from web_tools.profiles import normalize_wsl_path
from web_tools.runner import normalize_runner_profile
from web_tools.browser_client import BrowserBridgeClient, BrowserBridgeError
from web_tools.discovery import discover_environment as discover_environment_readonly
from web_tools.repair_contract import build_repair_contract
from web_tools.web_search import WebSearchError, build_search_query, search_public_web, sanitize_search_text
from web_tools.windows_user_process import run_command_as_active_user, should_use_active_user_context


# Keep a runner request open long enough for a normal GLYCAM/GROMACS setup and
# the bounded EM self-healing loop to reach a real terminal result.  Returning
# at 60 seconds made the web model emit a progress paragraph while the Worker
# was still running, so the visible ChatGPT turn ended before NVT/NPT.  The
# Worker still has its independent 24-hour cap; this is only the MCP-side
# hand-off window.
MCP_REQUEST_WAIT_SECONDS = 15 * 60

# A failed runner task is not automatically an answer boundary.  In practice
# the web agent often needs one or two diagnostic/repair cycles for a real
# path, argument, input-identity, WSL, or browser-bridge problem.  Keep this
# bounded so a genuinely broken task cannot spin forever, while still making
# the recovery contract explicit to the model.
RECOVERY_ATTEMPT_LIMIT = 3
RECOVERY_STATE_TTL_SECONDS = 60 * 60

# ``run_autonomous_workflow`` is deliberately bounded.  A single MCP call may
# contain many browser/file/runner operations, but it must still have a hard
# stop for a broken bridge, an unfixable dependency, or a model that keeps
# proposing the same action.  The user sees one final result rather than a
# sequence of "continue" checkpoints.
AUTONOMOUS_MAX_CYCLES = 48
AUTONOMOUS_TRANSCRIPT_CHARS = 60_000
# MCP sampling is a client callback.  Some web MCP clients advertise a
# session but do not implement ``sampling/createMessage``; awaiting that
# callback without a bound leaves the whole one-shot tool visibly spinning
# forever.  Keep the callback shorter than the one-shot request window so the
# supervisor can switch to its deterministic fallback or return a real,
# bounded diagnostic.
AUTONOMOUS_SAMPLING_TIMEOUT_SECONDS = 60
AUTONOMOUS_ACTIONS = frozenset(
    {
        "browser_open_read",
        "browser_start",
        "browser_list_tabs",
        "browser_list_downloads",
        "browser_open_tab",
        "browser_navigate",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_select_option",
        "browser_wait",
        "browser_wait_until",
        "browser_screenshot",
        "browser_close_tab",
        "browser_build_glycam",
        "create_directory",
        "copy_path",
        "move_path",
        "list_directory",
        "get_path_info",
        "search_file",
        "find_paths",
        "inspect_project",
        "discover_environment",
        "read_file",
        "write_file",
        "patch_file",
        "modify_project",
        "self_check",
        "search_web",
        "monitor_task",
        "wait_task",
        "read_task_output",
        "retry_task",
        "python",
        "powershell",
        "cmd",
        "terminal",
        "wsl",
        "project_build",
        "md_simulation",
        "production_md",
        "run_workflow_until_complete",
        "sleep",
    }
)

_COMPAT_ACTION_ALIASES = {
    "open_url": "browser_start",
    "browser_open_url": "browser_start",
    "browser_open_tab": "browser_start",
    "browser_read_page": "browser_read",
    "browser_get_page": "browser_read",
    "discover": "discover_environment",
    "inspect_environment": "discover_environment",
    "file_write": "write_file",
    "write_text_file": "write_file",
    "patch": "patch_file",
    "apply_patch": "patch_file",
    "file_read": "read_file",
    "list_dir": "list_directory",
    "run_python": "python",
    "run_python_script": "python",
    "run_powershell": "powershell",
    "run_cmd": "cmd",
    "run_terminal": "terminal",
    "run_wsl": "wsl",
    "run_python_script": "python",
    "run_powershell_workflow": "powershell",
    "run_cmd_workflow": "cmd",
    "run_terminal_workflow": "terminal",
    "run_wsl_workflow": "wsl",
    "run_project_build": "project_build",
    "run_md_simulation": "md_simulation",
    "run_production_md": "production_md",
    "final_production_md": "production_md",
    "browser_open_tab": "browser_start",
    "browser_read_page": "browser_read",
}


def _compat_action(value: Any) -> str:
    action = str(value or "").strip().lower().replace("-", "_")
    return _COMPAT_ACTION_ALIASES.get(action, action)


def _compat_args(step: dict[str, Any]) -> dict[str, Any]:
    raw = step.get("args", step.get("arguments", {}))
    args = dict(raw) if isinstance(raw, dict) else {}
    for key, value in step.items():
        if key not in {"id", "label", "action", "args", "arguments", "retries", "optional", "profile", "payload"}:
            args.setdefault(key, value)
    return args


def _resolve_compat_value(value: Any, context: dict[str, Any]) -> Any:
    """Resolve the reference form used by the Xiaoruo2 workflow examples."""

    if isinstance(value, str) and value.startswith("$"):
        current: Any = context
        for part in value[1:].split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                raise ValueError(f"未找到工作流引用：{value}")
        return current
    if isinstance(value, list):
        return [_resolve_compat_value(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): _resolve_compat_value(item, context) for key, item in value.items()}
    return value


def _redact_compat_text(value: Any) -> str:
    text = str(value)
    return re.sub(
        r"(?i)(token|secret|password|authorization|cookie|api[_ -]?key)\s*([=:])\s*([^\s,;&]+)",
        r"\1\2[REDACTED]",
        text,
    )


def _redact_compat_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            result[str(key)] = "[REDACTED]" if any(marker in lowered for marker in ("token", "secret", "password", "cookie", "authorization")) else _redact_compat_value(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_compat_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _redact_compat_text(value[:8_000])
    return value


def _extract_json_object(text: str) -> Any | None:
    """Parse the first JSON value from a model response.

    Sampling clients occasionally wrap JSON in Markdown fences or add one
    sentence before it.  The autonomous executor is a protocol boundary, so
    it accepts only a parsed JSON object/list and never executes free-form
    model text as a command.
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass
        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
                return value
            except (TypeError, ValueError):
                continue
    return None


def _sampling_text(result: Any) -> str:
    """Extract text blocks from an MCP ``sampling/createMessage`` result."""

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not isinstance(content, list):
        content = [content] if content is not None else []
    parts: list[str] = []
    for item in content:
        value = getattr(item, "text", None)
        if value is None and isinstance(item, dict):
            value = item.get("text")
        if value:
            parts.append(str(value))
    return "\n".join(parts).strip()


def _sampling_decision(text: str) -> dict[str, Any] | None:
    """Normalize the small JSON protocol used by the internal supervisor."""

    parsed = _extract_json_object(text)
    if isinstance(parsed, list):
        parsed = {"steps": parsed}
    if not isinstance(parsed, dict):
        return None

    if bool(parsed.get("done") or parsed.get("complete") or parsed.get("finished")):
        acceptance = parsed.get("acceptance", parsed.get("acceptance_checks", parsed.get("final_checks")))
        if isinstance(acceptance, dict):
            acceptance = [acceptance]
        return {
            "done": True,
            "answer": str(parsed.get("answer") or parsed.get("final") or "").strip(),
            "acceptance": acceptance,
        }

    raw_steps = parsed.get("steps") or parsed.get("actions")
    normalized_steps: list[dict[str, Any]] = []
    if isinstance(raw_steps, dict):
        raw_steps = list(raw_steps.values())
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if isinstance(item, dict):
                normalized_steps.append(dict(item))

    raw_action = parsed.get("action") or parsed.get("tool_call") or parsed.get("next_action")
    if isinstance(raw_action, str):
        raw_action = {"name": raw_action, "arguments": parsed.get("arguments", parsed.get("args", {}))}
    elif isinstance(raw_action, dict):
        raw_action = dict(raw_action)
    elif parsed.get("tool") or parsed.get("name"):
        raw_action = {
            "name": parsed.get("tool") or parsed.get("name"),
            "arguments": parsed.get("arguments", parsed.get("args", {})),
        }

    if isinstance(raw_action, dict):
        name = raw_action.get("name") or raw_action.get("tool") or raw_action.get("action")
        arguments = raw_action.get("arguments", raw_action.get("args", {}))
        if isinstance(name, dict):
            nested = name
            name = nested.get("name") or nested.get("tool") or nested.get("action")
            arguments = nested.get("arguments", nested.get("args", arguments))
        if name:
            normalized_steps.insert(0, {"action": str(name), "args": dict(arguments) if isinstance(arguments, dict) else {}})

    if normalized_steps:
        return {"done": False, "steps": normalized_steps, "raw": parsed}
    return {"done": False, "invalid": True, "raw": parsed}


def _normalize_acceptance_specs(value: Any) -> list[Any]:
    """Normalize the model/workflow acceptance field without inventing checks."""

    if value is None:
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return list(value)
    return [value]


def _acceptance_contract_error(value: Any) -> str | None:
    """Return a protocol error for an absent or unusable completion contract."""

    specs = _normalize_acceptance_specs(value)
    if not specs:
        return "completion requires at least one concrete acceptance check"
    for index, item in enumerate(specs, start=1):
        if not isinstance(item, dict):
            return f"acceptance check {index} must be an object"
        check_type = str(item.get("type", item.get("kind", item.get("check", "")))).strip()
        if not check_type:
            return f"acceptance check {index} is missing type/kind/check"
    return None


def _autonomous_action_fingerprint(action: str, args: dict[str, Any]) -> str:
    """Create a stable identity for loop detection without exposing raw secrets."""

    ignored_keys = {"timeout", "timeout_ms", "poll_interval_seconds", "milliseconds", "interval_ms"}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): scrub(item) for key, item in sorted(value.items()) if str(key) not in ignored_keys and not str(key).startswith("__")}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, tuple):
            return [scrub(item) for item in value]
        return value

    encoded = json.dumps({"action": str(action), "args": scrub(args)}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()[:24]


def _autonomous_result_needs_supervision(result: Any) -> bool:
    """Return whether an action result still needs diagnosis or verification.

    Filesystem helpers intentionally return small success objects without a
    ``terminal`` field.  Runner and browser helpers, on the other hand, use
    explicit nonterminal markers.  Keeping this distinction in one function
    prevents the supervisor from either stopping on a queued/failed action or
    treating a simple successful write as an unfinished task.
    """

    if not isinstance(result, dict):
        return False
    if any(
        bool(result.get(key))
        for key in (
            "repair_required",
            "recoverable_error",
            "continue_required",
            "detached",
            "follow_up_required",
        )
    ) or result.get("final_answer_allowed") is False:
        return True
    if result.get("verification") in {
        "click_sent_no_immediate_state_change",
        "page_read_pending",
    }:
        return True
    status = str(result.get("status") or "").lower()
    if status in {"queued", "running", "processing", "detached", "failed", "recoverable_error", "cancelled"}:
        return True
    if result.get("terminal") is False and result.get("completed") is False:
        return True
    if result.get("terminal") is True and result.get("completed") is False and (
        result.get("error") or result.get("message") or result.get("workflow_status") in {"terminal_failure", "failure"}
    ):
        return True
    return False


def _task_id_from_result(result: Any) -> str | None:
    """Return the task id carried by a runner result, if any."""

    if not isinstance(result, dict):
        return None
    task_id = result.get("task_id")
    return str(task_id).strip() if task_id else None


def _looks_like_md_workflow(
    steps: list[dict[str, Any]],
    final_checks: list[dict[str, Any]] | None,
) -> tuple[bool, list[str]]:
    """Detect MD work hidden inside the generic workflow interface.

    The web model can still select ``run_workflow_until_complete`` when it
    expands a GLYCAM/GROMACS request into shell steps. That path cannot own
    the GLYCAM input identity, EM self-healing, or the NVT/NPT hand-off. Keep
    the protection at the server boundary rather than relying on tool
    descriptions alone.
    """
    try:
        haystack = json.dumps(
            {"steps": steps, "final_checks": final_checks or []},
            ensure_ascii=False,
            default=str,
        ).lower()
    except (TypeError, ValueError):
        haystack = str(steps).lower() + " " + str(final_checks or []).lower()

    markers: list[str] = []
    for marker in (
        "glycam",
        "gromacs",
        "gmx",
        "molecular dynamics",
        "分子动力学",
        "glycan",
        "nvt",
        "npt",
        "md_simulation",
        "main.py",
        "auto_file",
        "pentasaccharide",
        "五糖",
        "四糖",
        "三糖",
    ):
        if marker in haystack:
            markers.append(marker)

    # Avoid treating an arbitrary occurrence of the word ``em`` as an MD
    # request, but recognize the common standalone stage token when it is
    # present in a workflow description or command.
    if re.search(r"(?<![a-z])em(?![a-z])", haystack):
        markers.append("em")
    return bool(markers), markers


def _production_command_details(workflow: Any, commands: Any) -> dict[str, Any]:
    """Recognize a legacy raw-shell Production-MD invocation.

    Older ChatGPT MCP sessions cached only ``run_wsl_workflow`` and therefore
    bypassed the newer ``run_production_md`` schema.  This compatibility
    detector is deliberately narrow: it requires ``gmx mdrun`` plus an
    explicit production/final/timed marker.  Ordinary WSL commands and EM,
    NVT, or NPT stages continue to use their original runner.
    """

    raw_commands = commands if isinstance(commands, list) else [commands]
    command_text = " ".join(str(item) for item in raw_commands if item is not None)
    haystack = f"{workflow or ''} {command_text}".casefold()
    if "mdrun" not in haystack:
        return {"detected": False, "text": command_text}

    markers: list[str] = []
    for marker in ("production", "final", "5min", "5 min", "wall", "maxh", "md_5min"):
        if marker in haystack:
            markers.append(marker)
    if not markers:
        return {"detected": False, "text": command_text}

    duration_seconds = 300.0
    timeout_match = re.search(r"\btimeout\s+(\d+(?:\.\d+)?)\s*s\b", haystack)
    maxh_match = re.search(r"(?:^|\s)-maxh\s+(\d+(?:\.\d+)?)", haystack)
    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*min(?:ute)?s?\b", haystack)
    if timeout_match:
        duration_seconds = float(timeout_match.group(1))
    elif maxh_match:
        duration_seconds = float(maxh_match.group(1)) * 3600.0
    elif minute_match:
        duration_seconds = float(minute_match.group(1)) * 60.0

    prefix_match = re.search(r"(?:^|\s)(?:-deffnm|--deffnm)\s+([^\s;&|]+)", command_text, re.IGNORECASE)
    prefix = prefix_match.group(1).strip("'\"") if prefix_match else "md"
    work_match = re.search(r"[\\/]work_([A-Za-z0-9_.-]+)(?:[\\/]|$)", command_text, re.IGNORECASE)
    work_id = work_match.group(1) if work_match else "1"
    threads_match = re.search(r"(?:^|\s)-ntomp\s+(\d+)", command_text, re.IGNORECASE)
    threads = int(threads_match.group(1)) if threads_match else 4

    # Keep candidate paths separate from the shell command.  A command may
    # contain ``&&`` and redirections, and none of those should become part
    # of the filesystem path used by the compatibility router.
    path_candidates: list[str] = []
    for match in re.finditer(r"/mnt/([A-Za-z])/(?P<rest>[^;&|\"']+)", command_text, re.IGNORECASE):
        rest = re.split(r"\s+", match.group("rest").strip(), maxsplit=1)[0].rstrip("/")
        if rest:
            drive_path = rest.replace("/", "\\")
            path_candidates.append(f"{match.group(1).upper()}:\\{drive_path}")
    for match in re.finditer(r"[A-Za-z]:[\\/][^;&|\"']+", command_text):
        path_candidates.append(match.group(0).rstrip("/\\"))

    return {
        "detected": True,
        "markers": markers,
        "text": command_text,
        "duration_seconds": max(1.0, min(duration_seconds, 24 * 60 * 60)),
        "prefix": prefix,
        "work_id": work_id,
        "threads": max(1, min(threads, 256)),
        "path_candidates": list(dict.fromkeys(path_candidates)),
    }


def _production_intent_details(workflow: Any, commands: Any) -> dict[str, Any]:
    """Detect a production-MD request even when the client sends diagnostics.

    Older web-chat turns commonly start a production request with a harmless
    directory listing such as ``find_latest_production_input`` or
    ``prepare_and_run_5min_md``. That action is not itself a production run,
    but returning ordinary success lets the model treat the diagnostic as the
    requested result and leaves the old conversation's paths in play. Keep
    this detector separate from ``_production_command_details``: the latter
    identifies a raw ``gmx mdrun`` compatibility call, while this one protects
    the larger production intent from diagnostics-only substitutions.
    """

    raw_commands = commands if isinstance(commands, list) else [commands]
    command_text = " ".join(str(item) for item in raw_commands if item is not None)
    haystack = f"{workflow or ''} {command_text}".casefold()
    markers: list[str] = []
    for marker in (
        "production",
        "production_md",
        "final simulation",
        "final_md",
        "5min",
        "5 min",
        "5-minute",
        "md_5min",
        "prepare_and_run",
        "find_latest_production_input",
    ):
        if marker in haystack:
            markers.append(marker)
    return {
        "detected": bool(markers),
        "markers": list(dict.fromkeys(markers)),
        "text": command_text,
    }


def _production_system_from_command(details: dict[str, Any], cwd: Any, allowed_workspaces: list[Path]) -> Path | None:
    """Resolve the GLYCAM system named by a legacy shell command.

    The old web client often sends ``cwd='.'`` and embeds the real WSL path
    after ``cd``.  Looking only at ``cwd`` consequently loses the system
    identity and encourages a runner to pick the newest directory by mtime.
    Prefer the explicit ``.../GLYCAM_system_*/work_1/3_core_file`` path, then
    fall back to an absolute cwd that has the same layout.
    """

    candidates = list(details.get("path_candidates") or [])
    if cwd and str(cwd).strip() not in {"", "."}:
        candidates.append(str(cwd))
    for raw in candidates:
        try:
            candidate = resolve_workspace_path(str(raw), allowed_workspaces, must_exist=False)
        except (OSError, ValueError):
            continue
        for ancestor in (candidate, *candidate.parents):
            if ancestor.name.casefold() != "3_core_file":
                continue
            work_dir = ancestor.parent
            if work_dir.name.casefold() != "work_1":
                continue
            system = work_dir.parent
            if system.name.casefold().startswith("glycam_system_"):
                try:
                    resolved = resolve_workspace_path(str(system), allowed_workspaces, must_exist=True)
                except (OSError, ValueError):
                    continue
                if resolved.is_dir():
                    return resolved
    return None


def _production_log_guard(
    profile: str,
    payload: dict[str, Any],
    streams: dict[str, str],
    allowed_workspaces: list[Path],
) -> dict[str, Any] | None:
    """Reject a zero-exit raw mdrun that did not meet a timed-MD request."""

    details = _production_command_details(payload.get("workflow"), payload.get("commands"))
    is_legacy_raw = profile in {"wsl_workflow", "terminal_workflow", "cmd_workflow"} and details.get("detected")
    if profile == "production_md":
        stdout = str(streams.get("stdout") or "")
        required_markers = (
            "PRODUCTION_MD_SUCCESS:",
            "PRODUCTION_MD_LOG:",
            "PRODUCTION_MD_TPR:",
            "PRODUCTION_MD_CHECKPOINT:",
            "PRODUCTION_MD_TRAJECTORY:",
            "PRODUCTION_MD_RUNTIME_SECONDS:",
            "PRODUCTION_MD_STEPS:",
            "PRODUCTION_MD_SIMULATION_TIME_PS:",
        )
        missing = [marker for marker in required_markers if marker not in stdout]
        if "PRODUCTION_MD_REQUESTED_WALL_SECONDS:" not in stdout:
            missing.append("PRODUCTION_MD_REQUESTED_WALL_SECONDS:")
        if missing:
            return {
                "error_class": "production_md_output_not_verified",
                "error": "PRODUCTION_MD_OUTPUT_NOT_VERIFIED: missing " + ", ".join(missing),
                "repair_hint": (
                    "The production script exited zero without emitting every required "
                    "fresh-artifact marker. Read its stdout and output directory, "
                    "repair the script or missing trajectory/checkpoint/log, and "
                    "rerun with a new output prefix; do not report success."
                ),
            }
        requested_match = re.search(
            r"PRODUCTION_MD_REQUESTED_WALL_SECONDS:\s*([0-9]+(?:\.[0-9]+)?)",
            stdout,
        )
        runtime_match = re.search(
            r"PRODUCTION_MD_RUNTIME_SECONDS:\s*([0-9]+(?:\.[0-9]+)?)",
            stdout,
        )
        steps_match = re.search(r"PRODUCTION_MD_STEPS:\s*([0-9]+)", stdout)
        simulation_time_match = re.search(
            r"PRODUCTION_MD_SIMULATION_TIME_PS:\s*([0-9]+(?:\.[0-9]+)?)",
            stdout,
        )
        requested_seconds = float(requested_match.group(1)) if requested_match else 0.0
        runtime_seconds = float(runtime_match.group(1)) if runtime_match else 0.0
        steps = int(steps_match.group(1)) if steps_match else 0
        simulation_time_ps = float(simulation_time_match.group(1)) if simulation_time_match else 0.0
        if (
            requested_seconds <= 0
            # The user asked for a five-minute final simulation.  A 95%
            # tolerance silently accepted a 285-second run, which is not an
            # exact five-minute result and made the final answer look valid
            # while the requested wall-clock work was incomplete.
            or runtime_seconds < requested_seconds
            or steps <= 0
            or simulation_time_ps <= 0
        ):
            return {
                "error_class": "production_md_output_not_verified",
                "error": (
                    "PRODUCTION_MD_OUTPUT_NOT_VERIFIED: independent runtime evidence is insufficient "
                    f"(requested={requested_seconds:g}s, runtime={runtime_seconds:g}s, "
                    f"steps={steps}, simulation_time={simulation_time_ps:g}ps)"
                ),
                "repair_hint": (
                    "Read the fresh GROMACS log and rerun the maintained production script. "
                    "A success marker alone is not evidence; require the requested wall time, "
                    "positive integration steps, and a positive trajectory time axis."
                ),
            }
        return None
    if not is_legacy_raw:
        return None

    system = _production_system_from_command(details, payload.get("cwd"), allowed_workspaces)
    if system is None:
        return {
            "error_class": "production_system_not_resolved",
            "error": "PRODUCTION_MD_OUTPUT_NOT_VERIFIED: the raw mdrun command did not identify a GLYCAM system directory",
            "repair_hint": (
                "Recursively discover the real GLYCAM_system_* directory and the "
                "existing AUTO_file/automated_scripts/md.py, then call the maintained "
                "production runner with that exact system_dir. Do not select a prior "
                "run by directory order or mtime alone."
            ),
        }

    prefix = str(details.get("prefix") or "md")
    core = system / "work_1" / "3_core_file"
    output = system / "work_1" / "4_output"
    log_candidates = [core / f"{prefix}.log", output / f"{prefix}.log"]
    log_path = next((path for path in log_candidates if path.is_file()), None)
    if log_path is None:
        return {
            "error_class": "production_md_output_not_verified",
            "error": f"PRODUCTION_MD_OUTPUT_NOT_VERIFIED: {prefix}.log was not found under the identified system",
            "repair_hint": "Read the raw runner output, locate the fresh log recursively, and rerun through the maintained production script if the raw command wrote incomplete artifacts.",
        }
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "error_class": "production_md_output_not_verified",
            "error": f"PRODUCTION_MD_OUTPUT_NOT_VERIFIED: cannot read {log_path}: {exc}",
            "repair_hint": "Inspect the production log and retry through the maintained script after fixing the output path.",
        }

    duration = float(details.get("duration_seconds") or 300.0)
    wall_match = re.search(r"Wall t \(s\):\s*([0-9]+(?:\.[0-9]+)?)", log_text)
    wall_seconds = float(wall_match.group(1)) if wall_match else 0.0
    clean = "Finished mdrun" in log_text or "Time limit was reached" in log_text
    reached_requested_window = "Time limit was reached" in log_text or wall_seconds >= max(1.0, duration * 0.8)
    if not clean or not reached_requested_window:
        reason = "no clean completion marker" if not clean else f"wall time {wall_seconds:g}s is below the requested {duration:g}s window"
        return {
            "error_class": "production_md_output_not_verified",
            "error": f"PRODUCTION_MD_OUTPUT_NOT_VERIFIED: {reason}; log={log_path}",
            "repair_hint": (
                "The raw mdrun returned zero but did not prove the requested timed "
                "Production MD. Read the log, ensure the production MDP has a large "
                "nsteps ceiling and a wall-clock limit, then rerun via the existing "
                "automated_scripts/md.py with fresh output names."
            ),
        }
    return None


def _autonomous_goal_requires_md(
    goal: str,
    workflow: dict[str, Any] | None = None,
    transcript: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """Return whether a goal needs the dedicated, verified MD completion gate.

    The previous supervisor only inspected the action that happened to be
    running.  A model could therefore send ``done`` after a diagnostic action
    even though the original request was still a GLYCAM/GROMACS job.  Keep the
    domain signal in the server-side state so it cannot be lost when a failed
    runner is followed by ``self_check`` or a search action.
    """

    try:
        haystack = json.dumps(
            {"goal": goal, "workflow": workflow or {}, "transcript": transcript or []},
            ensure_ascii=False,
            default=str,
        ).lower()
    except (TypeError, ValueError):
        haystack = f"{goal} {workflow or {}} {transcript or []}".lower()

    markers: list[str] = []
    for marker in (
        "glycam",
        "gromacs",
        "gmx",
        "molecular dynamics",
        "分子动力学",
        "glycan",
        "nvt",
        "npt",
        "md_simulation",
        "main.py",
        "auto_file",
        "四糖",
        "五糖",
        "六糖",
        "三糖",
        "tetrasaccharide",
        "pentasaccharide",
        "hexasaccharide",
    ):
        if marker in haystack:
            markers.append(marker)
    if re.search(r"(?<![a-z])em(?![a-z])", haystack):
        markers.append("em")
    return bool(markers), markers


def _autonomous_goal_requires_production(
    goal: str,
    workflow: dict[str, Any] | None = None,
    transcript: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str], float]:
    """Detect an explicitly requested final/production stage.

    A natural-language goal can contain several sequential deliverables.  The
    supervisor must remember those deliverables instead of assuming that the
    first successful runner is the whole task.  This detector is intentionally
    based on the requested stage and duration, not on a particular directory
    name or a particular model response.
    """

    try:
        haystack = json.dumps(
            {"goal": goal, "workflow": workflow or {}, "transcript": transcript or []},
            ensure_ascii=False,
            default=str,
        ).lower()
    except (TypeError, ValueError):
        haystack = f"{goal} {workflow or {}} {transcript or []}".lower()

    markers: list[str] = []
    for marker in (
        "production_md",
        "run_production_md",
        "production md",
        "production simulation",
        "production run",
        "final simulation",
        "final md",
        "final production",
        "5min",
        "5 min",
        "5-minute",
        "5分钟",
        "生产模拟",
        "生产阶段",
        "最终模拟",
        "最终阶段",
        "最后模拟",
        "正式模拟",
    ):
        if marker in haystack:
            markers.append(marker)

    # A timed stage is also explicit when the user writes a duration in a
    # molecular-dynamics goal but does not use the English word production.
    md_signal = any(
        token in haystack
        for token in (
            "gromacs",
            "gmx",
            "molecular dynamics",
            "分子动力学",
            "md_simulation",
            "mdrun",
            "nvt",
            "npt",
            "模拟",
        )
    )
    duration_seconds = 300.0
    duration_match = re.search(
        r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*-?\s*(分钟|分|min(?:ute)?s?|秒|sec(?:ond)?s?)",
        haystack,
        re.IGNORECASE,
    )
    if duration_match and md_signal:
        amount = float(duration_match.group(1))
        unit = duration_match.group(2).casefold()
        duration_seconds = amount if ("秒" in unit or unit.startswith("sec")) else amount * 60.0
        markers.append(f"timed_stage_{int(duration_seconds)}s")
    else:
        duration_seconds = 300.0

    # A workflow can declare the maintained action without mentioning it in
    # the user's prose.  The action itself is an unambiguous requirement.
    if re.search(r"(?:^|[\"'\s])(?:production_md|run_production_md)(?:$|[\"'\s])", haystack):
        markers.append("declared_production_action")

    unique = list(dict.fromkeys(markers))
    return bool(unique), unique, max(1.0, min(duration_seconds, 24 * 60 * 60))


def _extract_md_system_dir(value: Any, _depth: int = 0) -> str | None:
    """Extract the exact fresh GLYCAM run directory from runner evidence."""

    if _depth > 5:
        return None
    if isinstance(value, dict):
        for key in ("system_dir", "system_root", "run_dir", "glycam_system_dir"):
            candidate = value.get(key)
            if isinstance(candidate, str) and "glycam_system_" in candidate.casefold():
                return _normalize_extracted_system_path(candidate)
        for key in ("stdout", "result", "output", "details", "transcript"):
            if key in value:
                found = _extract_md_system_dir(value.get(key), _depth + 1)
                if found:
                    return found
        return None
    if isinstance(value, list):
        for item in reversed(value):
            found = _extract_md_system_dir(item, _depth + 1)
            if found:
                return found
        return None
    if not isinstance(value, str):
        return None
    match = re.search(r"(?im)^\s*PIPELINE_SUCCESS:\s*(.+?)\s*$", value)
    if not match:
        return None
    candidate = match.group(1).strip().strip("'\"")
    if "glycam_system_" not in candidate.casefold():
        return None
    return _normalize_extracted_system_path(candidate)


def _normalize_extracted_system_path(value: str) -> str:
    """Convert a WSL ``/mnt/x/...`` runner path to the host path form."""

    normalized = str(value or "").strip().rstrip("/\\")
    match = re.match(r"^/mnt/([a-z])/(.+)$", normalized, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}:\\{match.group(2).replace('/', chr(92))}"
    return normalized


def _md_setup_production_checkpoint(
    base: dict[str, Any],
    task_id: str,
    current: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Turn a successful setup pipeline into an explicit production checkpoint.

    ``AUTO_file/main.py`` owns the GLYCAM/system setup plus EM/NVT/NPT.  A
    successful exit from that entry point is deliberately *not* the final
    result for the compatibility ``run_md_simulation`` surface: older web
    clients selected that surface for a request that also asked for a timed
    production run and then treated ``PIPELINE_SUCCESS`` as the whole task.
    Keep the underlying Worker success as evidence, but make the overall
    operation nonterminal until the separate production runner has emitted its
    independent runtime/artifact markers.
    """

    system_dir = str(base.get("system_dir") or "").strip()
    target_root = str(base.get("target_root") or "").strip()
    try:
        duration_seconds = float(payload.get("production_duration_seconds", 300) or 300)
    except (TypeError, ValueError):
        duration_seconds = 300.0
    duration_seconds = max(1.0, min(duration_seconds, 24 * 60 * 60))
    checkpoint = {
        **base,
        # ``status=success`` belongs to the setup Worker task.  ``completed``
        # below belongs to the user's whole request and must remain false.
        "status": "success",
        "completed": False,
        "terminal": False,
        "user_visible_final": False,
        "do_not_reply": True,
        "final_answer_allowed": False,
        "terminal_status": "recoverable_failure",
        "workflow_status": "repair_required",
        "finalize_now": False,
        "continue_required": True,
        "next_action": "run_production_md",
        "repair_required": True,
        "recoverable_error": True,
        "self_check_required": False,
        "production_required": True,
        "production_duration_seconds": duration_seconds,
        "setup_completed": True,
        "error_class": "md_setup_complete_production_required",
        "error": (
            "The GLYCAM setup, EM, NVT, and NPT stages completed, but the "
            "requested final production MD has not run. This is a checkpoint, "
            "not a user-facing completion."
        ),
        "repair_hint": (
            "Use the exact current system_dir below and call run_production_md "
            f"with duration_seconds={duration_seconds:g}. The production runner "
            "must create a fresh log, checkpoint, trajectory, energy output, "
            "positive step/time metrics, and a wall-clock marker at least equal "
            "to the requested duration. Do not answer from PIPELINE_SUCCESS, "
            "the setup task's elapsed time, or any older sibling directory."
        ),
        "diagnostic_evidence": {
            "setup_task_id": task_id,
            "setup_status": current.get("status"),
            "setup_finished_at": current.get("finished_at"),
            "system_dir": system_dir or None,
            "target_root": target_root or None,
            "production_duration_seconds": duration_seconds,
            "required_next_stage": "production_md",
            "required_markers": [
                "PRODUCTION_MD_SUCCESS",
                "PRODUCTION_MD_LOG",
                "PRODUCTION_MD_TPR",
                "PRODUCTION_MD_CHECKPOINT",
                "PRODUCTION_MD_TRAJECTORY",
                "PRODUCTION_MD_RUNTIME_SECONDS",
                "PRODUCTION_MD_STEPS",
                "PRODUCTION_MD_SIMULATION_TIME_PS",
                "PRODUCTION_MD_REQUESTED_WALL_SECONDS",
            ],
        },
    }
    return checkpoint


def _autonomous_terminal_success(result: Any) -> bool:
    """Recognize a real terminal success result, never a queued checkpoint."""

    if not isinstance(result, dict):
        return False
    if result.get("terminal") is not True or result.get("completed") is not True:
        return False
    if result.get("repair_required") or result.get("recoverable_error"):
        return False
    workflow_status = str(result.get("workflow_status") or "").lower()
    terminal_status = str(result.get("terminal_status") or "").lower()
    return bool(
        result.get("success") is True
        or workflow_status in {"terminal_success", "success"}
        or terminal_status == "success"
    )


def _requested_sugar_count(text: str) -> int | None:
    """Extract an explicit glycan residue count from a user goal."""

    normalized = str(text or "").lower()
    word_counts = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    # Accept the common keyboard typo "塘" as well as the canonical "糖";
    # the surrounding GLYCAM/MD markers still gate this interpretation.
    match = re.search(r"([0-9]{1,2})\s*(?:个)?\s*(?:糖|塘)", normalized)
    if match:
        return int(match.group(1))
    match = re.search(r"([一二三四五六七八九十])\s*(?:个)?\s*(?:糖|塘)", normalized)
    if match:
        return word_counts[match.group(1)]
    for name, count in (
        ("monosaccharide", 1),
        ("disaccharide", 2),
        ("trisaccharide", 3),
        ("tetrasaccharide", 4),
        ("pentasaccharide", 5),
        ("hexasaccharide", 6),
        ("heptasaccharide", 7),
        ("octasaccharide", 8),
        ("nonasaccharide", 9),
        ("decasaccharide", 10),
    ):
        if name in normalized:
            return count
    return None


def _extract_latest_chat_goal(page_text: str) -> str | None:
    """Extract the newest user request from a visible ChatGPT conversation.

    A web model can still select a low-level browser tool even though the
    request is clearly a multi-step task.  The browser page is the only
    durable context available to that low-level call, so the server uses the
    newest user turn as a hand-off signal.  This parser is intentionally
    presentation-tolerant and never treats assistant prose as the goal.
    """

    text = str(page_text or "")
    if not text:
        return None
    user_markers = ("你说：", "你：", "User:", "User said:", "用户：")
    assistant_markers = ("ChatGPT 说：", "ChatGPT:", "Assistant:", "助手：")
    candidates: list[tuple[int, str]] = []
    for marker in user_markers:
        start = 0
        while True:
            position = text.find(marker, start)
            if position < 0:
                break
            candidates.append((position, marker))
            start = position + len(marker)
    for position, marker in sorted(candidates, reverse=True):
        body = text[position + len(marker):]
        ends = [body.find(item) for item in assistant_markers if body.find(item) >= 0]
        if ends:
            body = body[:min(ends)]
        body = re.sub(r"(?:展开收起|收起展开)\s*$", "", body).strip()
        if len(body) >= 12:
            return body
    return None


def _chat_goal_signature(page_text: str, goal: str) -> str:
    """Identify this user turn without including streaming assistant output.

    The same text can legitimately be submitted twice in one conversation.
    Hashing the conversation prefix through the newest user turn keeps those
    executions distinct while remaining stable as the assistant streams its
    answer and tool cards after that turn.
    """

    text = str(page_text or "")
    value = str(goal or "").strip()
    position = text.rfind(value) if value else -1
    prefix = text[: position + len(value)] if position >= 0 else value
    return hashlib.sha256(prefix.encode("utf-8", errors="replace")).hexdigest()[:24]


def _looks_like_autonomous_chat_goal(goal: str) -> bool:
    """Recognize a likely multi-step request without hard-coding one domain."""

    value = str(goal or "").strip()
    if len(value) < 30:
        return False
    lowered = value.lower()
    action_markers = (
        r"打开", r"构建", r"创建", r"调用", r"运行", r"执行", r"跑", r"写入",
        r"读取", r"搜索", r"查找", r"修改", r"修复", r"下载", r"生成", r"验证",
        r"整理", r"导出", r"部署", r"安装", r"open\b", r"build\b", r"create\b",
        r"run\b", r"execute\b", r"search\b", r"fix\b", r"download\b", r"verify\b",
    )
    hits = sum(1 for marker in action_markers if re.search(marker, lowered if marker.isascii() else value, re.IGNORECASE))
    # Xiaoruo1-tagged requests are the strongest signal.  For untagged
    # requests require three independent action verbs so an ordinary one-step
    # browser question is not hijacked.
    return ("xiaoruo1" in lowered and hits >= 2) or hits >= 3


def _glycam_sequence_count(sequence: str) -> int:
    """Count GLYCAM condensed monosaccharide tokens in a sequence string."""

    return len(
        re.findall(
            r"(?<![A-Za-z])(?:[DL][A-Za-z0-9]+(?:\[[^\]]+\])?[AaBb][1-9])",
            str(sequence or ""),
        )
    )


def _metadata_sequence(value: Any) -> str | None:
    """Find a non-empty GLYCAM sequence in info/response JSON."""

    if isinstance(value, dict):
        for key in ("indexOrderedSequence", "incomingSequence", "sequence", "payload"):
            candidate = value.get(key)
            if isinstance(candidate, str) and _glycam_sequence_count(candidate):
                return candidate
        for child in value.values():
            found = _metadata_sequence(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _metadata_sequence(child)
            if found:
                return found
    return None


def _browser_result_has_verified_glycam(result: Any) -> bool:
    """Find the explicit terminal marker in a browser result tree."""

    if isinstance(result, dict):
        evidence = result.get("completion_evidence") or []
        if (
            result.get("workflow_status") == "terminal_success"
            and result.get("completed") is True
            and any("GLYCAM" in str(item) or "PDB" in str(item) for item in evidence)
        ):
            return True
        return any(_browser_result_has_verified_glycam(child) for child in result.values())
    if isinstance(result, list):
        return any(_browser_result_has_verified_glycam(child) for child in result)
    return False


def _browser_download_records(value: Any) -> list[dict[str, Any]]:
    """Collect authoritative browser download records from nested results."""

    records: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("file_path") or item.get("filename"):
                if item.get("state") or item.get("end_time") or item.get("start_time"):
                    records.append(dict(item))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return records


def _download_end_epoch(record: dict[str, Any]) -> float | None:
    for key in ("end_time", "start_time"):
        value = record.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
    return None


# These actions inspect evidence.  They cannot by themselves repair a failed
# operation, so they must not clear the server-side recovery latch.
_AUTONOMOUS_DIAGNOSTIC_ACTIONS = frozenset(
    {
        "browser_open_read",
        "self_check",
        "search_web",
        "find_paths",
        "search_file",
        "list_directory",
        "get_path_info",
        "inspect_project",
        "discover_environment",
        "read_file",
        "browser_list_tabs",
        "browser_list_downloads",
        "browser_read",
        "browser_navigate",
        "browser_screenshot",
        "browser_wait",
        "browser_wait_until",
        "monitor_task",
        "wait_task",
        "read_task_output",
    }
)

_AUTONOMOUS_NON_BLOCKING_ACTIONS = frozenset(
    {
        "browser_wait",
        "browser_wait_until",
        "monitor_task",
        "wait_task",
        "read_task_output",
        "sleep",
    }
)

_AUTONOMOUS_ROOT_DIAGNOSIS_ACTIONS = frozenset({"self_check", "search_web"})


def _tail(text: str, limit: int = 4_000) -> str:
    text = str(text or "")
    return text[-limit:] if len(text) > limit else text


def _terminal_marker_text(result: Any) -> str:
    """Return bounded stdout-like evidence from a terminal runner result."""

    if not isinstance(result, dict):
        return ""
    candidates: list[Any] = [result.get("stdout")]
    nested = result.get("result")
    if isinstance(nested, dict):
        candidates.append(nested.get("stdout"))
    streams = result.get("streams")
    if isinstance(streams, dict):
        candidates.append(streams.get("stdout"))
    return next((str(value) for value in candidates if str(value or "").strip()), "")


def _terminal_marker(stdout: str, name: str) -> str:
    """Extract one machine-readable terminal marker without guessing values."""

    match = re.search(rf"(?m)^\s*{re.escape(name)}:\s*(.+?)\s*$", stdout)
    return match.group(1).strip() if match else ""


def _terminal_user_answer(result: dict[str, Any]) -> str:
    """Build a non-empty user-facing message from already verified evidence."""

    profile = str(result.get("profile") or "workflow").strip()
    status = str(result.get("status") or result.get("terminal_status") or "terminal").strip()
    task_id = str(result.get("task_id") or "").strip()
    stdout = _terminal_marker_text(result)

    if profile == "production_md":
        markers = {
            "system_dir": _terminal_marker(stdout, "PRODUCTION_MD_SUCCESS"),
            "log": _terminal_marker(stdout, "PRODUCTION_MD_LOG"),
            "tpr": _terminal_marker(stdout, "PRODUCTION_MD_TPR"),
            "checkpoint": _terminal_marker(stdout, "PRODUCTION_MD_CHECKPOINT"),
            "trajectory": _terminal_marker(stdout, "PRODUCTION_MD_TRAJECTORY"),
            "runtime_seconds": _terminal_marker(stdout, "PRODUCTION_MD_RUNTIME_SECONDS"),
            "steps": _terminal_marker(stdout, "PRODUCTION_MD_STEPS"),
            "simulation_time_ps": _terminal_marker(stdout, "PRODUCTION_MD_SIMULATION_TIME_PS"),
            "requested_wall_seconds": _terminal_marker(stdout, "PRODUCTION_MD_REQUESTED_WALL_SECONDS"),
        }
        if markers["runtime_seconds"] or markers["requested_wall_seconds"]:
            lines = ["已验证完成：正式 production MD 已达到终态并通过服务器验收。"]
            if task_id:
                lines.append(f"任务 ID：{task_id}")
            if markers["requested_wall_seconds"]:
                lines.append(f"请求墙钟时长：{markers['requested_wall_seconds']} 秒")
            if markers["runtime_seconds"]:
                lines.append(f"实际墙钟运行：{markers['runtime_seconds']} 秒")
            if markers["steps"]:
                lines.append(f"积分步数：{markers['steps']}")
            if markers["simulation_time_ps"]:
                lines.append(f"模拟时间：{markers['simulation_time_ps']} ps")
            for label, key in (
                ("系统目录", "system_dir"),
                ("日志", "log"),
                ("TPR", "tpr"),
                ("检查点", "checkpoint"),
                ("轨迹", "trajectory"),
            ):
                if markers[key]:
                    lines.append(f"{label}：{markers[key]}")
            return "\n".join(lines)

    if result.get("success") is True or str(result.get("workflow_status") or "").lower() == "terminal_success":
        detail = f"任务已达到成功终态：{profile}。"
    else:
        error = str(result.get("error") or "").strip()
        detail = f"任务已达到终态但未成功：{profile}（{status}）。"
        if error:
            detail += f" 错误：{error[:1_000]}"
    if task_id:
        detail += f" 任务 ID：{task_id}。"
    return detail


def _ensure_terminal_user_answer(result: Any) -> Any:
    """Guarantee that every terminal result has a complete answer contract."""

    if not isinstance(result, dict) or result.get("terminal") is not True:
        return result
    answer = str(result.get("answer") or result.get("final_answer") or result.get("final_summary") or "").strip()
    if not answer:
        answer = _terminal_user_answer(result)
    enriched = dict(result)
    enriched["answer"] = answer
    enriched["final_answer"] = answer
    enriched["terminal_user_message"] = answer
    enriched["terminal_instruction"] = (
        "这是已验证的终态结果。下一条助手消息必须完整复述 terminal_user_message/answer；"
        "不得只回复‘已’、‘完成’等确认词，不得要求用户点击继续，也不得重新启动已完成的任务。"
    )
    return enriched


def _compact_terminal_result(result: Any) -> Any:
    """Keep terminal browser hand-offs small enough for the web model to answer."""

    result = _ensure_terminal_user_answer(result)
    if not isinstance(result, dict) or result.get("terminal") is not True:
        return result
    compact = dict(result)
    transcript = compact.pop("transcript", None)
    if isinstance(transcript, list):
        trace: list[dict[str, Any]] = []
        for record in transcript[-16:]:
            if not isinstance(record, dict):
                continue
            item = {key: record.get(key) for key in ("cycle", "kind", "id", "action") if key in record}
            nested = record.get("result")
            if isinstance(nested, dict):
                for key in (
                    "task_id", "profile", "status", "completed", "terminal", "workflow_status",
                    "terminal_status", "system_dir", "target_root", "sequence", "error_class",
                    "next_action", "answer", "final_answer", "completion_summary",
                ):
                    if key in nested:
                        item[key] = nested[key]
            if item:
                trace.append(item)
        compact["trace_summary"] = trace
    for stream in ("stdout", "stderr"):
        value = compact.get(stream)
        if isinstance(value, str) and len(value) > 8_000:
            compact[stream] = _tail(value, 8_000)
    compact["terminal_result_compact"] = True
    return compact


def _enrich_terminal_result(result: Any) -> Any:
    """Make a verified terminal result useful in a web-chat tool card.

    A supervisor may have all the evidence in its transcript while its short
    ``answer`` is only "completed successfully". Web ChatGPT can then end
    the turn with an equally short sentence, hiding the paths and phase
    results the user actually needs. Keep the generic answer, but append a
    bounded evidence summary derived only from already-verified result data.
    """

    if not isinstance(result, dict) or not result.get("completed") or not result.get("terminal"):
        return result

    result = _ensure_terminal_user_answer(result)
    base = str(result.get("answer") or "").strip()
    if not base:
        return result

    details: list[str] = []
    seen: set[str] = set()

    def add(label: str, value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        line = f"- {label}: {text}"
        if line not in seen:
            seen.add(line)
            details.append(line)

    def inspect_payload(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("glycan_input"):
            add("GLYCAM sequence", payload.get("sequence") or payload.get("glycam_sequence"))
            add("GLYCAM input", payload.get("glycan_input"))
            add("GLYCAM browser download", payload.get("browser_download_path"))
            add("GLYCAM build pUUID", payload.get("build_puuid"))
        task_id = payload.get("task_id")
        if task_id:
            add("runner task", task_id)
            add("runner status", payload.get("status"))
            run_result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            add("runner return code", run_result.get("return_code"))
            add("runner duration (seconds)", run_result.get("duration_seconds"))
            for output in run_result.get("output_files") or payload.get("output_files") or []:
                add("runner output", output)
            stdout = str(run_result.get("stdout") or payload.get("stdout") or "")
            for line in stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith((
                    "PIPELINE_SUCCESS:", "EM_LOG:", "NVT_LOG:", "NPT_LOG:",
                    "PRODUCTION_MD_SUCCESS:", "PRODUCTION_MD_LOG:",
                    "PRODUCTION_MD_TPR:", "PRODUCTION_MD_CHECKPOINT:",
                    "PRODUCTION_MD_TRAJECTORY:", "PRODUCTION_MD_RUNTIME_SECONDS:",
                    "PRODUCTION_MD_STEPS:", "PRODUCTION_MD_SIMULATION_TIME_PS:",
                    "PRODUCTION_MD_REQUESTED_WALL_SECONDS:",
                )):
                    add("verified log", stripped)

    for record in result.get("transcript") or []:
        if not isinstance(record, dict):
            continue
        inspect_payload(record.get("result"))
        inspect_payload(record)

    for check in result.get("acceptance") or []:
        if isinstance(check, dict) and check.get("passed") is True:
            add("acceptance check", check.get("check") or check.get("type") or "passed")

    if not details:
        return result
    enriched = dict(result)
    enriched["answer"] = base + "\n\nVerified result details:\n" + "\n".join(details[:40])
    enriched["completion_summary"] = details[:40]
    enriched["final_answer"] = enriched["answer"]
    enriched["terminal_user_message"] = enriched["answer"]
    return enriched


def _failure_text(current: dict[str, Any], streams: dict[str, str]) -> str:
    """Build bounded diagnostic text without requiring a second tool call."""
    parts: list[str] = []
    for key in ("error",):
        value = current.get(key)
        if value:
            parts.append(str(value))
    result = current.get("result")
    if result:
        try:
            parts.append(json.dumps(result, ensure_ascii=False, default=str))
        except TypeError:
            parts.append(str(result))
    for stream in ("stdout", "stderr"):
        value = streams.get(stream)
        if value:
            parts.append(value)
    return "\n".join(parts)


def _classify_recoverable_failure(
    profile: str,
    payload: dict[str, Any],
    current: dict[str, Any],
    streams: dict[str, str],
) -> dict[str, Any] | None:
    """Classify errors that the web agent can diagnose and repair itself.

    This deliberately returns *guidance*, not a guessed command or a guessed
    user input.  The model must inspect the evidence, correct the call/source
    or reconnect the bridge, and submit a changed attempt.  Hard permission,
    authentication, cancellation, and storage failures remain terminal.
    """
    if current.get("status") != "failed":
        return None
    text = _failure_text(current, streams)
    lower = text.lower()
    result = current.get("result") if isinstance(current.get("result"), dict) else {}
    return_code = result.get("return_code")
    nonzero_return = return_code is not None and str(return_code).strip() not in {"", "0", "none"}
    if not lower.strip() and nonzero_return:
        lower = f"task failed with non-zero return code {return_code}"
    if not lower.strip():
        return {
            "error_class": "unknown_diagnosable_failure",
            "repair_hint": (
                "The runner failed without useful diagnostic text. Do not treat "
                "that as a completed task: run self_check with the task id, read "
                "stdout/stderr and the task record, search the relevant public "
                "documentation if needed, then retry with a changed invocation."
            ),
        }

    terminal_markers = (
        "permission denied",
        "access is denied",
        "access denied",
        "not authorized",
        "unauthorized",
        "forbidden",
        "invalid token",
        "token is not valid",
        "no space left on device",
        "disk full",
        "user cancelled",
        "user canceled",
        "cancel requested",
    )
    if any(marker in lower for marker in terminal_markers):
        return None

    if "production_md_output_not_verified" in lower or "production_md_output_not_verified" in text.casefold():
        return {
            "error_class": "production_md_output_not_verified",
            "repair_hint": (
                "A Production-MD command returned without proving the requested "
                "fresh timed result. Recursively locate the existing "
                "AUTO_file/automated_scripts/md.py, read its complete help and the "
                "latest log, repair the duration/output configuration, and retry "
                "with a fresh output prefix. Do not accept a zero exit code alone."
            ),
        }

    if (
        "the following arguments are required" in lower
        or "missing required argument" in lower
        or ("required:" in lower and "--" in lower)
        or "--glycan-dir" in lower
        or "--target-root" in lower
    ):
        return {
            "error_class": "missing_required_arguments",
            "repair_hint": (
                "Read the complete stderr and the real script help/signature. "
                "Locate the actual entry point with find_paths/search_file, then "
                "supply every required argument (including --glycan-dir and "
                "--target-root) from the current task inputs before retrying. "
                "Do not repeat the identical invocation."
            ),
        }

    if any(
        marker in lower
        for marker in (
            "can't open file",
            "cannot open file",
            "no such file or directory",
            "filenotfounderror",
            "file not found",
            "path does not exist",
            "path not found",
            "unknown powershell workflow",
            "unknown workflow",
            "not recognized as an internal or external command",
            "找不到文件",
            "路径不存在",
            "目录不存在",
        )
    ):
        return {
            "error_class": "path_or_invocation",
            "repair_hint": (
                "Inspect the exact error, search nested workspaces for the real "
                "script/workflow and verify its existence and shell before retrying. "
                "Correct the path or workflow arguments; never substitute a guessed "
                "path and never repeat the failed call unchanged."
            ),
        }

    if (
        "unchanged from before this workflow" in lower
        or "final checks failed" in lower and "directory exists" in lower
    ):
        return {
            "error_class": "stale_or_non_specific_final_check",
            "repair_hint": (
                "The workflow only proved that an old directory or artifact is "
                "still present; it did not prove that this request produced a "
                "fresh result. Do not repeat the diagnostics-only workflow. For "
                "GLYCAM/GROMACS/MD, perform the browser build, pass its actual "
                "downloaded glycan directory to run_md_simulation, and validate "
                "fresh EM, NVT, and NPT artifacts under a new run-specific output "
                "directory. Existing files from another sugar count are not a "
                "successful result."
            ),
        }

    if (
        "energy minimization reached the maximum number of steps" in lower
        or "did not converge to fmax" in lower
        or ("maximum number of steps" in lower and "forces" in lower)
    ):
        return {
            "error_class": "gromacs_em_not_converged",
            "repair_hint": (
                "This is a bounded, diagnosable GROMACS EM failure. Inspect the "
                "run_manifest.json self_healing.em record and the EM log. The "
                "maintained AUTO_file/main.py entry point must increase only EM "
                "nsteps, archive the failed EM artifacts, rerun EM, and continue "
                "to NVT/NPT; do not report completion after the first EM attempt."
            ),
        }

    if (
        "min-gas.pdb" in lower
        or "sequence mismatch" in lower
        or "does not match" in lower
        or "mismatch" in lower
        or "three-sugar" in lower
        or "four-sugar" in lower
        or "five-sugar" in lower
        or "三糖" in lower
        or "四糖" in lower
        or "五糖" in lower
        or ("glycan" in lower and "sequence" in lower)
    ):
        return {
            "error_class": "input_identity_or_cardinality",
            "repair_hint": (
                "Re-read the requested GLYCAM sequence and verify the residue count "
                "against the candidate directory and its generated structure files. "
                "If the count or identity is wrong, use the browser workflow to create "
                "the requested structure and obtain its real downloaded artifact; do "
                "not reuse an old folder merely because files already exist. Then retry."
            ),
        }

    if any(
        marker in lower
        for marker in (
            "mcp_network_error",
            "502 bad gateway",
            "502 upstream",
            "bad gateway",
            "gateway timeout",
            "connection reset",
            "connection refused",
            "connection timed out",
            "timed out",
            "timeout",
            "cloudflare",
            "browser bridge",
            "edge extension",
            "websocket",
            "upstream",
        )
    ):
        return {
            "error_class": "transient_bridge_or_network",
            "repair_hint": (
                "Treat this as a recoverable bridge/network event: inspect service and "
                "browser connectivity, rebind the browser tab if needed, wait briefly, "
                "and retry the same logical operation. Preserve the user's inputs and "
                "do not report failure before the bounded recovery attempts are used."
            ),
        }

    if any(
        marker in lower
        for marker in (
            "command not found",
            "gmx: command not found",
            "module not found",
            "modulenotfounderror",
            "importerror",
            "acpype",
            "traceback",
            "exception:",
            "fatal error:",
            "assertionerror",
            "typeerror:",
            "valueerror:",
            "keyerror:",
            "internal server error",
            "failed to submit",
            "worker exception",
            "invalid workflow:",
        )
    ) or nonzero_return:
        return {
            "error_class": "diagnosable_runtime_failure",
            "repair_hint": (
                "Read stderr, stdout, task logs, and the relevant source/configuration "
                "to identify the actual cause. Apply the smallest safe repair to the "
                "script/configuration or corrected tool arguments, validate the change, "
                "and rerun. Do not stop at the first traceback."
            ),
        }

    return None


# Path discovery is often asked for in natural language (for example,
# ``mds-gromacs-automation`` or ``GROMACS 自动化脚本``), while the local
# checkout may use a project-specific directory name such as ``AUTO_file``.
# Keep the fallback deliberately small and discovery-oriented: exact matches
# always win, and token/alias matches only help the first repository/script
# lookup.  This lets the web agent discover an existing checkout without
# requiring the user to know its local clone path.
_DISCOVERY_ALIASES: dict[str, tuple[str, ...]] = {
    "自动化": ("automation", "automated", "auto", "script", "workflow", "pipeline", "自动化"),
    "脚本": ("script", "scripts", "automated", "automation", "auto", "workflow", "pipeline", "脚本"),
    "分子动力学": ("molecular", "gromacs", "mds", "md", "simulation", "分子动力学"),
    "automation": ("automation", "automated", "auto", "script", "workflow", "pipeline"),
    "automated": ("automation", "automated", "auto", "script", "workflow", "pipeline"),
    "gromacs": ("gromacs", "mds", "md", "molecular", "simulation"),
    "molecular": ("molecular", "gromacs", "mds", "md", "simulation"),
    "simulation": ("simulation", "gromacs", "mds", "md", "molecular"),
    "mds": ("mds", "gromacs", "md", "molecular", "simulation"),
    "script": ("script", "scripts", "automated", "automation", "auto", "workflow", "pipeline"),
    "scripts": ("script", "scripts", "automated", "automation", "auto", "workflow", "pipeline"),
    "github": ("github", "git", "repo", "repository", "project", "source"),
}


def _discovery_tokens(query: str) -> tuple[str, ...]:
    """Return useful search tokens while ignoring punctuation/noise."""
    raw = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(query).lower())
    return tuple(token for token in raw if len(token) >= 3)


def _discovery_match_score(item: Path, query: str, case_sensitive: bool = False) -> tuple[int, str] | None:
    """Score exact and semantic path matches for bounded recursive discovery."""
    display = relative_display(item, [])
    haystack = display if case_sensitive else display.lower()
    name = item.name if case_sensitive else item.name.lower()
    needle = query if case_sensitive else query.lower()
    if needle in name or needle in haystack:
        return (1000, "exact")

    tokens = _discovery_tokens(query)
    if not tokens:
        return None
    components = tuple(part for part in re.split(r"[\\/_.\-\s]+", haystack) if part)
    matched = 0
    score = 0
    for token in tokens:
        candidates = _DISCOVERY_ALIASES.get(token, (token,))
        token_hit = False
        for candidate in candidates:
            if any(candidate == part or (len(candidate) >= 4 and candidate in part) for part in components):
                token_hit = True
                score += 80 if candidate == token else 35
                break
        if token_hit:
            matched += 1
    # Require at least one meaningful token.  Multi-token repository names
    # get a stronger score when more than one token is represented.
    if matched:
        return (100 + score + matched * 20, "semantic")
    return None


def _discovery_branch_score(item: Path, query: str, case_sensitive: bool = False) -> int:
    """Prioritize work/project/script branches before unrelated drive folders."""
    name = item.name if case_sensitive else item.name.lower()
    score = 0
    if _discovery_match_score(item, query, case_sensitive):
        score += 10000
    for token in ("work", "project", "repo", "source", "script", "auto", "md", "molecular", "gromacs", "computational", "biology"):
        if token in name:
            score += 500 - len(token)
    return score


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    permission: str = "admin"
    scope: str = "workspace"


class ToolContext:
    def __init__(self, settings: Settings, db: Database, canvas: CanvasService, logger):
        self.settings = settings
        self.db = db
        self.canvas = canvas
        self.tasks = TaskRepository(db)
        self.logger = logger
        self.conversations = ConversationRepository(db)
        self.browser = BrowserBridgeClient(settings)


class ToolRegistry:
    @property
    def chatgpt_direct(self) -> bool:
        """The host owns planning; this server executes only submitted actions."""
        settings = getattr(getattr(self, "ctx", None), "settings", None)
        return getattr(settings, "orchestration_mode", "legacy_sampling") == "chatgpt_direct"

    def __init__(self, context: ToolContext):
        self.ctx = context
        self.tasks = context.tasks
        # A browser model may choose a low-level action instead of the
        # one-shot workflow tool.  Keep a process-local hand-off cache keyed
        # by the actual ChatGPT conversation and newest user goal so the
        # first such action can be promoted into the same server-owned
        # supervisor without starting duplicate browser/Worker runs.
        self._browser_goal_handoffs: dict[str, dict[str, Any]] = {}
        self._browser_goal_handoff_tasks: dict[str, asyncio.Task[Any]] = {}
        self._browser_goal_handoff_lock = asyncio.Lock()
        # Recovery is intentionally process-local and bounded.  A corrected
        # invocation gets a fresh task, while an unchanged root operation still
        # consumes the same budget instead of looping forever.
        self._recovery_attempts: dict[str, dict[str, Any]] = {}
        self._recovery_task_results: dict[str, dict[str, Any]] = {}
        self.specs: dict[str, ToolSpec] = {
            "list_directory": ToolSpec("list_directory", "List files and directories inside an allowed workspace.", scope="workspace"),
            "get_path_info": ToolSpec("get_path_info", "Return metadata for an allowed file or directory without returning its content.", scope="workspace"),
            "create_directory": ToolSpec("create_directory", "Create a directory inside an allowed workspace.", scope="workspace"),
            "read_file": ToolSpec("read_file", "Read a UTF-8 text file inside an allowed workspace.", scope="workspace"),
            "write_file": ToolSpec("write_file", "Atomically write a UTF-8 text file inside an allowed workspace.", scope="workspace"),
            "patch_file": ToolSpec("patch_file", "Replace an exact text fragment in one allowed UTF-8 file with an expected-match guard.", scope="workspace"),
            "copy_path": ToolSpec("copy_path", "Copy an allowed file or directory to another allowed workspace path.", scope="workspace"),
            "move_path": ToolSpec("move_path", "Move an allowed file or directory to another allowed workspace path.", scope="workspace"),
            "delete_path": ToolSpec("delete_path", "Delete an allowed file or directory only when explicit confirmation is provided.", scope="workspace"),
            "search_file": ToolSpec(
                "search_file",
                "Search recursively across allowed workspace files and paths. Matches both file/directory names and file contents by default; use this before concluding that a nested script or project is absent.",
                scope="workspace",
            ),
            "find_paths": ToolSpec(
                "find_paths",
                "Find files and directories recursively by name or path substring inside allowed workspaces. Use this to locate a project or script when its exact directory is unknown.",
                scope="workspace",
            ),
            "inspect_project": ToolSpec("inspect_project", "Inspect project manifests, tree and git metadata without running commands.", scope="workspace"),
            "modify_project": ToolSpec("modify_project", "Apply explicit file changes inside an allowed workspace.", scope="workspace"),
            "get_system_info": ToolSpec("get_system_info", "Return non-secret local system and runtime information.", scope="system"),
            "list_processes": ToolSpec("list_processes", "List local processes without returning environment variables or command secrets.", scope="system"),
            "list_wsl_directory": ToolSpec("list_wsl_directory", "List entries inside a WSL Linux directory without using the Windows workspace path resolver.", scope="workspace"),
            "discover_environment": ToolSpec("discover_environment", "Recursively discover Xiaoruo1 project instructions and executable scripts with an explicit depth/scan report. Results prioritize real entry points such as AUTO_file/automated_scripts/md.py instead of returning only the first shallow files.", scope="workspace"),
            "self_check": ToolSpec(
                "self_check",
                "Run a read-only local health/error check. When an error is supplied or a failed task is inspected, automatically search public web documentation with a sanitized query, return evidence and a bounded repair plan, and keep final_answer_allowed=false until a changed retry succeeds.",
                scope="system",
            ),
            "search_web": ToolSpec(
                "search_web",
                "Search public web documentation for a bounded, already-sanitized troubleshooting query. Returns result metadata only; never include local paths, tokens, cookies or private data.",
                scope="web",
            ),
            "browser_start": ToolSpec("browser_start", "Compatibility alias that opens a visible local Edge tab and returns its tab metadata.", scope="browser"),
            "browser_list_tabs": ToolSpec("browser_list_tabs", "List tabs in the local user-session browser controlled by Xiaoruo1. For a browser task this must be the first action: call the tool immediately without an assistant preamble, then continue the same turn until terminal verification.", scope="browser"),
            "browser_list_downloads": ToolSpec("browser_list_downloads", "List recent browser downloads with their real local file paths and completion state.", scope="browser"),
            "browser_open_tab": ToolSpec("browser_open_tab", "Open a visible local browser tab at an HTTP(S) URL. This is an intermediate action, not task completion; call it without an assistant preamble, continue the requested operation, and verify its final state in the same turn.", scope="browser"),
            "browser_navigate": ToolSpec("browser_navigate", "Navigate a local browser tab to an HTTP(S) URL. Opening a page is never completion of a larger user task; call it without an assistant preamble, continue with the required reads/actions and final verification in the same turn.", scope="browser"),
            "browser_read_page": ToolSpec(
                "browser_read_page",
                "Read rendered page text and visible interactive-element metadata from the local Edge session. This is an intermediate observation and is never completion of a larger task unless the returned page has workflow_status=terminal_success. For a verified GLYCAM download page, terminal_success is emitted only when the page shows Project completed, Simulation finished normally, and a PDB download control. The result includes visible inputs/buttons/links with id and a usable CSS selector when available; use that selector for browser_type/browser_click instead of guessing generic textarea/input targets. Large GitHub/SPAs use a fast content-root strategy automatically; selector can target a CSS region and mode can be auto, visible, or fast. In a multi-step task, always pass the explicit tab_id returned by browser_list_tabs; omit it only for a one-shot inspection of the active tab. Call this tool directly without a natural-language bridge message; after reading, continue all required actions and final verification in the same turn. Once terminal_success arrives, return a compact final result rather than replaying the tool trace.",
                scope="browser",
            ),
            "browser_read": ToolSpec("browser_read", "Compatibility alias for browser_read_page; read the current local Edge page with bounded visible text and interactive metadata.", scope="browser"),
            "browser_click": ToolSpec("browser_click", "Click one addressed element in the local Edge tab and verify a URL, page-content, target-state, or completed download change. A click is an intermediate action, not completion; call it without an assistant bridge message. If verification is click_sent_no_immediate_state_change, wait/read again in the same turn instead of stopping. For downloads use the returned download.file_path, never a guessed Downloads path.", scope="browser"),
            "browser_type": ToolSpec("browser_type", "Type text into one addressed visible element in the local Edge tab and verify the resulting value. For a native HTML select/combobox, use the exact CSS selector returned by browser_read_page and pass the option's visible label as text; this same tool selects that option and verifies the resulting control value. Typing/selection is an intermediate action, not completion; call it without an assistant bridge message, continue the requested sequence and final verification in the same turn. Prefer by=css with the exact selector returned by browser_read_page.", scope="browser"),
            "browser_press": ToolSpec("browser_press", "Press a real keyboard key in the local Edge page or the addressed visible element and verify the resulting page/target state. Use this for keyboard-only controls such as @-mention autocomplete, ArrowDown, Enter, Escape, and Tab. This is an intermediate action, not completion; continue the same task and verify the terminal result.", scope="browser"),
            "browser_instances": ToolSpec("browser_instances", "Discover authenticated browser instances, generation, capabilities and tabs without starting a new browser or copying login data. Use explicit returned browser_id/tab_id for browser_control.", scope="browser"),
            "browser_control": ToolSpec("browser_control", "Operate one explicit existing browser instance/tab through the unified adapter. Writes require confirm=true, a unique operation_id and arguments.expected_generation from discovery; unknown outcomes are fenced, never blindly replayed. No arbitrary JavaScript or private ChatGPT protocol. Publishing remains separately authorized.", scope="browser"),
            "browser_debug_control": ToolSpec("browser_debug_control", "Inspect or explicitly attach/detach bounded Chromium debugging for one selected tab. Debugger permissions must already be granted; never requests or expands permissions automatically. Normal control remains available when unsupported. No arbitrary JavaScript, cookies or network tracing.", scope="browser"),
            "browser_upload_file": ToolSpec("browser_upload_file", "Select real bytes from an explicitly authorized media file in a previously observed unique file input in the current Edge tab. Requires confirm=true, an absolute date-project path and a unique idempotency_key. Read file_inputs first; selection is not proof of network upload or platform processing. After uncertainty query browser_upload_status and inspect the page; never blindly repeat uploads. Bilibili web only, never the desktop client.", scope="browser"),
            "browser_upload_status": ToolSpec("browser_upload_status", "Read the durable result of a prior file-selection request without sending or repeating it. Inspect the actual webpage separately for upload/processing/draft evidence.", scope="browser"),
            "video_project_path": ToolSpec("video_project_path", "Resolve the fixed external Bilibili video project root/yyyy_MM/dd for an ISO date in Asia/Shanghai; optionally create that directory. Video assets must never be written in the MCP source project.", scope="workspace"),
            "browser_select_option": ToolSpec("browser_select_option", "Select one option in a native HTML select/combobox in the local Edge tab and verify the selected value/index changed. Use browser_read_page first, pass the exact CSS selector for the select, and use option_by=label for visible option text, option_by=value for the option value, or option_by=index for a zero-based option index. This is an intermediate action; call it without an assistant bridge message, continue the page workflow and verify its final result.", scope="browser"),
            "browser_wait": ToolSpec("browser_wait", "Wait for a local browser tab to settle and return a fresh page snapshot. Waiting alone is never completion of a larger task; use the returned snapshot to continue inspecting or acting and do not return any user-facing answer while the requested result is unverified. Call it without an assistant bridge message and always pass the explicit tab_id in a multi-step workflow.", scope="browser"),
            "browser_screenshot": ToolSpec("browser_screenshot", "Save a screenshot of a local browser tab under the private data directory.", scope="browser"),
            "browser_close_tab": ToolSpec("browser_close_tab", "Close one local browser tab.", scope="browser"),
            "git_status": ToolSpec("git_status", "Read Git branch and working-tree status for an allowed project.", scope="workspace"),
            "git_diff": ToolSpec("git_diff", "Read a bounded Git diff for an allowed project.", scope="workspace"),
            "git_log": ToolSpec("git_log", "Read recent Git commits for an allowed project.", scope="workspace"),
            "git_branches": ToolSpec("git_branches", "Read local and remote Git branch names for an allowed project.", scope="workspace"),
            "git_commit": ToolSpec("git_commit", "Create a Git commit after explicit confirmation.", scope="workspace"),
            "run_python_script": ToolSpec("run_python_script", "Run a Python script and wait for its terminal result. A diagnosable failure is nonterminal: inspect the returned stdout/stderr, repair the cause, and submit a changed retry; only an irrecoverable failure or exhausted recovery budget is terminal.", scope="task"),
            "run_wsl_workflow": ToolSpec("run_wsl_workflow", "Run an approved standalone WSL workflow and wait for its terminal result. Use run_md_simulation for GLYCAM/GROMACS setup and run_production_md for a final timed stage. For compatibility, a raw gmx mdrun command carrying production/final/5min markers is automatically routed to the recursively discovered AUTO_file/automated_scripts/md.py and is never allowed to pass on exit code alone. Path, dependency, bridge, stale-artifact, and runtime failures return repair_required=true instead of ending the task.", scope="task"),
            "run_powershell_workflow": ToolSpec("run_powershell_workflow", "Run a PowerShell workflow and wait for its terminal result. Diagnosable path, argument, workflow, or service failures return terminal=false with repair guidance so the web agent must inspect and retry.", scope="task"),
            "run_cmd_workflow": ToolSpec("run_cmd_workflow", "Run a Windows CMD workflow and wait for its terminal result. Diagnosable failures are recovery checkpoints, not final answers; inspect, repair, and retry.", scope="task"),
            "run_terminal_workflow": ToolSpec("run_terminal_workflow", "Run a Windows terminal workflow. Use run_wsl_workflow instead for GROMACS/GLYCAM/WSL; an unknown preset without commands is a repairable invocation error, not a successful no-op.", scope="task"),
            "run_project_build": ToolSpec("run_project_build", "Run a bounded Python or frontend project build and wait for its terminal result. On a diagnosable failure, inspect logs/source, make the smallest safe repair, and retry before answering.", scope="task"),
            "run_md_simulation": ToolSpec("run_md_simulation", "PRIMARY runner for GLYCAM/GROMACS molecular-dynamics requests. After the browser has produced and verified the requested glycan, pass the real glycan directory, the maintained AUTO_file/main.py entry point, and explicit --glycan-dir/--target-root args here. The maintained entry point uses the repository's real EM/NVT/NPT parameters by default; do not inject --nsteps 100, --em-emtol 10000, or --allow-nonconverged-em unless the user explicitly asks for a smoke test. It self-inspects EM logs, archives failed EM artifacts, repairs genuine EM non-convergence, and continues to NVT/NPT before returning. For a normal web-chat MD request, a successful setup is a mandatory hand-off checkpoint, never the final answer: the server automatically queues run_production_md and requires fresh timed production markers before terminal_success. PIPELINE_SUCCESS, setup elapsed time, NVT/NPT completion, or an old production file can never satisfy the final stage. Do not replace this with diagnostics-only run_wsl_workflow or a generic run_workflow_until_complete call. Missing args, wrong paths, stale/mismatched GLYCAM inputs, bridge errors, short-test arguments, and diagnosable runtime failures return repair_required=true and terminal=false so the web agent must diagnose, modify, and retry.", scope="task"),
            "run_production_md": ToolSpec("run_production_md", "Run the user's existing recursively discovered AUTO_file/automated_scripts/md.py after a verified NPT result. This is the final Production MD stage, not a replacement setup pipeline: pass the exact GLYCAM system directory when known and duration_seconds=300 for a five-minute wall-clock run. The profile verifies npt.gro/npt.cpt/npt.tpr/index.ndx/topol.top plus setting_file.json/hardsoft.json, invokes the existing script non-interactively, creates/normalizes the production MDP when the script's md.mdp is absent, disables unavailable GPU flags, uses a task-unique output prefix, and requires fresh log/checkpoint/energy/trajectory outputs plus the requested wall-clock marker. Missing paths, stale artifacts, script failures, or invalid artifacts are repair_required checkpoints; diagnose/search/repair and retry with changed arguments before answering.", scope="task"),
            "create_task": ToolSpec("create_task", "Queue a named runner task and return its task id; use run_* or run_workflow_until_complete when the caller needs a terminal result.", scope="task"),
            "run_workflow_until_complete": ToolSpec(
                "run_workflow_until_complete",
                "Runner for ordinary non-MD multi-step tasks or deliverable files: submit concrete action/profile steps and non-empty final checks in one workflow, wait for the Worker, retry as configured, and return only after fresh postconditions pass or a real terminal failure is recorded. A risky step may include on_error (or repair/recovery_steps) plus max_retries; the server executes that repair branch and retries inside this same call, so a client never needs a second continuation turn. For GLYCAM/GROMACS/MD use run_md_simulation instead; do not submit a diagnostics-only step plus a reused directory as a substitute. Final checks default to fresh output checks, so old files in a reused folder cannot satisfy a new task.",
                scope="task",
            ),
            "run_workflow": ToolSpec("run_workflow", "Compatibility workflow surface with browser_start/browser_read, read-only discovery, file patching, bounded runner steps, acceptance checks and a nested repair contract. The MCP executes the whole workflow in one call and may internally ask the connected ChatGPT model for repair decisions.", scope="task"),
            "run_autonomous_workflow": ToolSpec(
                "run_autonomous_workflow",
                "PRIMARY one-call supervisor for any complex task, regardless of domain. Give it the user's goal and optionally an initial workflow of concrete action/args objects; it keeps the MCP request open while it executes browser/file/search/runner actions, automatically injects read-only diagnosis/search after recoverable failures, applies declared on_error/repair/recovery_steps branches, retries changed operations, blocks repeated identical failing calls, and validates a concrete acceptance contract before returning. A server-side recovery latch survives self_check/search/read-only actions, so the model cannot return done after an unresolved failure or after merely opening/queuing/inspecting something. When MCP sampling is supported it asks the connected ChatGPT model for the next decision inside the same turn; if sampling is unavailable or times out it uses only deterministic branches and returns a bounded capability/timeout failure, never a false success. Domain-specific runners are optional implementations behind this generic supervisor, not the completion policy.",
                scope="task",
            ),
            "monitor_task": ToolSpec("monitor_task", "Read task status, logs, outputs and errors. If a finished task has recoverable_error=true or repair_required=true, it is not an answer boundary: diagnose, modify/correct, and retry. Only terminal=true with no repair_required is an answer boundary.", scope="task"),
            "wait_task": ToolSpec("wait_task", "Compatibility alias for waiting on a queued task until a bounded terminal result or an explicit nonterminal checkpoint.", scope="task"),
            "list_tasks": ToolSpec("list_tasks", "List recent task records and statuses.", scope="task"),
            "read_task_output": ToolSpec("read_task_output", "Read a bounded stdout or stderr output file from a completed task.", scope="task"),
            "retry_task": ToolSpec("retry_task", "Create a new task from a previous task payload.", scope="task"),
            "cancel_task": ToolSpec("cancel_task", "Request cancellation of a queued or running task.", scope="task"),
            "list_conversations": ToolSpec("list_conversations", "List saved local Xiaoruo1 conversations.", scope="memory"),
            "get_conversation": ToolSpec("get_conversation", "Read one saved local Xiaoruo1 conversation and its messages.", scope="memory"),
            "create_canvas": ToolSpec("create_canvas", "Create a revisioned Canvas document.", scope="canvas"),
            "get_canvas": ToolSpec("get_canvas", "Read a Canvas and its current revision.", scope="canvas"),
            "update_canvas": ToolSpec("update_canvas", "Create a new Canvas revision.", scope="canvas"),
            "list_canvas_history": ToolSpec("list_canvas_history", "List Canvas revisions without returning document content.", scope="canvas"),
            "restore_canvas_revision": ToolSpec("restore_canvas_revision", "Restore a Canvas revision as a new revision.", scope="canvas"),
        }
        from web_mcp.direct_tools import DirectTools, TOOL_METADATA

        self.direct_tools = DirectTools(context)
        self.direct_tools.task_status_callback = self._monitor_task
        self._direct_tool_names = frozenset(TOOL_METADATA)
        self.specs.update({name: ToolSpec(name, **meta) for name, meta in TOOL_METADATA.items()})
        if self.chatgpt_direct:
            for name in ("run_python_script", "run_wsl_workflow", "run_powershell_workflow", "run_cmd_workflow",
                         "run_terminal_workflow", "run_project_build", "run_md_simulation", "run_production_md"):
                self.specs[name].description = "Queue the explicitly requested deterministic operation; returns task_id immediately. Read task_status and bounded task output for the real result. ChatGPT chooses any next operation."
            self.specs["run_autonomous_workflow"].description = "Compatibility name for an explicit finite workflow.steps batch. A goal without steps returns needs_chatgpt_planning. No server-side sampling or hidden planning."
            self.specs["run_workflow"].description = self.specs["run_autonomous_workflow"].description
            self.specs["run_workflow_until_complete"].description = "Queue explicit runner steps and final_checks in SQLite; returns task_id promptly for task_status."
            self.specs["monitor_task"].description = "Return the current task snapshot immediately. queued/running is normal; inspect the recorded terminal result when ready."
            self.specs["wait_task"].description = "Wait at most five seconds for an existing deterministic task, then return its real status."
            self.specs["create_task"].description = "Queue one named deterministic runner profile and return task_id immediately."
            for name, spec in self.specs.items():
                if name.startswith("browser_"):
                    spec.description = f"Execute only the addressed {name} operation through the existing local browser bridge and return the observed state. ChatGPT uses the result to choose the next tool."

    def list_specs(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "description": s.description, "permission": s.permission, "scope": s.scope} for s in self.specs.values()]

    @staticmethod
    def _recovery_root_key(profile: str, payload: dict[str, Any], error_class: str) -> str:
        """Identify one logical operation without making the raw error the key."""
        identity = {
            "profile": profile,
            "script_path": payload.get("script_path"),
            "cwd": payload.get("cwd"),
            "workflow": payload.get("workflow"),
            "distribution": payload.get("distribution"),
            "error_class": error_class,
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _take_recovery_attempt(self, profile: str, payload: dict[str, Any], error_class: str) -> int:
        """Consume a bounded recovery attempt for one logical failure class."""
        key = self._recovery_root_key(profile, payload, error_class)
        now = time.monotonic()
        previous = self._recovery_attempts.get(key)
        if not previous or now - float(previous.get("last_at", 0.0)) > RECOVERY_STATE_TTL_SECONDS:
            attempt = 1
        else:
            attempt = int(previous.get("attempts", 0)) + 1
        self._recovery_attempts[key] = {"attempts": attempt, "last_at": now}
        # Bound dictionary growth if a long-running MCP process handles many
        # unrelated tasks.
        if len(self._recovery_attempts) > 512:
            cutoff = now - RECOVERY_STATE_TTL_SECONDS
            self._recovery_attempts = {
                item_key: item
                for item_key, item in self._recovery_attempts.items()
                if float(item.get("last_at", 0.0)) >= cutoff
            }
        return attempt

    async def _capture_task_streams(self, task_id: str) -> dict[str, str]:
        streams: dict[str, str] = {}
        for stream in ("stdout", "stderr"):
            try:
                # Long scientific runners usually print the authoritative
                # completion record at the very end of stdout.  Reading only
                # the first 50k silently dropped PIPELINE_SUCCESS and caused
                # the following production stage to lose the exact current
                # system directory, after which it correctly refused to pick
                # one of several old siblings.  Keep both bounded ends so
                # completion evidence and useful diagnostics survive large
                # logs without loading an unbounded file.
                head = await self._read_task_output(task_id, stream, 30_000)
                tail = await self._read_task_output(task_id, stream, 30_000, tail=True)
                head_text = str(head.get("content", ""))
                tail_text = str(tail.get("content", ""))
                if head.get("truncated"):
                    streams[stream] = (
                        head_text
                        + "\n\n[... task output middle omitted by MCP ...]\n\n"
                        + tail_text
                    )
                else:
                    streams[stream] = head_text
            except (FileNotFoundError, PermissionError, ValueError):
                continue
        return streams

    async def _finished_task_result(
        self,
        profile: str,
        payload: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one answer-boundary policy to every runner entry point."""
        task_id = str(current.get("id") or current.get("task_id") or "")
        cached_recovery = self._recovery_task_results.get(task_id)
        if cached_recovery is not None:
            # A model may defensively call monitor_task after receiving this
            # checkpoint.  Do not consume another budget unit for the same
            # immutable failed task; the next valid action is repair + retry.
            return dict(cached_recovery)
        streams = await self._capture_task_streams(task_id) if task_id else {}
        status = current.get("status")
        if status == "success":
            # A process exit code is not sufficient evidence for a timed
            # scientific task.  Validate the production markers/log window at
            # the answer boundary as well, including for old clients that may
            # still reach a generic shell profile.
            guard = _production_log_guard(
                profile,
                payload,
                streams,
                self.ctx.settings.allowed_workspaces if getattr(self, "ctx", None) else [],
            )
            if guard:
                current = dict(current)
                current["status"] = "failed"
                current["error"] = guard.get("error")
                status = "failed"
            if self.chatgpt_direct:
                recorded_code = (current.get("result") or {}).get("return_code")
                if not guard and (type(recorded_code) is not int or recorded_code != 0):
                    current = {**current, "status": "failed", "error": "Worker success has no verified zero return_code"}
                    status = "failed"
                if status == "failed" and task_id:
                    await self.ctx.db.execute("UPDATE tasks SET status='failed',error=? WHERE id=? AND status='success'", (current["error"], task_id))
                    await self.ctx.db.commit()
        base: dict[str, Any] = {
            "task_id": task_id,
            "profile": profile,
            "status": status,
            "completed": status == "success",
            "error": current.get("error"),
            "result": current.get("result"),
            "output_files": current.get("output_files", []),
            "logs": current.get("logs", []),
        }
        base.update(streams)
        if profile == "md_simulation":
            # main.py prints the authoritative run directory as a WSL path.
            # Preserve it as structured evidence so a mandatory production
            # stage can continue the exact run instead of scanning and picking
            # an older sibling by time or directory order.
            system_dir = _extract_md_system_dir(base)
            if system_dir:
                base["system_dir"] = system_dir
                base.setdefault("target_root", str(Path(system_dir).parent))
        elif profile == "production_md":
            # Preserve the exact current system identity across monitor_task
            # and the terminal answer.  A production result must never force a
            # client to rediscover a sibling directory by mtime.
            system_dir = str(payload.get("system_dir") or payload.get("system_root") or "").strip()
            if system_dir:
                base["system_dir"] = system_dir
                base.setdefault("target_root", str(Path(system_dir).parent))
            if payload.get("duration_seconds") is not None:
                base["requested_wall_seconds"] = payload.get("duration_seconds")

        if self.chatgpt_direct:
            # The submitted process is terminal even when the host may
            # decide to repair it. Task success does not finish a larger goal.
            base.update(terminal=True, retryable=status == "failed")
            if status == "failed":
                classification = _classify_recoverable_failure(profile, payload, current, streams)
                base["error_code"] = (classification or {}).get("error_class", "task_failed")
                if classification:
                    base["repair_hint"] = classification["repair_hint"]
            elif profile == "md_simulation" and bool(payload.get("require_production", True)):
                base["suggested_next_tool"] = "run_production_md"
                base["message"] = "The submitted setup task finished; production remains a separate host decision."
            return base

        if status == "success":
            if profile == "md_simulation" and bool(payload.get("require_production", True)):
                # A successful AUTO_file/main.py setup is only a verified
                # hand-off point for the separate timed production driver.
                # Never let the compatibility runner expose it as a terminal
                # answer: the autonomous supervisor will consume this
                # machine-readable checkpoint and enqueue run_production_md.
                checkpoint = _md_setup_production_checkpoint(base, task_id, current, payload)
                self._recovery_task_results[task_id] = checkpoint
                if len(self._recovery_task_results) > 512:
                    self._recovery_task_results = dict(list(self._recovery_task_results.items())[-256:])
                return checkpoint
            return _ensure_terminal_user_answer({
                **base,
                "terminal": True,
                "user_visible_final": True,
                "do_not_reply": False,
                "terminal_status": "success",
                "workflow_status": "terminal_success",
                "finalize_now": True,
                "final_answer_allowed": True,
                "continue_required": False,
                "next_action": None,
                "repair_required": False,
                "terminal_proof": {
                    "task_id": task_id,
                    "status": "success",
                    "finished_at": current.get("finished_at"),
                },
            })

        classification = _classify_recoverable_failure(profile, payload, current, streams)
        if classification:
            attempt = self._take_recovery_attempt(profile, payload, classification["error_class"])
            if attempt <= RECOVERY_ATTEMPT_LIMIT:
                evidence_text = _failure_text(current, streams)
                fingerprint = hashlib.sha256(
                    f"{classification['error_class']}\n{evidence_text}".encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                recovery_result = {
                    **base,
                    "terminal": False,
                    "completed": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "terminal_status": "recoverable_failure",
                    "workflow_status": "repair_required",
                    "finalize_now": False,
                    "continue_required": True,
                    "next_action": "self_check_search_repair_and_retry",
                    "recoverable_error": True,
                    "repair_required": True,
                    "self_check_required": True,
                    "self_check_request": {
                        "operation": profile,
                        "task_id": task_id,
                        "error_text": _tail(evidence_text, 4_000),
                        "auto_search": True,
                    },
                    "web_search_required": True,
                    "recovery_attempt": attempt,
                    "recovery_attempts_remaining": RECOVERY_ATTEMPT_LIMIT - attempt,
                    "error_class": classification["error_class"],
                    "repair_hint": classification["repair_hint"],
                    "retry_fingerprint": fingerprint,
                    "diagnostic_evidence": {
                        "stderr_tail": _tail(streams.get("stderr", "")),
                        "stdout_tail": _tail(streams.get("stdout", "")),
                        "task_error": str(current.get("error") or ""),
                    },
                    "message": (
                        "任务遇到可恢复错误。不得向用户结束或要求用户继续；先读取证据并诊断，"
                        "修正参数/路径/输入/脚本或恢复连接，然后以不同的调用重试。"
                    ),
                }
                self._recovery_task_results[task_id] = recovery_result
                if len(self._recovery_task_results) > 512:
                    self._recovery_task_results = dict(list(self._recovery_task_results.items())[-256:])
                return recovery_result

            # The same logical operation has now used its bounded repair budget.
            # Return a truthful terminal failure with the evidence, rather than
            # allowing an unbounded model/tool loop.
            base.update(
                {
                    "recovery_budget_exhausted": True,
                    "recovery_attempts": attempt,
                    "error_class": classification["error_class"],
                    "repair_hint": classification["repair_hint"],
                    "diagnostic_evidence": {
                        "stderr_tail": _tail(streams.get("stderr", "")),
                        "stdout_tail": _tail(streams.get("stdout", "")),
                        "task_error": str(current.get("error") or ""),
                    },
                }
            )

        return _ensure_terminal_user_answer({
            **base,
            "terminal": True,
            "user_visible_final": True,
            "do_not_reply": False,
            "terminal_status": "failure" if status != "cancelled" else "cancelled",
            "workflow_status": "terminal_failure" if status != "cancelled" else "terminal_cancelled",
            "finalize_now": True,
            "final_answer_allowed": True,
            "continue_required": False,
            "next_action": None,
            "repair_required": False,
            "terminal_proof": {
                "task_id": task_id,
                "status": status,
                "finished_at": current.get("finished_at"),
            },
        })

    def _browser_failure_result(self, tool_name: str, arguments: dict[str, Any], exc: BrowserBridgeError) -> dict[str, Any]:
        """Turn a browser bridge/page-state exception into a resumable result."""
        message = str(exc)
        lower = message.lower()
        hard = ("token is not available", "token is empty", "unauthorized", "forbidden", "permission")
        attempt = self._take_recovery_attempt(
            f"browser:{tool_name}",
            {**arguments, "error": message},
            "browser_bridge_or_page_state",
        )
        if any(marker in lower for marker in hard) or attempt > RECOVERY_ATTEMPT_LIMIT:
            return {
                "tool": tool_name,
                "status": "failed",
                "error": message,
                "terminal": True,
                "completed": False,
                "user_visible_final": True,
                "do_not_reply": False,
                "terminal_status": "failure",
                "workflow_status": "terminal_failure",
                "finalize_now": True,
                "continue_required": False,
                "next_action": None,
                "repair_required": False,
                "recovery_budget_exhausted": attempt > RECOVERY_ATTEMPT_LIMIT,
                "recovery_attempts": attempt,
            }
        return {
            "tool": tool_name,
            "status": "recoverable_error",
            "error": message,
            "terminal": False,
            "completed": False,
            "user_visible_final": False,
            "do_not_reply": True,
            "terminal_status": "recoverable_failure",
            "workflow_status": "repair_required",
            "finalize_now": False,
            "continue_required": True,
            "recoverable_error": True,
            "repair_required": True,
            "self_check_required": True,
            "self_check_request": {
                "operation": tool_name,
                "error_text": message[:4_000],
                "browser_check": True,
                "auto_search": True,
            },
            "web_search_required": True,
            "recovery_attempt": attempt,
            "recovery_attempts_remaining": RECOVERY_ATTEMPT_LIMIT - attempt,
            "error_class": "browser_bridge_or_page_state",
            "next_action": "self_check_search_rebind_and_retry",
            "repair_hint": (
                "Do not answer yet. Re-list the local browser tabs, rebind the explicit "
                "tab_id, read the current page, and inspect the exact visible selector "
                "or bridge/service state. For a transient 502/timeout, wait briefly "
                "and retry; for a stale page/selector, use the fresh page metadata. "
                "Do not repeat the failed browser call unchanged."
            ),
            "diagnostic_evidence": {"tool": tool_name, "arguments": arguments, "error": message},
            "message": "浏览器桥接/页面状态异常，必须先诊断并恢复后继续，不得向用户结束任务。",
        }

    async def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self.specs:
            raise ValueError(f"Unknown tool: {name}")
        arguments = dict(arguments or {})
        mcp_ctx = arguments.pop("__mcp_ctx", None)
        method = getattr(self, f"_{name}", None)
        extension = name in self._direct_tool_names
        if method is None and not extension:
            raise ValueError(f"Tool has no implementation: {name}")
        audit(self.ctx.logger, "tool_call", tool_name=name, argument_keys=sorted(arguments.keys()))
        try:
            handoff = await self._maybe_handoff_browser_goal(name, arguments, mcp_ctx) if name.startswith("browser_") and not self.chatgpt_direct else None
            if handoff is not None:
                result = handoff
            elif extension:
                result = await self.direct_tools.invoke(name, arguments)
            else:
                result = await method(**arguments)
        except BrowserBridgeError as exc:
            if name.startswith("browser_"):
                if self.chatgpt_direct:
                    result = {"status": "failed", "terminal": True, "completed": False,
                              "error": _redact_compat_text(exc), "error_code": "browser_bridge_error",
                              "retryable": True, "suggested_next_tool": "browser_list_tabs"}
                else:
                    result = self._browser_failure_result(name, arguments, exc)
            else:
                raise
        audit(self.ctx.logger, "tool_result", tool_name=name, result_type=type(result).__name__)
        return result

    def _list_directory_sync(self, path: str = ".", recursive: bool = False, limit: int = 500) -> dict[str, Any]:
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        limit = max(1, min(int(limit), 2000))
        entries: list[dict[str, Any]] = []
        ignored = {".git", "node_modules", ".venv", "__pycache__"}
        pending = [root]
        visited_directories = 0
        while pending and len(entries) < limit and visited_directories < 10_000:
            current = pending.pop()
            visited_directories += 1
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for item in children:
                try:
                    relative_parts = item.relative_to(root).parts
                except ValueError:
                    continue
                if any(part in ignored for part in relative_parts):
                    continue
                try:
                    is_directory = item.is_dir()
                except OSError:
                    is_directory = False
                info = {
                    "path": relative_display(item, self.ctx.settings.allowed_workspaces),
                    "name": item.name,
                    "type": "directory" if is_directory else "file",
                }
                if not is_directory:
                    try:
                        info["size"] = item.stat().st_size
                    except OSError:
                        info["size"] = None
                entries.append(info)
                if len(entries) >= limit:
                    break
                if recursive and is_directory and not item.is_symlink():
                    pending.append(item)
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "entries": entries, "truncated": len(entries) >= limit}

    async def _list_directory(self, path: str = ".", recursive: bool = False, limit: int = 500) -> dict[str, Any]:
        # Directory enumeration is synchronous filesystem I/O.  Keep it off
        # the ASGI event loop so a broad request (for example, path="D:\\")
        # cannot make /health and unrelated MCP calls hang.
        return await asyncio.to_thread(self._list_directory_sync, path, recursive, limit)

    async def _get_path_info(self, path: str = ".") -> dict[str, Any]:
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        stat = target.stat()
        return {
            "path": relative_display(target, self.ctx.settings.allowed_workspaces),
            "absolute_path": str(target),
            "name": target.name or str(target),
            "type": "directory" if target.is_dir() else "file",
            "size": stat.st_size if target.is_file() else None,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
            "readonly": not bool(stat.st_mode & 0o200),
        }

    async def _create_directory(self, path: str, parents: bool = True, exist_ok: bool = False) -> dict[str, Any]:
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces)
        if target.exists():
            if target.is_dir() and exist_ok:
                return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "created": False}
            raise FileExistsError(str(target))
        target.mkdir(parents=parents, exist_ok=False)
        return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "created": True}

    async def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not target.is_file():
            raise ValueError("path is not a file")
        if target.stat().st_size > 2_000_000:
            raise ValueError("File exceeds 2 MB read limit")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or len(lines)))
        if start > end + 1:
            raise ValueError("start_line must not be after end_line")
        selected = "\n".join(lines[start - 1:end])
        return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "content": selected, "start_line": start, "end_line": end, "total_lines": len(lines)}

    async def _write_file(self, path: str, content: str, create_dirs: bool = False) -> dict[str, Any]:
        if not isinstance(content, str) or len(content) > 5_000_000:
            raise ValueError("content is required and limited to 5 MB")
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces)
        if target.exists() and target.is_dir():
            raise ValueError("path is a directory")
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            raise FileNotFoundError(str(target.parent))
        temp = target.with_name(target.name + ".web_agent_tmp")
        temp.write_text(content, encoding="utf-8", newline="")
        temp.replace(target)
        return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "bytes": len(content.encode("utf-8"))}

    async def _patch_file(self, path: str, find: str, replace: str, expected_count: int = 1) -> dict[str, Any]:
        if not isinstance(find, str) or not find:
            raise ValueError("find must be a non-empty string")
        if not isinstance(replace, str):
            raise ValueError("replace must be a string")
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not target.is_file():
            raise ValueError("path is not a file")
        if target.stat().st_size > 5_000_000:
            raise ValueError("File exceeds 5 MB patch limit")
        content = target.read_text(encoding="utf-8", errors="replace")
        count = content.count(find)
        if count != max(1, min(int(expected_count), 1000)):
            raise ValueError(f"Expected {expected_count} matches but found {count}")
        updated = content.replace(find, replace)
        temp = target.with_name(target.name + ".web_agent_tmp")
        temp.write_text(updated, encoding="utf-8", newline="")
        temp.replace(target)
        return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "matches": count, "bytes": len(updated.encode("utf-8"))}

    async def _copy_path(self, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        source_path = resolve_workspace_path(source, self.ctx.settings.allowed_workspaces, must_exist=True)
        destination_path = resolve_workspace_path(destination, self.ctx.settings.allowed_workspaces)
        if destination_path.exists() and not overwrite:
            raise FileExistsError(str(destination_path))
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path, dirs_exist_ok=overwrite)
        else:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
        return {"source": relative_display(source_path, self.ctx.settings.allowed_workspaces), "destination": relative_display(destination_path, self.ctx.settings.allowed_workspaces)}

    async def _move_path(self, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        source_path = resolve_workspace_path(source, self.ctx.settings.allowed_workspaces, must_exist=True)
        destination_path = resolve_workspace_path(destination, self.ctx.settings.allowed_workspaces)
        if destination_path.exists() and not overwrite:
            raise FileExistsError(str(destination_path))
        if destination_path.exists() and overwrite:
            raise ValueError("overwrite move is disabled; delete the destination explicitly first")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(destination_path))
        return {"source": relative_display(source_path, self.ctx.settings.allowed_workspaces), "destination": relative_display(destination_path, self.ctx.settings.allowed_workspaces)}

    async def _delete_path(self, path: str, recursive: bool = False, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise PermissionError("delete_path requires confirm=true")
        target = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if any(target == root.resolve() for root in self.ctx.settings.allowed_workspaces):
            raise PermissionError("Deleting a configured workspace root is not allowed")
        if target.is_dir():
            if not recursive:
                raise ValueError("Directory deletion requires recursive=true")
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"path": relative_display(target, self.ctx.settings.allowed_workspaces), "deleted": True}

    def _find_paths_sync(
        self,
        query: str,
        path: str = ".",
        kind: str = "any",
        case_sensitive: bool = False,
        max_results: int = 200,
        max_depth: int = 32,
    ) -> dict[str, Any]:
        """Find workspace paths without requiring the caller to know the tree.

        The old search workflow only inspected file contents.  That is a poor
        fit for locating a script by project name, and it made a legitimate
        nested repository look missing when the model searched the drive.  A
        bounded path index-style walk is both cheaper and more reliable for
        that first discovery step.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        normalized_kind = str(kind or "any").strip().lower()
        if normalized_kind not in {"any", "file", "directory"}:
            raise ValueError("kind must be any, file, or directory")
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        try:
            result_limit = max(1, min(int(max_results), 1000))
            depth_limit = max(0, min(int(max_depth), 64))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_results and max_depth must be integers") from exc

        ignored = {".git", "node_modules", ".venv", "__pycache__", "08_web_data", "09_web_logs"}
        drive_noise = {"$recycle.bin", "system volume information", "$extend", "windows", "program files", "program files (x86)"}
        pending = [(root, 0)]
        results: list[dict[str, Any]] = []
        directories_seen = 0
        entries_seen = 0
        deadline = time.monotonic() + 30.0

        def matches(item: Path) -> tuple[int, str] | None:
            return _discovery_match_score(item, query, case_sensitive)

        while pending and len(results) < result_limit:
            if time.monotonic() >= deadline:
                results.sort(key=lambda item: (-int(item.get("score", 0)), int(item.get("depth", 0)), str(item.get("path", "")).lower()))
                return {
                    "query": query,
                    "root": relative_display(root, self.ctx.settings.allowed_workspaces),
                    "results": results,
                    "truncated": True,
                    "timed_out": True,
                    "entries_scanned": entries_seen,
                    "directories_scanned": directories_seen,
                }
            current, depth = pending.pop()
            directories_seen += 1
            if root.is_file():
                children = [root]
            else:
                try:
                    children = sorted(
                        current.iterdir(),
                        key=lambda item: (
                            item.is_dir() is False,
                            -_discovery_branch_score(item, query, case_sensitive),
                            item.name.lower(),
                        ),
                    )
                except OSError:
                    continue
            next_directories: list[tuple[Path, int]] = []
            for item in children:
                entries_seen += 1
                try:
                    relative_parts = item.relative_to(root).parts
                except ValueError:
                    continue
                if any(part in ignored for part in relative_parts) or (root.parent == root and any(part.lower() in drive_noise for part in relative_parts)):
                    continue
                try:
                    is_directory = item.is_dir()
                except OSError:
                    is_directory = False
                match = matches(item)
                if match and (normalized_kind == "any" or (is_directory and normalized_kind == "directory") or (not is_directory and normalized_kind == "file")):
                    info: dict[str, Any] = {
                        "path": relative_display(item, self.ctx.settings.allowed_workspaces),
                        "name": item.name,
                        "type": "directory" if is_directory else "file",
                        "depth": depth + 1,
                        "match": match[1],
                        "score": match[0],
                    }
                    if not is_directory:
                        try:
                            info["size"] = item.stat().st_size
                        except OSError:
                            info["size"] = None
                    results.append(info)
                    if len(results) >= result_limit:
                        break
                if is_directory and depth < depth_limit:
                    try:
                        if not item.is_symlink():
                            next_directories.append((item, depth + 1))
                    except OSError:
                        continue
            # Prefer a useful project-like branch and descend it before the
            # remaining drive-level branches.  A breadth-first walk spent the
            # whole timeout in unrelated user/audit folders before reaching a
            # legitimate nested automation directory.
            pending.extend(reversed(next_directories))
            if root.is_file():
                break

        results.sort(key=lambda item: (-int(item.get("score", 0)), int(item.get("depth", 0)), str(item.get("path", "")).lower()))
        return {
            "query": query,
            "root": relative_display(root, self.ctx.settings.allowed_workspaces),
            "results": results,
            "truncated": bool(pending) or len(results) >= result_limit,
            "timed_out": False,
            "entries_scanned": entries_seen,
            "directories_scanned": directories_seen,
        }

    async def _find_paths(
        self,
        query: str,
        path: str = ".",
        kind: str = "any",
        case_sensitive: bool = False,
        max_results: int = 200,
        max_depth: int = 32,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._find_paths_sync, query, path, kind, case_sensitive, max_results, max_depth)

    def _search_file_sync(
        self,
        query: str,
        path: str = ".",
        case_sensitive: bool = False,
        max_results: int = 200,
        include_paths: bool = True,
        max_depth: int = 32,
    ) -> dict[str, Any]:
        if not query:
            raise ValueError("query is required")
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        needle = query if case_sensitive else query.lower()
        result_limit = max(1, min(int(max_results), 1000))
        depth_limit = max(0, min(int(max_depth), 64))
        results: list[dict[str, Any]] = []
        ignored = {".git", "node_modules", ".venv", "__pycache__", "08_web_data", "09_web_logs"}
        pending = [(root, 0)] if root.is_dir() else []
        files_seen = 0
        directories_seen = 0
        deadline = time.monotonic() + 30.0

        def path_match(item: Path) -> tuple[int, str] | None:
            return _discovery_match_score(item, query, case_sensitive)

        def add_path_result(item: Path, is_directory: bool, depth: int) -> bool:
            if not include_paths:
                return False
            match = path_match(item)
            if not match:
                return False
            info: dict[str, Any] = {
                "path": relative_display(item, self.ctx.settings.allowed_workspaces),
                "kind": "path",
                "type": "directory" if is_directory else "file",
                "depth": depth,
                "match": match[1],
                "score": match[0],
            }
            if not is_directory:
                try:
                    info["size"] = item.stat().st_size
                except OSError:
                    info["size"] = None
            results.append(info)
            return len(results) >= result_limit

        def scan_file(file: Path) -> bool:
            nonlocal files_seen
            files_seen += 1
            try:
                if file.stat().st_size > 2_000_000:
                    return False
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return False
            for number, line in enumerate(lines, 1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    results.append({"path": relative_display(file, self.ctx.settings.allowed_workspaces), "line": number, "text": line[:1000]})
                    if len(results) >= result_limit:
                        return True
            return False

        if root.is_file():
            add_path_result(root, False, 0)
            scan_file(root)
        else:
            while pending and files_seen < 20_000 and directories_seen < 10_000:
                if time.monotonic() >= deadline:
                    return {"query": query, "results": results, "truncated": True, "timed_out": True, "files_scanned": files_seen}
                current, depth = pending.pop()
                directories_seen += 1
                try:
                    children = sorted(
                        current.iterdir(),
                        key=lambda item: (
                            item.is_dir() is False,
                            -_discovery_branch_score(item, query, case_sensitive),
                            item.name.lower(),
                        ),
                    )
                except OSError:
                    continue
                next_directories: list[tuple[Path, int]] = []
                for item in children:
                    try:
                        relative_parts = item.relative_to(root).parts
                    except ValueError:
                        continue
                    if any(part in ignored for part in relative_parts) or (root.parent == root and any(part.lower() in drive_noise for part in relative_parts)):
                        continue
                    try:
                        is_directory = item.is_dir()
                        if add_path_result(item, is_directory, depth + 1):
                            return {"query": query, "results": results, "truncated": True, "files_scanned": files_seen, "directories_scanned": directories_seen}
                        if is_directory:
                            if not item.is_symlink():
                                if depth < depth_limit:
                                    next_directories.append((item, depth + 1))
                            continue
                    except OSError:
                        continue
                    if scan_file(item):
                        return {"query": query, "results": results, "truncated": True, "files_scanned": files_seen}
                    if time.monotonic() >= deadline:
                        return {"query": query, "results": results, "truncated": True, "timed_out": True, "files_scanned": files_seen}
                pending.extend(reversed(next_directories))
        truncated = bool(pending or files_seen >= 20_000 or directories_seen >= 10_000)
        return {"query": query, "results": results, "truncated": truncated, "files_scanned": files_seen}

    async def _search_file(
        self,
        query: str,
        path: str = ".",
        case_sensitive: bool = False,
        max_results: int = 200,
        include_paths: bool = True,
        max_depth: int = 32,
    ) -> dict[str, Any]:
        # A full-drive search must never monopolize the MCP/ASGI event loop.
        return await asyncio.to_thread(self._search_file_sync, query, path, case_sensitive, max_results, include_paths, max_depth)

    async def _inspect_project(self, path: str = ".") -> dict[str, Any]:
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        manifests = ["pyproject.toml", "requirements.txt", "package.json", "Cargo.toml", "go.mod", "README.md", ".gitignore"]
        present = [name for name in manifests if (root / name).exists()]
        tree = await self._list_directory(path, recursive=True, limit=300)
        git = root / ".git"
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "manifests": present, "has_git": git.exists(), "tree": tree["entries"], "tree_truncated": tree["truncated"]}

    async def _modify_project(self, path: str = ".", files: dict[str, str] | None = None, changes: list[dict[str, Any]] | None = None, create_dirs: bool = True) -> dict[str, Any]:
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        operations: list[tuple[str, str]] = []
        for relative, content in (files or {}).items():
            operations.append((relative, content))
        for change in changes or []:
            if not isinstance(change, dict) or "path" not in change or "content" not in change:
                raise ValueError("Each change requires path and content")
            operations.append((str(change["path"]), str(change["content"])))
        if not operations or len(operations) > 100:
            raise ValueError("Provide 1-100 explicit file changes")
        written = []
        for relative, content in operations:
            target = resolve_workspace_path(str(root / relative), self.ctx.settings.allowed_workspaces)
            if target.exists() and target.is_dir():
                raise ValueError(f"Cannot write directory: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True) if create_dirs else None
            if not target.parent.exists():
                raise FileNotFoundError(str(target.parent))
            temp = target.with_name(target.name + ".web_agent_tmp")
            temp.write_text(content, encoding="utf-8", newline="")
            temp.replace(target)
            written.append(relative_display(target, self.ctx.settings.allowed_workspaces))
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "written": written}

    async def _get_system_info(self) -> dict[str, Any]:
        try:
            import psutil

            memory = {"total": psutil.virtual_memory().total, "available": psutil.virtual_memory().available}
            disk = {"total": psutil.disk_usage(str(self.ctx.settings.workspace_root)).total, "free": psutil.disk_usage(str(self.ctx.settings.workspace_root)).free}
        except Exception:
            memory = None
            disk = None
        return {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "python": sys.version,
            "executable": sys.executable,
            "node": shutil.which("node"),
            "wsl": shutil.which("wsl.exe") or shutil.which("wsl"),
            "workspace_root": str(self.ctx.settings.workspace_root),
            "admin_mode": self.ctx.settings.admin_mode,
            "memory": memory,
            "disk": disk,
        }

    async def _list_processes(self, name_contains: str | None = None, limit: int = 100) -> dict[str, Any]:
        try:
            import psutil
        except ImportError as exc:  # pragma: no cover - dependency is installed by pyproject
            raise RuntimeError("psutil is not installed") from exc
        needle = (name_contains or "").lower()
        processes: list[dict[str, Any]] = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = str(process.info.get("name") or "")
                if needle and needle not in name.lower():
                    continue
                processes.append({"pid": process.info.get("pid"), "name": name, "exe": process.info.get("exe")})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if len(processes) >= max(1, min(int(limit), 500)):
                break
        return {"processes": processes, "truncated": len(processes) >= max(1, min(int(limit), 500))}

    async def _run_git(self, root: Path, args: list[str], timeout: int = 30) -> str:
        executable = shutil.which("git") or "git"
        process = await asyncio.create_subprocess_exec(
            executable,
            "-C",
            str(root),
            *args,
            cwd=str(root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=max(1, min(int(timeout), 120)))
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("Git operation timed out")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ValueError(stderr.strip() or stdout.strip() or "Git operation failed")
        return stdout

    def _project_root(self, path: str) -> Path:
        root = resolve_workspace_path(path, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("project path is not a directory")
        return root

    async def _git_status(self, path: str = ".") -> dict[str, Any]:
        root = self._project_root(path)
        output = await self._run_git(root, ["status", "--short", "--branch"])
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "status": output}

    async def _git_diff(self, path: str = ".", staged: bool = False, file_path: str | None = None, max_chars: int = 200_000) -> dict[str, Any]:
        root = self._project_root(path)
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file_path:
            candidate = (root / file_path).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError("file_path is outside the project") from exc
            args.extend(["--", str(candidate.relative_to(root.resolve()))])
        output = await self._run_git(root, args)
        max_chars = max(1, min(int(max_chars), 1_000_000))
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "diff": output[:max_chars], "truncated": len(output) > max_chars}

    async def _git_log(self, path: str = ".", limit: int = 20) -> dict[str, Any]:
        root = self._project_root(path)
        limit = max(1, min(int(limit), 100))
        output = await self._run_git(root, ["log", "--oneline", "--decorate", "--no-color", f"--max-count={limit}"])
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "log": output}

    async def _git_branches(self, path: str = ".") -> dict[str, Any]:
        root = self._project_root(path)
        output = await self._run_git(root, ["branch", "--all", "--no-color"])
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "branches": output}

    async def _git_commit(self, path: str, message: str, paths: list[str], confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise PermissionError("git_commit requires confirm=true")
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise ValueError("message must be 1-500 characters")
        if not isinstance(paths, list) or not paths or len(paths) > 100:
            raise ValueError("paths must contain 1-100 explicit project-relative paths")
        root = self._project_root(path)
        normalized: list[str] = []
        for item in paths:
            candidate = (root / str(item)).resolve()
            try:
                normalized.append(str(candidate.relative_to(root.resolve())))
            except ValueError as exc:
                raise ValueError(f"Git path is outside the project: {item}") from exc
        await self._run_git(root, ["add", "--", *normalized])
        output = await self._run_git(root, ["commit", "-m", message])
        return {"root": relative_display(root, self.ctx.settings.allowed_workspaces), "committed": normalized, "output": output}

    async def _execute_wsl_script(self, script: str, distribution: str | None, timeout: int = 30) -> tuple[int, str, str]:
        command = ["wsl.exe"]
        if distribution:
            command.extend(["--distribution", distribution])
        command.extend(["--", "bash", "-lc", script])
        if should_use_active_user_context():
            result = await asyncio.to_thread(
                run_command_as_active_user,
                command,
                self.ctx.settings.workspace_root,
                timeout,
            )
            if result.timed_out:
                raise ValueError("WSL directory operation timed out")
            return result.return_code, result.stdout, result.stderr
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.ctx.settings.workspace_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=max(1, min(int(timeout), 120)))
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("WSL directory operation timed out")
        return (
            process.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def _list_wsl_directory(
        self,
        path: str = "/",
        distribution: str | None = None,
        recursive: bool = False,
        limit: int = 500,
        timeout: int = 30,
    ) -> dict[str, Any]:
        path_distribution, linux_path = normalize_wsl_path(path, distribution)
        limit = max(1, min(int(limit), 2000))
        max_depth = 20 if recursive else 1
        quoted_path = shlex.quote(linux_path)
        script = (
            "set -euo pipefail\n"
            f"test -d {quoted_path}\n"
            f"find -- {quoted_path} -mindepth 1 -maxdepth {max_depth} "
            "-printf '%y\\t%s\\t%p\\n' | head -n "
            f"{limit}"
        )
        return_code, stdout, stderr = await self._execute_wsl_script(script, path_distribution, timeout)
        if return_code != 0:
            detail = stderr.strip() or stdout.strip() or "WSL directory operation failed"
            raise ValueError(detail)
        entries: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            entry_type, size, entry_path = parts
            entries.append(
                {
                    "path": entry_path,
                    "name": entry_path.rstrip("/").rsplit("/", 1)[-1],
                    "type": {"d": "directory", "f": "file", "l": "symlink"}.get(entry_type, "other"),
                    "size": int(size) if size.isdigit() else None,
                }
            )
        return {
            "distribution": path_distribution or "default",
            "root": linux_path,
            "entries": entries,
            "truncated": len(entries) >= limit,
        }

    async def _discover_environment(
        self,
        request: str = "",
        cwd: str | None = None,
        distribution: str | None = None,
        roots: list[str] | None = None,
        max_results: int = 120,
        read_limit: int = 6_000,
        max_depth: int = 32,
    ) -> dict[str, Any]:
        """Discover local task context without executing discovered assets."""

        return await asyncio.to_thread(
            discover_environment_readonly,
            self.ctx.settings,
            request,
            cwd,
            distribution,
            roots,
            max_results,
            read_limit,
            max_depth,
        )

    async def _search_web(
        self,
        query: str,
        max_results: int = 5,
        domains: list[str] | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        """Search public documentation without exposing local credentials."""

        return await search_public_web(query, max_results, domains, timeout_seconds)

    async def _self_check(
        self,
        operation: str = "",
        task_id: str | None = None,
        error_text: str = "",
        expected_paths: list[str] | None = None,
        browser_check: bool = False,
        auto_search: bool = True,
        search_domains: list[str] | None = None,
        search_max_results: int = 5,
    ) -> dict[str, Any]:
        """Run read-only local checks and search public troubleshooting evidence."""

        task = await self.tasks.get(task_id) if task_id else None
        streams: dict[str, str] = {}
        actual_error = str(error_text or "").strip()
        task_status = str(task.get("status") or "") if task else ""
        if task and task_status in {"queued", "running"}:
            return {
                "status": task_status,
                "terminal": False,
                "completed": False,
                "final_answer_allowed": False,
                "do_not_reply": True,
                "repair_required": False,
                "recoverable_error": False,
                "workflow_status": "in_progress",
                "task_id": task_id,
                "next_action": "monitor_task",
                "message": "自检发现任务仍在运行；必须继续调用 monitor_task，不能把运行中状态当成结果。",
                "local_checks": {
                    "task": {
                        "task_id": task_id,
                        "profile": task.get("profile"),
                        "status": task_status,
                    }
                },
            }

        if task:
            streams = await self._capture_task_streams(str(task_id))
            if not actual_error and task_status == "failed":
                actual_error = _failure_text(task, streams).strip() or "task failed without diagnostic text"
            elif not actual_error and task_status == "cancelled":
                actual_error = str(task.get("error") or "task was cancelled").strip()

        expected_checks: list[dict[str, Any]] = []
        missing_expected: list[str] = []
        for raw_path in (expected_paths or [])[:50]:
            path_text = str(raw_path or "").strip()
            if not path_text:
                continue
            try:
                target = resolve_workspace_path(path_text, self.ctx.settings.allowed_workspaces, must_exist=False)
                exists = target.exists()
                record = {
                    "path": relative_display(target, self.ctx.settings.allowed_workspaces),
                    "exists": exists,
                    "is_file": target.is_file() if exists else False,
                    "is_directory": target.is_dir() if exists else False,
                }
                if exists and target.is_file():
                    record["size"] = target.stat().st_size
                expected_checks.append(record)
                if not exists:
                    missing_expected.append(path_text)
            except (OSError, ValueError) as exc:
                expected_checks.append({"path": path_text, "valid": False, "error": str(exc)[:500]})
                missing_expected.append(path_text)
        if missing_expected and not actual_error:
            actual_error = "self_check final output validation failed: missing expected output(s): " + ", ".join(missing_expected)

        browser_state: dict[str, Any] | None = None
        if browser_check:
            try:
                browser_state = await self.ctx.browser.list_tabs()
            except BrowserBridgeError as exc:
                browser_state = {"status": "failed", "error": str(exc)[:1_000]}
                if not actual_error:
                    actual_error = "browser bridge self-check failed: " + str(exc)

        task_summary = None
        if task:
            task_summary = {
                key: task.get(key)
                for key in ("id", "profile", "status", "error", "result", "output_files")
                if key in task
            }
        local_checks: dict[str, Any] = {
            "system": await self._get_system_info(),
            "runtime": {
                "mcp_port": self.ctx.settings.port,
                "browser_bridge_url": self.ctx.settings.browser_bridge_url,
                "workspace_root": str(self.ctx.settings.workspace_root),
            },
            "task": task_summary,
            "expected_outputs": expected_checks,
        }
        if streams:
            local_checks["task_streams"] = {
                "stdout_tail": _tail(streams.get("stdout", "")),
                "stderr_tail": _tail(streams.get("stderr", "")),
            }
        if browser_state is not None:
            local_checks["browser"] = browser_state

        if not actual_error:
            return {
                "status": "healthy",
                "terminal": True,
                "completed": True,
                "final_answer_allowed": True,
                "do_not_reply": False,
                "repair_required": False,
                "recoverable_error": False,
                "workflow_status": "self_check_passed",
                "next_action": None,
                "local_checks": local_checks,
                "message": "自检通过；没有发现需要修复的错误或缺失输出。",
            }

        current = {
            "status": "failed",
            "error": actual_error,
            "result": task.get("result") if task else {},
        }
        classification = _classify_recoverable_failure(
            str(operation or (task.get("profile") if task else "self_check")),
            {},
            current,
            streams,
        )
        if classification is None:
            classification = {
                "error_class": "unknown_diagnosable_failure",
                "repair_hint": (
                    "Inspect the exact local evidence and public documentation. "
                    "Apply the smallest safe correction, then retry with a changed "
                    "invocation and fresh output checks."
                ),
            }
        error_class = str(classification["error_class"])
        query = build_search_query(operation, error_class, actual_error)
        web_search: dict[str, Any] | None = None
        search_error: str | None = None
        if auto_search:
            try:
                web_search = await search_public_web(
                    query,
                    max_results=search_max_results,
                    domains=search_domains,
                )
            except (WebSearchError, ValueError) as exc:
                search_error = str(exc)[:1_000]

        results = list(web_search.get("results") or []) if web_search else []
        hard_markers = (
            "permission denied",
            "access is denied",
            "access denied",
            "not authorized",
            "unauthorized",
            "forbidden",
            "invalid token",
            "token is not valid",
            "no space left on device",
            "disk full",
            "user cancelled",
            "user canceled",
            "cancel requested",
        )
        terminal_failure = any(marker in actual_error.lower() for marker in hard_markers)
        search_payload: dict[str, Any] = {
            "performed": bool(web_search),
            "required": not bool(results),
            "query": query,
            "results": results,
            "source_urls": [str(item.get("url")) for item in results if item.get("url")],
            "instruction": (
                "Use these public results as troubleshooting evidence. Prefer official "
                "documentation or the project's issue tracker, do not copy commands "
                "blindly, and do not send local paths or secrets to a search engine."
            ),
        }
        if search_error:
            search_payload["error"] = search_error

        evidence = {
            "operation": sanitize_search_text(operation, 120),
            "error_class": error_class,
            "error": actual_error[:4_000],
            "local_checks": local_checks,
        }
        if terminal_failure:
            return {
                "status": "terminal_failure",
                "terminal": True,
                "completed": False,
                "final_answer_allowed": True,
                "do_not_reply": False,
                "repair_required": False,
                "recoverable_error": False,
                "workflow_status": "self_check_terminal_failure",
                "error_class": error_class,
                "error": actual_error[:4_000],
                "diagnostic_evidence": evidence,
                "web_search": search_payload,
                "next_action": None,
                "message": "自检完成；该错误属于权限、认证、取消或存储类终态，已附上公开检索证据。",
            }

        return {
            "status": "recoverable_error",
            "terminal": False,
            "completed": False,
            "final_answer_allowed": False,
            "do_not_reply": True,
            "repair_required": True,
            "recoverable_error": True,
            "workflow_status": "self_check_repair_required",
            "error_class": error_class,
            "error": actual_error[:4_000],
            "repair_hint": classification["repair_hint"],
            "diagnostic_evidence": evidence,
            "web_search": search_payload,
            "web_search_required": not bool(results),
            "self_check_completed": True,
            "next_action": (
                "inspect_search_evidence_apply_minimal_fix_and_retry"
                if results
                else "search_web_with_a_changed_public_query_then_apply_fix_and_retry"
            ),
            "message": (
                "自检发现可恢复错误并已搜索公开网页。不得向用户结束；先结合本地证据和检索结果做最小修复，"
                "再以不同参数或不同源配置重试，直到 fresh final checks 通过。"
            ),
        }

    async def _browser_start(self, url: str | None = None) -> dict[str, Any]:
        return await self._browser_open_tab(url)

    async def _browser_list_tabs(self) -> dict[str, Any]:
        return await self.ctx.browser.list_tabs()

    async def _browser_list_downloads(self, limit: int = 20) -> dict[str, Any]:
        return await self.ctx.browser.list_downloads(limit)

    async def _browser_open_tab(self, url: str | None = None) -> dict[str, Any]:
        return await self.ctx.browser.open_tab(url)

    async def _browser_navigate(self, tab_id: str, url: str, timeout: int = 30_000) -> dict[str, Any]:
        return await self.ctx.browser.navigate(tab_id, url, timeout)

    @staticmethod
    def _annotate_browser_page(page: dict[str, Any]) -> dict[str, Any]:
        """Turn a verified GLYCAM result page into an explicit answer boundary.

        Chat models previously had to infer completion from a long rendered
        page.  On larger oligosaccharides that inference sometimes ended with
        a truncated final message even though GLYCAM had already completed the
        project.  Keep the raw page unchanged, but add a small machine-readable
        status contract so the model can distinguish an in-progress page from
        a verified terminal result.
        """
        if not isinstance(page, dict):
            return page
        url = str(page.get("url") or "").lower()
        if "glycam.org/" not in url:
            return page

        text = str(page.get("text") or "")
        normalized = re.sub(r"\s+", " ", text).lower()
        on_download_page = "/cb/download/" in url or "步骤3" in text or "step 3" in normalized
        completed_marker = (
            ("项目完成" in text or "project completed" in normalized)
            and ("模拟正常结束" in text or "simulation finished normally" in normalized)
        )
        has_structure_control = any(
            "download-structure" in str(item.get("id") or "")
            for item in (page.get("interactive_elements") or [])
            if isinstance(item, dict)
        )
        if on_download_page and completed_marker and has_structure_control:
            page.update(
                {
                    "terminal": True,
                    "completed": True,
                    "user_visible_final": True,
                    "do_not_reply": False,
                    "workflow_status": "terminal_success",
                    "completion_evidence": [
                        "GLYCAM download page reached",
                        "project completed",
                        "simulation finished normally",
                        "PDB download control present",
                    ],
                }
            )
            return page

        page.update(
            {
                "terminal": False,
                "completed": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "workflow_status": "in_progress",
                "follow_up_required": True,
            }
        )
        return page

    async def _browser_read_page(
        self,
        tab_id: str | None = None,
        max_chars: int = 20_000,
        settle_ms: int = 750,
        include_frames: bool = False,
        selector: str | None = None,
        mode: str = "auto",
    ) -> dict[str, Any]:
        page = await self.ctx.browser.read_page(tab_id, max_chars, settle_ms, include_frames, selector, mode)
        return page if self.chatgpt_direct else self._annotate_browser_page(page)

    async def _browser_read(self, tab_id: str | None = None, max_chars: int = 20_000) -> dict[str, Any]:
        """Compatibility alias for the older browser_read contract."""

        return await self._browser_read_page(tab_id, max_chars, 750, False, None, "auto")

    async def _browser_click(
        self,
        tab_id: str,
        by: str | None = None,
        selector: str | None = None,
        name: str | None = None,
        exact: bool = False,
        timeout: int = 15_000,
    ) -> dict[str, Any]:
        return await self.ctx.browser.click(tab_id, by, selector, name, exact, timeout)

    async def _browser_type(
        self,
        tab_id: str,
        text: str,
        by: str | None = None,
        selector: str | None = None,
        name: str | None = None,
        exact: bool = False,
        clear: bool = True,
        timeout: int = 15_000,
    ) -> dict[str, Any]:
        return await self.ctx.browser.type_text(tab_id, text, by, selector, name, exact, clear, timeout)

    async def _browser_press(
        self,
        tab_id: str,
        key: str,
        by: str | None = None,
        selector: str | None = None,
        name: str | None = None,
        exact: bool = False,
        timeout: int = 15_000,
    ) -> dict[str, Any]:
        from web_tools.browser_bridge import PressRequest

        request = PressRequest(
            tab_id=tab_id,
            key=key,
            by=by,
            selector=selector,
            name=name,
            exact=exact,
            timeout=timeout,
        )
        return await self.ctx.browser.press_key(request)

    async def _browser_instances(self, timeout: int = 10_000) -> dict[str, Any]:
        return await self.ctx.browser.browser_instances(timeout=timeout)

    async def _browser_control(self, browser_id: str, action: str, tab_id: str | None = None,
                               arguments: dict[str, Any] | None = None, operation_id: str | None = None,
                               confirm: bool = False, timeout: int = 30_000) -> dict[str, Any]:
        reads = {"status", "list_tabs", "read", "screenshot", "downloads", "task_status"}
        if action not in reads and not self.ctx.settings.admin_mode:
            raise PermissionError("Browser writes require the existing admin mode permission")
        return await self.ctx.browser.browser_control(browser_id=browser_id, action=action, tab_id=tab_id,
            arguments=arguments, operation_id=operation_id, confirm=confirm, timeout=timeout)

    async def _browser_debug_control(self, browser_id: str, tab_id: str, action: str = "status",
                                     arguments: dict[str, Any] | None = None,
                                     operation_id: str | None = None, confirm: bool = False,
                                     timeout: int = 10_000) -> dict[str, Any]:
        if action not in {"status", "attach", "snapshot", "detach"}:
            raise ValueError("Unsupported bounded debugger action")
        if action in {"attach", "detach"} and not self.ctx.settings.admin_mode:
            raise PermissionError("Debugger changes require the existing admin mode permission")
        return await self.ctx.browser.browser_control(browser_id=browser_id, action="debug_" + action,
            tab_id=tab_id, arguments=arguments, operation_id=operation_id, confirm=confirm, timeout=timeout)

    async def _browser_upload_file(self, tab_id: str, selector: str, file_path: str,
                                   idempotency_key: str, confirm: bool = False,
                                   timeout: int = 30_000) -> dict[str, Any]:
        if not self.ctx.settings.admin_mode:
            raise PermissionError("Media upload requires the existing admin mode permission")
        if confirm is not True:
            raise PermissionError("File upload requires explicit confirm=true")
        return await self.ctx.browser.upload_file(tab_id=tab_id, selector=selector,
            file_path=file_path, confirm=confirm, timeout=timeout, idempotency_key=idempotency_key)

    async def _browser_upload_status(self, idempotency_key: str) -> dict[str, Any]:
        return await self.ctx.browser.upload_status(idempotency_key)

    async def _video_project_path(self, date: str | None = None, create: bool = False) -> dict[str, Any]:
        from web_tools.video_paths import video_project_path
        if create and not self.ctx.settings.admin_mode:
            raise PermissionError("Creating a video project directory requires existing admin mode")
        return await asyncio.to_thread(video_project_path, date, create)

    async def _browser_select_option(
        self,
        tab_id: str,
        option: str,
        option_by: str = "label",
        by: str | None = None,
        selector: str | None = None,
        name: str | None = None,
        exact: bool = False,
        timeout: int = 15_000,
    ) -> dict[str, Any]:
        return await self.ctx.browser.select_option(tab_id, option, option_by, by, selector, name, exact, timeout)

    async def _browser_wait(self, tab_id: str, milliseconds: int = 1_000) -> dict[str, Any]:
        waited = await self.ctx.browser.wait(tab_id, milliseconds)
        # A wait that returns no page state is easy for a chat model to treat
        # as an implicit stopping point. Attach a bounded observation to the
        # same result so a slow SPA/download flow remains one continuous
        # browser operation from the model's point of view. If the page cannot
        # be read, preserve the successful wait result and let the next read
        # report the concrete browser error.
        try:
            page = await self.ctx.browser.read_page(
                tab_id,
                max_chars=12_000,
                settle_ms=0,
                include_frames=False,
                mode="auto",
            )
        except Exception as exc:
            return {
                **waited,
                "after_wait_read_error": f"{type(exc).__name__}: {exc}",
                "verification": "page_read_pending",
                "follow_up_required": True,
            }
        return {**waited, "after_wait": page if self.chatgpt_direct else self._annotate_browser_page(page)}

    async def _browser_screenshot(self, tab_id: str, full_page: bool = False) -> dict[str, Any]:
        return await self.ctx.browser.screenshot(tab_id, full_page)

    async def _browser_close_tab(self, tab_id: str) -> dict[str, Any]:
        return await self.ctx.browser.close_tab(tab_id)

    @staticmethod
    def _browser_tabs_from_result(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            raw = value.get("tabs", value.get("result", []))
        else:
            raw = value
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    async def _infer_latest_chat_goal(self) -> tuple[str, str, str] | None:
        """Find the newest actionable user request in the visible ChatGPT tab."""

        if self.chatgpt_direct:
            return None
        tabs_result = await self._browser_list_tabs()
        tabs = self._browser_tabs_from_result(tabs_result)
        candidates = [
            item
            for item in tabs
            if "chatgpt.com/c/" in str(item.get("url") or "").lower()
            or "chat.openai.com/c/" in str(item.get("url") or "").lower()
        ]
        # Prefer the active conversation, then the highest tab id.  Edge can
        # leave an older ChatGPT tab earlier in the list while a newly opened
        # conversation is already the source of the current MCP call.
        def tab_number(item: dict[str, Any]) -> int:
            raw = str(item.get("tab_id") or item.get("id") or "")
            match = re.search(r"(\d+)$", raw)
            return int(match.group(1)) if match else -1

        candidates.sort(key=lambda item: (bool(item.get("active")), tab_number(item)), reverse=True)
        for tab in candidates[:8]:
            tab_id = str(tab.get("tab_id") or tab.get("id") or "").strip()
            if not tab_id:
                continue
            try:
                page = await self.ctx.browser.read_page(
                    tab_id,
                    max_chars=18_000,
                    settle_ms=0,
                    include_frames=False,
                    mode="fast",
                )
            except Exception:
                continue
            page_text = str(page.get("text") or "") if isinstance(page, dict) else ""
            goal = _extract_latest_chat_goal(page_text)
            if goal and _looks_like_autonomous_chat_goal(goal):
                return tab_id, goal, _chat_goal_signature(page_text, goal)
        return None

    async def _maybe_handoff_browser_goal(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        mcp_ctx: Any = None,
    ) -> dict[str, Any] | None:
        """Promote an accidental low-level browser turn to one server call.

        The MCP instructions strongly prefer ``run_autonomous_workflow``, but
        a web ChatGPT session can have a cached tool list or choose
        ``browser_wait``/``browser_click`` directly.  In that case returning
        one more intermediate browser result recreates the exact "click
        Continue" failure.  When the target is not the ChatGPT page, inspect
        the newest ChatGPT user turn and, if it is a genuine multi-step goal,
        run the same supervisor here.  Internal supervisor actions call the
        browser client directly, so this hand-off cannot recurse.
        """

        if self.chatgpt_direct:
            return None
        trigger_tools = {
            "browser_read_page",
            "browser_read",
            "browser_click",
            "browser_type",
            "browser_press",
            "browser_select_option",
            "browser_wait",
            "browser_wait_until",
            "browser_screenshot",
            "browser_list_downloads",
        }
        if tool_name not in trigger_tools:
            return None

        tab_id = str(arguments.get("tab_id") or "").strip()
        if tab_id:
            try:
                target_page = await self.ctx.browser.read_page(
                    tab_id,
                    max_chars=800,
                    settle_ms=0,
                    include_frames=False,
                    mode="fast",
                )
                target_url = str(target_page.get("url") or "").lower() if isinstance(target_page, dict) else ""
                if "chatgpt.com" in target_url or "chat.openai.com" in target_url:
                    return None
            except Exception:
                # The original browser action will return the concrete bridge
                # diagnostic if the target cannot be inspected.
                pass

        inferred = await self._infer_latest_chat_goal()
        if not inferred:
            return None
        chat_tab_id, goal, turn_signature = inferred
        # Cache only this concrete user-turn instance.  A later identical
        # prompt in the same conversation must be a fresh execution, not a
        # replay of an older terminal result.
        key = f"{chat_tab_id}:{turn_signature}"
        async with self._browser_goal_handoff_lock:
            cached = self._browser_goal_handoffs.get(key)
            if cached is not None:
                return cached.get("result")
            running = self._browser_goal_handoff_tasks.get(key)
            if running is None:
                running = asyncio.create_task(
                    self._run_browser_goal_handoff(goal, chat_tab_id, mcp_ctx)
                )
                self._browser_goal_handoff_tasks[key] = running
        # Do not hold a low-level browser HTTP request open for the complete
        # supervisor.  The browser bridge/proxy can time out long before
        # setup + EM/NVT/NPT + production finishes; that stale 504 used to be
        # mistaken by ChatGPT for a terminal failure even though the
        # server-owned supervisor was still running successfully.  Return a
        # mandatory checkpoint immediately and let the next addressed browser
        # action rebind to this same key.  The task is intentionally retained
        # in ``_browser_goal_handoff_tasks`` until it reaches a terminal
        # boundary.
        if not running.done():
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "final_answer_allowed": False,
                "do_not_reply": True,
                "continue_required": True,
                "recoverable_error": True,
                "workflow_status": "in_progress",
                "error_class": "browser_goal_handoff_in_progress",
                "message": (
                    "服务器已接管本次网页任务；监督器仍在执行，不能回答用户。"
                    "请继续对同一目标调用 browser_wait 或 monitor_task，直到返回终态。"
                ),
                "next_action": "continue_server_handoff_without_new_task",
                "handoff_id": key,
                "handoff_goal": goal,
                "handoff_chat_tab_id": chat_tab_id,
                "server_handoff": True,
                "handoff_source": "low_level_browser_tool",
            }

        try:
            # A completed supervisor is read exactly once.  Shielding is kept
            # for the tiny completion race so a client disconnect cannot
            # cancel the process-local task before its terminal result is
            # cached for the next browser call.
            result = await asyncio.shield(running)
        except Exception as exc:  # keep the browser call diagnosable
            result = {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "browser_goal_handoff_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "next_action": "retry_the_same_goal_through_run_autonomous_workflow",
            }
        async with self._browser_goal_handoff_lock:
            # A recoverable checkpoint is not a result cache. Caching it by
            # goal made the next identical request replay the old error
            # without executing a fresh browser/build attempt. Cache only a
            # genuine terminal boundary; failures must be retried with fresh
            # browser state and new evidence.
            if isinstance(result, dict) and result.get("terminal") is True:
                self._browser_goal_handoffs[key] = {"result": result, "goal": goal}
            else:
                self._browser_goal_handoffs.pop(key, None)
            self._browser_goal_handoff_tasks.pop(key, None)
        if isinstance(result, dict):
            return {
                **result,
                "server_handoff": True,
                "handoff_goal": goal,
                "handoff_chat_tab_id": chat_tab_id,
                "handoff_source": "low_level_browser_tool",
            }
        return result

    async def _run_browser_goal_handoff(self, goal: str, chat_tab_id: str, mcp_ctx: Any = None) -> dict[str, Any]:
        md_goal, _ = _autonomous_goal_requires_md(goal, None, [])
        result = await self._run_autonomous_workflow(
            goal,
            workflow=None,
            acceptance=[],
            max_cycles=AUTONOMOUS_MAX_CYCLES,
            timeout_seconds=6 * 3_600,
            # The concrete GLYCAM/MD adapter has a server-owned deterministic
            # path, but it must still retain the connected MCP context for a
            # repair/search decision when a browser or runner action fails.
            # Clients without sampling are handled by the bounded
            # deterministic fallback in the supervisor.
            ctx=mcp_ctx,
        )
        result = _compact_terminal_result(_enrich_terminal_result(result))
        return {
            **(result if isinstance(result, dict) else {"result": result}),
            "handoff_goal": goal,
            "handoff_chat_tab_id": chat_tab_id,
        }

    async def _maybe_handoff_runner_goal(
        self,
        provided_goal: str,
        step: dict[str, Any] | None = None,
        mcp_ctx: Any = None,
    ) -> dict[str, Any] | None:
        """Apply the same conversation hand-off to a prematurely chosen runner.

        ``run_wsl_workflow`` and the compatibility runners receive a short
        tool-generated goal (for example, "run WSL diagnostics"), not the
        user's original multi-step request.  If the web model selected one of
        those runners directly, recover the original goal from the current
        ChatGPT tab before the diagnostic can become the visible answer.
        """

        if self.chatgpt_direct:
            return None
        supplied = json.dumps(
            {"goal": provided_goal, "step": step or {}},
            ensure_ascii=False,
            default=str,
        ).lower()
        # Service reloads are administrative operations initiated by the
        # local operator, not a browser task to be handed back to ChatGPT.
        if (
            "restart-service" in supplied
            or "restart_service" in supplied
            or ("web_mcp" in supplied and "restart" in supplied)
            or ("web_worker" in supplied and "restart" in supplied)
        ):
            return None
        step_action = _compat_action(
            (step or {}).get("action", (step or {}).get("kind", (step or {}).get("name", "")))
        )
        # A cached web client often calls run_md_simulation directly after
        # opening GLYCAM.  That low-level call still belongs to the current
        # natural-language goal; otherwise the one-shot wrapper would run
        # only EM/NVT/NPT and silently lose the requested final production
        # stage.  Promote the MD call when the current ChatGPT tab exposes a
        # genuine multi-step goal, even if the short compatibility goal itself
        # is not autonomous-looking.
        if _looks_like_autonomous_chat_goal(provided_goal) and step_action != "md_simulation":
            return None
        return await self._maybe_handoff_browser_goal("browser_wait", {}, mcp_ctx)

    @staticmethod
    def _glycam_sequence_for_goal(goal: str, args: dict[str, Any]) -> tuple[str, int]:
        """Create a valid, deterministic GLYCAM sequence for the requested count.

        The browser is still the authority for the build and download.  This
        helper only removes an avoidable planning failure: a natural-language
        request such as "six sugar" must not depend on the sampled model
        inventing a valid condensed sequence before it can open GLYCAM.
        """

        requested = args.get("sugar_count") or args.get("count")
        try:
            sugar_count = int(requested) if requested is not None else None
        except (TypeError, ValueError):
            sugar_count = None
        if sugar_count is None:
            sugar_count = _requested_sugar_count(goal)
        if sugar_count is None:
            sugar_count = 4
        if sugar_count < 1 or sugar_count > 64:
            raise ValueError(f"GLYCAM sugar_count must be between 1 and 64, got {sugar_count}")

        supplied = str(args.get("sequence") or "").strip()
        sequence = supplied or ("DGlcpb1-4" * (sugar_count - 1) + "DGlcpb1-OH")
        actual = _glycam_sequence_count(sequence)
        if actual != sugar_count:
            raise ValueError(
                f"The GLYCAM sequence contains {actual} sugars but the goal requires {sugar_count}"
            )
        return sequence, sugar_count

    @staticmethod
    def _glycam_target_root(goal: str, args: dict[str, Any], roots: list[Path]) -> Path:
        raw = str(args.get("target_root") or "").strip()
        if not raw:
            # Keep this deliberately conservative: take the Windows path in
            # the user's request, then remove the natural-language suffix
            # commonly attached to it ("里面"/"下").
            match = re.search(r"([A-Za-z]:\\[^\r\n,，。；;]+)", goal)
            raw = match.group(1).strip() if match else ""
        raw = raw.strip(" `\t\r\n\"'")
        raw = re.sub(r"(里面|下|中)$", "", raw).rstrip("\\/")
        if not raw:
            raise ValueError("The GLYCAM/MD request does not contain a usable target_root")
        return resolve_workspace_path(raw, roots, must_exist=False)

    @staticmethod
    def _glycam_tab_id(value: Any) -> str | None:
        if isinstance(value, dict):
            direct = value.get("tab_id") or value.get("id")
            if direct is not None:
                return str(direct)
            for key in ("tab", "active_tab"):
                nested = ToolRegistry._glycam_tab_id(value.get(key))
                if nested:
                    return nested
            for item in value.get("tabs", []) if isinstance(value.get("tabs"), list) else []:
                nested = ToolRegistry._glycam_tab_id(item)
                if nested:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = ToolRegistry._glycam_tab_id(item)
                if nested:
                    return nested
        return None

    @staticmethod
    def _glycam_build_uuid(page: dict[str, Any]) -> str | None:
        text = str(page.get("text") or "")
        uuid_pattern = r"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
        labelled = re.search(
            rf"(?:build|构建)\s*p?uuid[^0-9a-f]+{uuid_pattern}",
            text,
            re.IGNORECASE,
        )
        if labelled:
            return labelled.group(1)
        matches = re.findall(uuid_pattern, text, re.IGNORECASE)
        return matches[-1] if matches else None

    @staticmethod
    def _glycam_conformer_id(page: dict[str, Any]) -> str:
        for item in page.get("interactive_elements") or []:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("id") or "")
            if element_id.startswith("download-all-"):
                suffix = element_id[len("download-all-"):].strip()
                if suffix:
                    return suffix
        return "structure"

    @staticmethod
    def _glycam_archive_members(path: Path) -> list[str]:
        required_suffixes = (".pdb", ".parm7", ".rst7")
        with zipfile.ZipFile(path) as archive:
            members = [str(name) for name in archive.namelist() if str(name) and not str(name).endswith("/")]
        lower = [name.lower() for name in members]
        missing = [suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in lower)]
        if missing:
            raise ValueError("GLYCAM archive is missing required files: " + ", ".join(missing))
        return members

    @staticmethod
    def _fresh_glycam_zip(records: list[dict[str, Any]], started_epoch: float) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            state = str(record.get("state") or record.get("status") or "complete").lower()
            path_text = str(record.get("file_path") or record.get("filename") or "")
            if state not in {"complete", "completed", "success", "succeeded"} or not path_text.lower().endswith(".zip"):
                continue
            ended = _download_end_epoch(record)
            if ended is not None and ended < started_epoch - 5.0:
                continue
            path = Path(path_text)
            if record.get("exists") is False or not path.is_file():
                continue
            candidates.append(record)
        if not candidates:
            return None
        candidates.sort(key=lambda item: _download_end_epoch(item) or started_epoch, reverse=True)
        return candidates[0]

    async def _browser_build_glycam(self, goal: str, args: dict[str, Any]) -> dict[str, Any]:
        """Build and verify the requested glycan in the user's real browser.

        This is a server-owned prerequisite for GLYCAM/MD goals.  It is not a
        synthetic filesystem shortcut: the sequence is typed into GLYCAM,
        the site is waited on until its terminal page is observed, and a
        fresh archive is downloaded from the page's actual build identifier.
        """

        sequence, sugar_count = self._glycam_sequence_for_goal(goal, args)
        target_root = self._glycam_target_root(goal, args, self.ctx.settings.allowed_workspaces)
        target_root.mkdir(parents=True, exist_ok=True)
        started_epoch = time.time()
        timeout_seconds = max(30, min(int(args.get("timeout_seconds", 900)), 1_800))
        tab_id: str | None = None
        page: dict[str, Any] = {}
        evidence: list[str] = []

        try:
            opened = await self._browser_start("https://glycam.org/txt/")
            tab_id = self._glycam_tab_id(opened)
            if not tab_id:
                tabs = await self._browser_list_tabs()
                tab_id = self._glycam_tab_id(tabs)
            if not tab_id:
                raise BrowserBridgeError("GLYCAM browser tab did not return a tab_id")
            evidence.append("GLYCAM text-builder tab opened")

            page = await self._browser_read_page(tab_id, max_chars=24_000)
            if "/txt/" not in str(page.get("url") or ""):
                await self._browser_navigate(tab_id, "https://glycam.org/txt/", 30_000)
                page = await self._browser_read_page(tab_id, max_chars=24_000)

            typed = await self._browser_type(
                tab_id,
                sequence,
                by="css",
                selector="#sequence_input",
                clear=True,
                timeout=30_000,
            )
            typed_text = str(typed.get("text") or typed.get("value") or "") if isinstance(typed, dict) else ""
            if typed_text and sequence not in typed_text:
                raise BrowserBridgeError("GLYCAM input verification did not contain the submitted sequence")
            evidence.append("requested GLYCAM sequence typed into the real input")
            await self._browser_click(tab_id, by="css", selector="#txt_submit", timeout=30_000)
            evidence.append("GLYCAM Submit clicked")
        except Exception as exc:  # noqa: BLE001 - returned as a recoverable action result
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "glycam_browser_action_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "next_action": "retry_glycam_browser_build_with_verified_selectors",
                "glycam_sequence": sequence,
                "sugar_count": sugar_count,
                "target_root": str(target_root),
                "tab_id": tab_id,
                "diagnostic_evidence": {"evidence": evidence, "page": _redact_compat_value(page)},
            }

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if page.get("workflow_status") == "terminal_success":
                break
            await asyncio.sleep(1.5)
            try:
                page = await self._browser_read_page(tab_id, max_chars=24_000, settle_ms=0)
            except Exception as exc:  # bridge may briefly be busy while GLYCAM changes pages
                evidence.append(f"browser read transiently unavailable: {type(exc).__name__}")
                continue
        if page.get("workflow_status") != "terminal_success":
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "glycam_build_timeout_or_error",
                "error": "GLYCAM did not expose its verified completed download page before the bounded wait expired",
                "next_action": "inspect_glycam_page_and_retry",
                "glycam_sequence": sequence,
                "sugar_count": sugar_count,
                "target_root": str(target_root),
                "tab_id": tab_id,
                "diagnostic_evidence": {"evidence": evidence, "page": _redact_compat_value(page)},
            }

        evidence.extend(str(item) for item in page.get("completion_evidence") or [])
        conformer_id = self._glycam_conformer_id(page)
        build_uuid = self._glycam_build_uuid(page)
        download_started = time.time()
        records: list[dict[str, Any]] = []
        try:
            download_button = await self._browser_click(
                tab_id,
                by="css",
                selector=f"#download-all-{conformer_id}",
                timeout=30_000,
            )
            records.extend(_browser_download_records(download_button))
            evidence.append(f"GLYCAM download-all-{conformer_id} clicked")
        except Exception as exc:
            evidence.append(f"download button observation failed: {type(exc).__name__}")

        async def collect_downloads() -> dict[str, Any] | None:
            try:
                listed = await self._browser_list_downloads(100)
                records.extend(_browser_download_records(listed))
            except Exception as exc:
                evidence.append(f"download list transiently unavailable: {type(exc).__name__}")
            return self._fresh_glycam_zip(records, download_started)

        download = await collect_downloads()
        # The conformer control is a dynamic JavaScript-generated download.
        # Some Edge versions report the navigation as a timeout even though
        # the download is successfully committed.  The authoritative check
        # is the fresh browser download record, not the navigation response.
        direct_urls: list[str] = []
        if build_uuid:
            direct_urls.append(f"https://glycam.org/json/download/conformer/{build_uuid}/{conformer_id}/")
            direct_urls.append(f"https://glycam.org/json/download/cb/project/zip/{build_uuid}")
        for direct_url in direct_urls:
            if download:
                break
            try:
                await self._browser_navigate(tab_id, direct_url, 30_000)
            except Exception as exc:
                evidence.append(f"download navigation returned {type(exc).__name__}; checking records")
            await asyncio.sleep(1.0)
            download = await collect_downloads()

        if not download:
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "glycam_download_not_recorded",
                "error": "GLYCAM reached project completion but no fresh downloadable ZIP was recorded",
                "next_action": "recheck_browser_downloads_and_retry_glycam_archive_download",
                "glycam_sequence": sequence,
                "sugar_count": sugar_count,
                "target_root": str(target_root),
                "tab_id": tab_id,
                "build_puuid": build_uuid,
                "conformer_id": conformer_id,
                "downloads": _redact_compat_value(records[-20:]),
                "diagnostic_evidence": {"evidence": evidence, "page": _redact_compat_value(page)},
            }

        source_path = Path(str(download.get("file_path") or download.get("filename") or "")).resolve()
        try:
            members = self._glycam_archive_members(source_path)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", sequence)[:90]
            destination = target_root / f"glycam_browser_{safe_name}_{uuid.uuid4().hex[:8]}.zip"
            shutil.copy2(source_path, destination)
            # Validate the copied artifact as well; the MD runner must never
            # consume a browser record that points at a disappearing file.
            copied_members = self._glycam_archive_members(destination)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "glycam_archive_invalid",
                "error": f"Fresh GLYCAM download could not be validated: {type(exc).__name__}: {exc}",
                "next_action": "download_a_complete_glycam_archive_and_retry",
                "glycam_sequence": sequence,
                "sugar_count": sugar_count,
                "target_root": str(target_root),
                "tab_id": tab_id,
                "build_puuid": build_uuid,
                "conformer_id": conformer_id,
                "download": _redact_compat_value(download),
                "diagnostic_evidence": {"evidence": evidence, "source_path": str(source_path)},
            }

        evidence.append("fresh GLYCAM ZIP download recorded")
        evidence.append("downloaded archive contains PDB, parm7 and rst7 inputs")
        return {
            "success": True,
            "completed": True,
            "terminal": True,
            "user_visible_final": False,
            "do_not_reply": True,
            "workflow_status": "terminal_success",
            "finalize_now": False,
            "internal_stage": True,
            "glycam_sequence": sequence,
            "sequence": sequence,
            "sugar_count": sugar_count,
            "target_root": str(target_root),
            "tab_id": tab_id,
            "build_puuid": build_uuid,
            "conformer_id": conformer_id,
            "browser_download_path": str(source_path),
            "glycan_input": str(destination),
            "download": _redact_compat_value(download),
            "archive_members": copied_members[:80],
            "completion_evidence": evidence,
            "page": _redact_compat_value(page),
        }

    async def _create_task(
        self,
        profile: str,
        payload: dict[str, Any] | None = None,
        _wait_for_terminal: bool = False,
    ) -> dict[str, Any]:
        allowed = {"python_script", "wsl_workflow", "powershell_workflow", "cmd_workflow", "terminal_workflow", "project_build", "md_simulation", "production_md", "workflow"}
        profile = normalize_runner_profile(profile) if profile != "workflow" else profile
        if profile not in allowed:
            raise ValueError(f"Profile must be one of: {', '.join(sorted(allowed))}")
        superseded: list[dict[str, Any]] = []
        if profile in {"md_simulation", "production_md"} and not self.chatgpt_direct:
            # A new MD goal owns the single local WSL/GROMACS slot.  Do not
            # let a previous web-chat turn remain in front of it or let the
            # new turn inherit its running state.  The repository performs a
            # cooperative cancellation for a running task and an immediate
            # terminal cancellation for queued tasks.
            superseded = await self.tasks.supersede_active_profiles(
                {"md_simulation", "production_md"},
                reason="Superseded by a newer MD goal; waiting for the current goal is forbidden.",
            )
        task = await self.tasks.create(profile, payload or {})
        queued = {
            "task_id": task["id"],
            "status": task["status"],
            "profile": task["profile"],
            "created_at": task["created_at"],
            "terminal": False,
            "completed": False,
            "user_visible_final": False,
            "do_not_reply": True,
        }
        if superseded:
            queued["superseded_task_ids"] = [str(item.get("id")) for item in superseded]
        if self.chatgpt_direct:
            queued.pop("do_not_reply", None)
            queued["suggested_next_tool"] = "task_status"
            return queued
        if not _wait_for_terminal:
            return queued
        current = await self._monitor_task(task["id"])
        if current is None:
            return {
                **queued,
                "status": "missing",
                "terminal": False,
                "completed": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "error": "Task disappeared before a terminal result was recorded",
            }
        return {**queued, **current}

    async def _run_task_until_complete(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one legacy runner call as a synchronous, result-bearing operation.

        The original low-level runner tools only returned ``queued``. That made
        a chat model treat a still-running task as a valid stopping point. Keep
        those public tool names for compatibility, but wait here and return the
        terminal task record plus bounded stdout/stderr so one call is useful by
        itself.
        """
        queued = await self._create_task(profile, payload, _wait_for_terminal=False)
        if self.chatgpt_direct:
            return queued
        task_id = queued["task_id"]
        requested_wait = payload.get("wait_timeout") or payload.get("timeout") or self.ctx.settings.default_task_timeout
        try:
            wait_seconds = max(1, min(int(requested_wait), MCP_REQUEST_WAIT_SECONDS))
        except (TypeError, ValueError):
            wait_seconds = MCP_REQUEST_WAIT_SECONDS
        deadline = asyncio.get_running_loop().time() + wait_seconds
        terminal_statuses = {"success", "failed", "cancelled"}
        current: dict[str, Any] | None = None
        while True:
            current = await self.tasks.get(task_id)
            if current is None:
                raise FileNotFoundError(task_id)
            if current["status"] in terminal_statuses:
                break
            if asyncio.get_running_loop().time() >= deadline:
                return {
                    **queued,
                    "status": current["status"],
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "detached": True,
                    "continue_required": True,
                    "next_action": "monitor_task",
                    "message": "本次请求等待窗口已到；Worker 会继续执行。必须继续调用 monitor_task，直到 status 为 success/failed/cancelled；不能把此结果当作任务完成。",
                    "error": current.get("error"),
                    "result": current.get("result"),
                    "output_files": current.get("output_files", []),
                    "logs": current.get("logs", []),
                }
            await asyncio.sleep(0.5)

        # Do not turn every failed process into a final answer.  This shared
        # policy exposes a repair checkpoint for recoverable failures and keeps
        # the actual terminal hand-off behavior for success/irrecoverable error.
        return await self._finished_task_result(profile, payload, current)

    async def _run_python_script(self, script_path: str | None = None, args: list[str] | None = None, cwd: str = ".", timeout: int | None = None, code: str | None = None) -> dict[str, Any]:
        payload = {"script_path": script_path, "args": args or [], "cwd": cwd, "timeout": timeout, "code": code}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await self._run_task_until_complete("python_script", payload)

    async def _run_wsl_workflow(self, workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", distribution: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        if self.chatgpt_direct:
            payload = {"workflow": workflow, "commands": commands, "cwd": cwd, "distribution": distribution, "timeout": timeout}
            return await self._run_task_until_complete("wsl_workflow", {key: value for key, value in payload.items() if value is not None})
        # Compatibility for web conversations that cached the old tool list.
        # A raw ``gmx mdrun`` command carrying an explicit final/production
        # marker must not bypass the maintained recursive ``md.py`` driver.
        # Otherwise a stale ``npt.tpr`` can return zero after only 100 steps
        # and be incorrectly presented as a completed five-minute run.
        production = _production_command_details(workflow, commands)
        if production.get("detected"):
            system = _production_system_from_command(
                production,
                cwd,
                self.ctx.settings.allowed_workspaces,
            )
            if system is None:
                return {
                    "status": "recoverable_error",
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "repair_required": True,
                    "recoverable_error": True,
                    "workflow_status": "repair_required",
                    "error_class": "production_system_not_resolved",
                    "next_action": "discover_environment_and_run_production_md",
                    "error": (
                        "A raw timed gmx mdrun request was detected, but its GLYCAM "
                        "system directory could not be resolved. No raw command was "
                        "started; recursively discover the system and existing md.py "
                        "before retrying."
                    ),
                    "diagnostic_evidence": {
                        "workflow": workflow,
                        "commands": commands or [],
                        "scan_required": True,
                    },
                }
            return await self._run_production_md(
                system_dir=str(system),
                # Keep the parent run root so the production profile can
                # compare this explicit path with newer verified sibling runs
                # instead of treating the old directory as an isolated world.
                target_root=str(system.parent),
                duration_seconds=float(production.get("duration_seconds", 300.0)),
                work_id=str(production.get("work_id", "1")),
                threads=int(production.get("threads", 4)),
                timeout=timeout,
            )
        # A cached client may first issue a read-only listing with a
        # production-shaped workflow name. Do not let that diagnostic return
        # an ordinary success: it neither runs md.py nor proves fresh output,
        # and it is especially dangerous in a long conversation where an old
        # system directory is still visible in prior messages. Returning a
        # repair checkpoint makes the model switch to run_production_md in the
        # same task instead of treating the listing as completion.
        intent = _production_intent_details(workflow, commands)
        if intent.get("detected"):
            return {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "production_diagnostic_only",
                "next_action": "run_production_md",
                "error": (
                    "This WSL action only inspected production inputs; it did not "
                    "run the production driver and cannot satisfy the request. "
                    "Do not reuse a path from an earlier conversation turn. "
                    "Call run_production_md with the exact current GLYCAM system "
                    "directory and a task-unique output prefix."
                ),
                "diagnostic_evidence": {
                    "workflow": workflow,
                    "commands": commands or [],
                    "markers": intent.get("markers", []),
                    "diagnostics_only": True,
                    "fresh_output_required": True,
                },
            }
        payload = {"workflow": workflow, "commands": commands, "cwd": cwd, "distribution": distribution, "timeout": timeout}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await self._run_task_until_complete("wsl_workflow", payload)

    async def _run_powershell_workflow(self, workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
        payload = {"workflow": workflow, "commands": commands, "cwd": cwd, "timeout": timeout}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await self._run_task_until_complete("powershell_workflow", payload)

    async def _run_cmd_workflow(self, workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
        payload = {"workflow": workflow, "commands": commands, "cwd": cwd, "timeout": timeout}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await self._run_task_until_complete("cmd_workflow", payload)

    async def _run_terminal_workflow(self, shell: str = "powershell", workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", distribution: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        payload = {"shell": shell, "workflow": workflow, "commands": commands, "cwd": cwd, "distribution": distribution, "timeout": timeout}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await self._run_task_until_complete("terminal_workflow", payload)

    async def _run_project_build(self, build_type: str = "python", cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
        return await self._run_task_until_complete("project_build", {"build_type": build_type, "cwd": cwd, "timeout": timeout})

    def _autonomous_md_preflight(self, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
        """Reject guessed/stale GLYCAM inputs before starting a real MD run."""

        if not context.get("__requires_md"):
            return None
        if not context.get("__glycam_browser_verified"):
            return {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "glycam_build_not_verified",
                "next_action": "use_browser_to_build_and_verify_requested_glycan",
                "error": (
                    "The GLYCAM browser build has not reached its verified download "
                    "page. Do not run molecular dynamics from a guessed or old "
                    "directory; finish the requested browser build first."
                ),
                "diagnostic_evidence": {
                    "required_markers": ["project completed", "simulation finished normally", "PDB download control"],
                },
            }

        raw_args = args.get("args", []) if isinstance(args, dict) else []
        glycan_dir = args.get("glycan_dir") if isinstance(args, dict) else None
        if not glycan_dir and isinstance(raw_args, list):
            for index, value in enumerate(raw_args):
                token = str(value)
                if token == "--glycan-dir" and index + 1 < len(raw_args):
                    glycan_dir = raw_args[index + 1]
                    break
                if token.startswith("--glycan-dir="):
                    glycan_dir = token.split("=", 1)[1]
                    break
        if not glycan_dir:
            return None

        try:
            directory = resolve_workspace_path(
                str(glycan_dir),
                self.ctx.settings.allowed_workspaces,
                must_exist=False,
            )
        except (OSError, ValueError):
            return None
        is_archive = directory.is_file() and directory.suffix.lower() == ".zip"
        if not directory.is_dir() and not is_archive:
            return None

        started_epoch = float(context.get("__autonomous_started_epoch") or 0.0)
        fresh_downloads = [
            item
            for item in (context.get("__glycam_downloads") or [])
            if str(item.get("state") or "complete").lower() == "complete"
            and (_download_end_epoch(item) or 0.0) >= started_epoch - 2.0
        ]
        if not fresh_downloads:
            return {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "stale_or_non_specific_final_check",
                "next_action": "download_fresh_glycam_artifact_and_retry",
                "error": (
                    "No completed GLYCAM download was recorded during this task. "
                    "The candidate directory may be an old run; download the "
                    "current browser build and use that artifact before MD."
                ),
                "diagnostic_evidence": {
                    "glycan_dir": str(directory),
                    "started_epoch": started_epoch,
                    "download_records_seen": len(context.get("__glycam_downloads") or []),
                },
            }

        # A fresh download record by itself is not enough: a model can still
        # pass the path from an older turn while a different new archive is
        # present in the download list.  The server-owned browser build stores
        # the exact archive it copied for this task.  Bind the MD input to
        # that path and reject any old directory/archive explicitly.
        bound_input = str(context.get("__glycam_input") or "").strip()
        if bound_input:
            try:
                bound_path = resolve_workspace_path(bound_input, self.ctx.settings.allowed_workspaces, must_exist=True)
                requested_path = directory.resolve()
            except (OSError, ValueError):
                bound_path = None
                requested_path = directory
            if bound_path is None or requested_path != bound_path.resolve():
                return {
                    "status": "recoverable_error",
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "repair_required": True,
                    "recoverable_error": True,
                    "workflow_status": "repair_required",
                    "error_class": "stale_or_non_specific_final_check",
                    "next_action": "use_the_current_browser_download_and_retry",
                    "error": (
                        "The MD action selected an artifact different from the exact "
                        "GLYCAM archive produced in this task. An older residual path "
                        "is not an acceptable input; retry with the server-bound "
                        "current browser download."
                    ),
                    "diagnostic_evidence": {
                        "requested_glycan_input": str(directory),
                        "task_bound_glycan_input": bound_input,
                        "fresh_download_records": len(fresh_downloads),
                    },
                }

        expected = _requested_sugar_count(str(context.get("goal") or ""))
        if expected is None:
            return None

        sequence: str | None = str(context.get("__glycam_sequence") or "").strip() or None
        metadata_path: Path | None = None
        candidates = [directory / "info.json", directory / "response.json"] if directory.is_dir() else []
        if directory.is_dir():
            try:
                candidates.extend(list(directory.rglob("info.json"))[:10])
                candidates.extend(list(directory.rglob("response.json"))[:10])
            except OSError:
                pass
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError, UnicodeError):
                continue
            sequence = _metadata_sequence(data)
            if sequence:
                metadata_path = candidate
                break

        if not sequence:
            return {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "input_identity_or_cardinality",
                "next_action": "inspect_glycam_metadata_and_retry",
                "error": (
                    "The candidate glycan directory has no readable GLYCAM sequence "
                    "metadata, so its residue count cannot be verified. Obtain the "
                    "actual browser-generated download and retry."
                ),
                "diagnostic_evidence": {"glycan_dir": str(directory), "expected_sugars": expected},
            }

        actual = _glycam_sequence_count(sequence)
        if actual != expected:
            return {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "input_identity_or_cardinality",
                "next_action": "build_requested_sugar_count_and_retry",
                "error": f"GLYCAM sequence contains {actual} sugars but the goal requires {expected}.",
                "diagnostic_evidence": {
                    "glycan_dir": str(directory),
                    "metadata_path": str(metadata_path) if metadata_path else None,
                    "sequence": sequence,
                    "expected_sugars": expected,
                    "actual_sugars": actual,
                },
            }
        return None

    async def _run_md_simulation(
        self,
        script_path: str | None = None,
        args: list[str] | None = None,
        cwd: str = ".",
        timeout: int | None = None,
        require_production: bool = True,
        production_duration_seconds: float = 300,
    ) -> dict[str, Any]:
        # Never start AUTO_file/main.py with an empty or partial invocation.
        # Older web-chat turns sometimes react to a failed tool call by
        # calling the compatibility runner again without carrying forward the
        # GLYCAM archive and target root.  Letting that reach the Worker only
        # creates a misleading argparse task and consumes another recovery
        # cycle.  Return a hard nonterminal checkpoint with the exact missing
        # contract instead; the web agent must inspect the current goal and
        # retry with the current browser artifact.
        raw_args = [str(item) for item in (args or [])]

        def option_value(option: str) -> str | None:
            for index, value in enumerate(raw_args):
                if value == option and index + 1 < len(raw_args):
                    candidate = raw_args[index + 1].strip()
                    return candidate or None
                prefix = option + "="
                if value.startswith(prefix):
                    candidate = value[len(prefix):].strip()
                    return candidate or None
            return None

        missing = [option for option in ("--glycan-dir", "--target-root") if not option_value(option)]
        if missing:
            if self.chatgpt_direct:
                raise ValueError("run_md_simulation requires " + ", ".join(missing))
            return {
                "status": "recoverable_error",
                "profile": "md_simulation",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "final_answer_allowed": False,
                "continue_required": True,
                "repair_required": True,
                "recoverable_error": True,
                "self_check_required": True,
                "web_search_required": True,
                "workflow_status": "repair_required",
                "error_class": "missing_required_arguments",
                "next_action": "inspect_current_goal_and_retry_md_with_current_glycam_input",
                "error": "run_md_simulation was not started because required arguments are missing: " + ", ".join(missing),
                "repair_hint": (
                    "Read the current user goal and the latest browser/build evidence, "
                    "recursively locate the maintained AUTO_file/main.py if needed, "
                    "then retry with the current browser-generated GLYCAM archive and "
                    "both --glycan-dir and --target-root. Do not repeat an empty call "
                    "and do not report completion."
                ),
                "diagnostic_evidence": {
                    "script_path": script_path,
                    "args": raw_args,
                    "cwd": cwd,
                    "missing": missing,
                },
            }
        payload = {
            "script_path": script_path,
            "args": raw_args,
            "cwd": cwd,
            "timeout": timeout,
            # Keep this contract in the task payload so the answer-boundary
            # guard survives a long Worker run and a later monitor/rebind.
            "require_production": bool(require_production),
            "production_duration_seconds": production_duration_seconds,
        }
        return await self._run_task_until_complete("md_simulation", payload)

    async def _run_production_md(
        self,
        system_dir: str | None = None,
        target_root: str = ".",
        duration_seconds: float = 300,
        work_id: str = "1",
        script_path: str | None = None,
        mdp: str | None = None,
        gpu: str | None = None,
        threads: int = 4,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "system_dir": system_dir,
            "target_root": target_root,
            "duration_seconds": duration_seconds,
            "work_id": work_id,
            "script_path": script_path,
            "mdp": mdp,
            "gpu": gpu,
            "threads": threads,
            "timeout": timeout,
        }
        return await self._run_task_until_complete("production_md", {key: value for key, value in payload.items() if value is not None})

    async def _run_workflow_until_complete(
        self,
        steps: list[dict[str, Any]],
        final_checks: list[dict[str, Any]] | None = None,
        max_attempts: int = 1,
        overall_timeout: int | None = None,
        wait_timeout: int | None = None,
        poll_interval_ms: int = 500,
        fresh_outputs: bool = True,
    ) -> dict[str, Any]:
        """Create one persistent workflow task and wait for its terminal state.

        The worker owns all step execution. If the MCP request disconnects after
        the task is created, the worker still finishes the workflow and records
        the result for ``monitor_task``.
        """
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list")

        # GLYCAM/GROMACS/EM/NVT/NPT work has a dedicated runner. Refuse a
        # generic workflow before it is queued so a diagnostics-only step or a
        # stale-directory final check can never masquerade as the requested MD
        # task. The MCP layer turns this into a real nonterminal tool error;
        # the web model must call run_md_simulation and may not answer yet.
        is_md_workflow, md_markers = _looks_like_md_workflow(steps, final_checks)
        if is_md_workflow:
            return {
                "status": "blocked",
                "profile": "workflow",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "continue_required": True,
                "repair_required": True,
                "recoverable_error": True,
                "error_class": "wrong_runner_for_md",
                "next_action": "run_md_simulation",
                "message": (
                    "This request contains a GLYCAM/GROMACS molecular-dynamics "
                    "workflow, so the generic workflow runner was not queued. "
                    "No task was started and this is not a user-facing result."
                ),
                "repair_hint": (
                    "Call run_md_simulation now. First use the browser-produced "
                    "GLYCAM directory and verify its requested residue count; "
                    "then pass the maintained AUTO_file/main.py entry point with "
                    "explicit --glycan-dir and --target-root arguments. Let that "
                    "runner perform the bounded EM self-healing and continue to "
                    "NVT and NPT. Do not call run_workflow_until_complete, "
                    "run_wsl_workflow, or a diagnostics-only substitute for this "
                    "request, and do not answer until the specialized runner is "
                    "terminal and its artifacts are verified."
                ),
                "diagnostic_evidence": {
                    "generic_workflow_rejected": True,
                    "markers": md_markers,
                    "step_count": len(steps),
                    "final_check_count": len(final_checks or []),
                },
            }
        try:
            normalized_attempts = int(max_attempts)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_attempts must be an integer") from exc
        if not 1 <= normalized_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if overall_timeout is None:
            normalized_overall_timeout = self.ctx.settings.default_task_timeout
        else:
            try:
                normalized_overall_timeout = int(overall_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("overall_timeout must be an integer") from exc
        normalized_overall_timeout = max(1, min(normalized_overall_timeout, 24 * 60 * 60))
        if wait_timeout is None:
            normalized_wait_timeout = normalized_overall_timeout
        else:
            try:
                normalized_wait_timeout = int(wait_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("wait_timeout must be an integer") from exc
            normalized_wait_timeout = max(1, min(normalized_wait_timeout, MCP_REQUEST_WAIT_SECONDS))
        # Keep the HTTP/MCP request below the upstream proxy lifetime. The
        # Worker still honors the independent overall_timeout in the queued
        # workflow payload.
        normalized_wait_timeout = min(normalized_wait_timeout, MCP_REQUEST_WAIT_SECONDS)
        try:
            normalized_poll_ms = int(poll_interval_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("poll_interval_ms must be an integer") from exc
        normalized_poll_ms = max(200, min(normalized_poll_ms, 5_000))

        workflow_payload = {
            "steps": steps,
            "final_checks": final_checks or [],
            "max_attempts": normalized_attempts,
            "overall_timeout": normalized_overall_timeout,
            "fresh_outputs": bool(fresh_outputs),
        }
        if self.chatgpt_direct:
            from web_tools.runner import ProfileRunner

            # Fail malformed explicit batches before creating a persistent task.
            ProfileRunner(self.ctx.settings)._normalize_workflow(workflow_payload)
        task = await self._create_task("workflow", workflow_payload, _wait_for_terminal=False)
        if self.chatgpt_direct:
            return task
        task_id = task["task_id"]
        deadline = asyncio.get_running_loop().time() + normalized_wait_timeout
        terminal_statuses = {"success", "failed", "cancelled"}
        while True:
            current = await self.tasks.get(task_id)
            if current is None:
                raise FileNotFoundError(task_id)
            if current["status"] in terminal_statuses:
                return await self._finished_task_result("workflow", workflow_payload, current)
            if asyncio.get_running_loop().time() >= deadline:
                return {
                    "task_id": task_id,
                    "status": current["status"],
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "detached": True,
                    "continue_required": True,
                    "next_action": "monitor_task",
                    "message": "本次请求等待窗口已到，但 Worker 会继续执行。必须继续调用 monitor_task，直到 status 为 success/failed/cancelled；不能把此结果当作任务完成。",
                    "error": current.get("error"),
                    "result": current.get("result"),
                    "output_files": current.get("output_files", []),
                    "logs": current.get("logs", []),
                }
            await asyncio.sleep(normalized_poll_ms / 1000)

    async def _autonomous_sample(
        self,
        goal: str,
        transcript: list[dict[str, Any]],
        ctx: Any,
        workflow: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Ask the connected ChatGPT client for one next supervisor action.

        This is MCP server-side sampling: it is a request to the model that is
        already connected to this MCP session, not a second conversation and
        not a new API conversation.  Keeping the loop here means tool results,
        error evidence and repairs stay inside the original web ChatGPT turn.
        """

        if self.chatgpt_direct:
            return None, "Sampling is disabled in chatgpt_direct mode; the host chooses the next tool."
        session = getattr(ctx, "session", None) if ctx is not None else None
        create_message = getattr(session, "create_message", None)
        if not callable(create_message):
            return None, "The connected MCP client does not expose sampling/createMessage"

        visible_transcript = _redact_compat_value(transcript)
        encoded_transcript = json.dumps(visible_transcript, ensure_ascii=False, default=str)
        if len(encoded_transcript) > AUTONOMOUS_TRANSCRIPT_CHARS:
            encoded_transcript = encoded_transcript[-AUTONOMOUS_TRANSCRIPT_CHARS:]
        md_goal, md_markers = _autonomous_goal_requires_md(goal, workflow, transcript)
        production_goal, production_markers, production_duration = _autonomous_goal_requires_production(
            goal, workflow, transcript
        )
        md_supervision_rule = ""
        if md_goal:
            md_supervision_rule = (
                "\n\nMANDATORY GLYCAM/MD GATE (enforced by the server): this goal is a "
                "GLYCAM/GROMACS molecular-dynamics request (markers: "
                + ", ".join(md_markers)
                + "). A missing or guessed glycan directory is a failed input, "
                "not a completed build. Use the browser actions to create and "
                "verify the requested sugar count and obtain the real downloaded "
                "artifact, inspect the returned download/path, then retry "
                "md_simulation with that actual directory. The supervisor will "
                "reject done until md_simulation itself returns terminal_success "
                "with completed=true; do not return done after self_check, search, "
                "or a directory listing."
            )
        system_prompt = (
            "You are the execution supervisor inside an MCP tool call. You are not "
            "writing the user-facing answer yet. Drive the goal to verified completion. "
            "Return JSON only, with no Markdown and no prose. Each response must be "
            "either {\"action\":{\"name\":\"...\",\"arguments\":{...}}} for exactly "
            "one next action, or {\"done\":true,\"answer\":\"...\"} only when the "
            "goal is actually verified. When an action fails, diagnose it from the "
            "evidence, use the server-injected self_check/search evidence and search_web "
            "when needed, inspect the real source or page, apply the smallest safe repair, "
            "and retry with changed arguments. The server preserves a recovery latch and "
            "may inject a diagnosis action automatically; do not skip it or repeat an "
            "identical failed action. "
            "Never say 'continue', never ask the user to click anything, and never mark "
            "a page merely opened or a task merely queued as done. Do not invent paths, "
            "selectors, downloads, or success. Before returning done, include an "
            "acceptance array with one or more concrete checks derived from the goal "
            "(for example file_exists/file_contains/result_success/task_terminal_success/"
            "page_text_contains or result_field_equals). Omit acceptance only when the "
            "latest action itself returned machine-readable terminal_success. The MCP "
            "executor will perform the action and send its result back on the next cycle."
            + md_supervision_rule
            + (
                "\n\nMANDATORY FINAL/PRODUCTION STAGE GATE (enforced by the server): the goal "
                "explicitly requests a final or timed production stage (markers: "
                + ", ".join(production_markers)
                + f", requested_seconds={production_duration:g}). The EM/NVT/NPT setup "
                "is only an intermediate stage. Do not return done after setup; call "
                "run_production_md with the exact system directory from the successful "
                "current setup, wait for its terminal result, and verify fresh production "
                "artifacts before done."
                if production_goal
                else ""
            )
            + "\n\n"
            "Allowed action names:\n"
            + ", ".join(sorted(AUTONOMOUS_ACTIONS))
        )
        prompt = (
            "Goal:\n"
            + str(goal).strip()
            + "\n\nExecution transcript (latest evidence is last):\n"
            + encoded_transcript
            + "\n\nChoose the single next action or verified completion now."
        )
        # Do not rely only on the presence of ``create_message``.  A client
        # may expose the method on every ServerSession while omitting the MCP
        # sampling capability, in which case send_request would wait forever.
        client_params = getattr(session, "_client_params", None)
        capabilities = getattr(client_params, "capabilities", None)
        if capabilities is not None and getattr(capabilities, "sampling", None) is None:
            return None, "The connected MCP client does not advertise sampling/createMessage"

        try:
            request_id = getattr(ctx, "request_id", None)
            response = await asyncio.wait_for(
                create_message(
                    [
                        mcp_types.SamplingMessage(
                            role="user",
                            content=mcp_types.TextContent(type="text", text=prompt),
                        )
                    ],
                    max_tokens=1_600,
                    system_prompt=system_prompt,
                    include_context="none",
                    temperature=0.1,
                    related_request_id=request_id,
                ),
                timeout=AUTONOMOUS_SAMPLING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None, (
                "sampling/createMessage timed out after "
                f"{AUTONOMOUS_SAMPLING_TIMEOUT_SECONDS}s; the connected client did not return a decision"
            )
        except Exception as exc:  # pragma: no cover - client capability varies
            return None, f"sampling/createMessage failed: {type(exc).__name__}: {exc}"
        response_text = _sampling_text(response)
        decision = _sampling_decision(response_text)
        if decision is None:
            return None, "sampling/createMessage returned no valid JSON decision"
        decision["sampling_text"] = response_text[:8_000]
        return decision, None

    @staticmethod
    def _autonomous_initial_steps(workflow: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(workflow, dict):
            return []
        raw_steps = workflow.get("steps", workflow.get("actions", []))
        if isinstance(raw_steps, dict):
            raw_steps = list(raw_steps.values())
        if not isinstance(raw_steps, list):
            return []
        return [dict(item) for item in raw_steps if isinstance(item, dict)]

    async def _autonomous_execute_action(
        self,
        raw_step: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Execute one action without returning control to the MCP client."""

        action_value = raw_step.get("action", raw_step.get("kind", raw_step.get("name", raw_step.get("tool", ""))))
        if isinstance(action_value, dict):
            nested = action_value
            action_value = nested.get("name", nested.get("tool", nested.get("action", "")))
            raw_args = nested.get("arguments", nested.get("args", raw_step.get("args", {})))
        else:
            raw_args = raw_step.get("args", raw_step.get("arguments", {}))
        action = _compat_action(action_value)
        if not action and raw_step.get("profile"):
            action = "__profile_step__"
            raw_args = {"step": raw_step}
        if action not in AUTONOMOUS_ACTIONS and action != "__profile_step__":
            raise ValueError(
                f"Unsupported autonomous action {action!r}; choose one of: "
                + ", ".join(sorted(AUTONOMOUS_ACTIONS))
            )
        args = _resolve_compat_value(dict(raw_args) if isinstance(raw_args, dict) else {}, context)
        # A model that is stuck can otherwise keep issuing the exact same
        # failing call until the cycle limit.  Allow one retry for transient
        # failures, but after the second identical attempt force a changed
        # action/argument or a new diagnosis.  Polling/read-only waits are
        # intentionally exempt because their arguments are commonly stable.
        fingerprint = _autonomous_action_fingerprint(action, args)
        attempts = context.setdefault("__action_attempts", {})
        previous_attempts = int(attempts.get(fingerprint, 0) or 0)
        if (
            bool(context.get("__recovery_pending"))
            and previous_attempts >= 2
            and action not in _AUTONOMOUS_ROOT_DIAGNOSIS_ACTIONS
            and action not in _AUTONOMOUS_NON_BLOCKING_ACTIONS
        ):
            return action, args, {
                "status": "failed",
                "terminal": False,
                "completed": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "repeated_identical_action",
                "error": (
                    f"The identical action {action!r} was already attempted "
                    "twice while the workflow still required recovery. "
                    "Do not repeat it unchanged; inspect the latest evidence, "
                    "search/diagnose if needed, and choose a changed action or arguments."
                ),
                "diagnostic_evidence": {"action": action, "fingerprint": fingerprint, "attempts": previous_attempts},
                "next_action": "choose_changed_action_after_diagnosis",
            }
        attempts[fingerprint] = previous_attempts + 1
        if action == "md_simulation":
            preflight = self._autonomous_md_preflight(args, context)
            if preflight is not None:
                return action, args, preflight
        if action == "production_md" and context.get("__requires_md") and not context.get("__md_verified"):
            return action, args, {
                "status": "recoverable_error",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "production_before_md",
                "next_action": "run_md_simulation_before_production_md",
                "error": (
                    "The requested production stage cannot start before the current "
                    "GLYCAM setup has returned verified EM/NVT/NPT terminal success."
                ),
            }
        if action == "__profile_step__":
            step = dict(args.get("step") or {})
            profile = str(step.get("profile") or "").strip()
            if not profile:
                raise ValueError("A profile workflow step requires profile")
            result = await self._run_workflow_until_complete(
                [step],
                [],
                max_attempts=max(1, min(int(step.get("retries", 0)) + 1, 3)),
                overall_timeout=max(1, int(step.get("timeout", self.ctx.settings.default_task_timeout))),
                wait_timeout=max(1, min(int(step.get("timeout", MCP_REQUEST_WAIT_SECONDS)), MCP_REQUEST_WAIT_SECONDS)),
            )
            return profile, step, result
        if action == "browser_build_glycam":
            result = await self._browser_build_glycam(str(context.get("goal") or ""), args)
        elif action == "browser_open_read":
            started = await self._browser_start(args.get("url"))
            tabs = started.get("tabs") if isinstance(started, dict) else []
            tab_id = args.get("tab_id") or (tabs[0].get("tab_id") if tabs and isinstance(tabs[0], dict) else None)
            if not tab_id:
                raise ValueError("browser_open_read did not return a tab_id")
            page = await self._browser_read(str(tab_id), int(args.get("max_chars", 20_000)))
            result = {"opened": True, "tab_id": str(tab_id), "started": started, "page": page}
        elif action == "browser_start":
            result = await self._browser_start(args.get("url"))
        elif action == "browser_list_tabs":
            result = await self._browser_list_tabs()
        elif action == "browser_list_downloads":
            result = await self._browser_list_downloads(int(args.get("limit", 20)))
        elif action == "browser_read":
            result = await self._browser_read(args.get("tab_id"), int(args.get("max_chars", 20_000)))
        elif action == "browser_navigate":
            result = await self._browser_navigate(str(args["tab_id"]), str(args["url"]), int(args.get("timeout", args.get("timeout_ms", 30_000))))
        elif action == "browser_click":
            result = await self._browser_click(
                str(args["tab_id"]),
                args.get("by"),
                args.get("selector"),
                args.get("name"),
                bool(args.get("exact", False)),
                int(args.get("timeout", args.get("timeout_ms", 15_000))),
            )
        elif action == "browser_type":
            result = await self._browser_type(
                str(args["tab_id"]),
                str(args.get("text", "")),
                args.get("by"),
                args.get("selector"),
                args.get("name"),
                bool(args.get("exact", False)),
                bool(args.get("clear", True)),
                int(args.get("timeout", args.get("timeout_ms", 15_000))),
            )
        elif action == "browser_press":
            result = await self._browser_press(
                str(args["tab_id"]),
                str(args["key"]),
                args.get("by"),
                args.get("selector"),
                args.get("name"),
                bool(args.get("exact", False)),
                int(args.get("timeout", args.get("timeout_ms", 15_000))),
            )
        elif action == "browser_select_option":
            result = await self._browser_select_option(
                str(args["tab_id"]),
                str(args["option"]),
                str(args.get("option_by", "label")),
                args.get("by"),
                args.get("selector"),
                args.get("name"),
                bool(args.get("exact", False)),
                int(args.get("timeout", args.get("timeout_ms", 15_000))),
            )
        elif action == "browser_wait":
            result = await self._browser_wait(str(args["tab_id"]), int(args.get("milliseconds", 1_000)))
        elif action == "browser_wait_until":
            result = await self._compat_execute_action(action, args)
        elif action == "browser_screenshot":
            result = await self._browser_screenshot(str(args["tab_id"]), bool(args.get("full_page", False)))
        elif action == "browser_close_tab":
            result = await self._browser_close_tab(str(args["tab_id"]))
        elif action == "create_directory":
            result = await self._create_directory(
                str(args["path"]), bool(args.get("parents", True)), bool(args.get("exist_ok", False))
            )
        elif action == "copy_path":
            result = await self._copy_path(str(args["source"]), str(args["destination"]), bool(args.get("overwrite", False)))
        elif action == "move_path":
            result = await self._move_path(str(args["source"]), str(args["destination"]), bool(args.get("overwrite", False)))
        elif action == "list_directory":
            result = await self._list_directory(str(args.get("path", ".")), bool(args.get("recursive", False)), int(args.get("limit", 500)))
        elif action == "get_path_info":
            result = await self._get_path_info(str(args["path"]))
        elif action == "search_file":
            result = await self._search_file(
                str(args["query"]), str(args.get("path", ".")), bool(args.get("case_sensitive", False)),
                int(args.get("max_results", 200)), bool(args.get("include_paths", True)), int(args.get("max_depth", 32)),
            )
        elif action == "find_paths":
            result = await self._find_paths(
                str(args["query"]), str(args.get("path", ".")), str(args.get("kind", "any")),
                bool(args.get("case_sensitive", False)), int(args.get("max_results", 200)), int(args.get("max_depth", 32)),
            )
        elif action == "inspect_project":
            result = await self._inspect_project(str(args.get("path", ".")))
        elif action == "discover_environment":
            result = await self._discover_environment(
                request=str(args.get("request", "")), cwd=args.get("cwd"), distribution=args.get("distribution"),
                roots=args.get("roots"), max_results=int(args.get("max_results", 120)), read_limit=int(args.get("read_limit", 6_000)),
                max_depth=int(args.get("max_depth", 32)),
            )
        elif action == "read_file":
            result = await self._read_file(str(args["path"]), args.get("start_line"), args.get("end_line"))
        elif action == "write_file":
            result = await self._write_file(str(args["path"]), str(args.get("content", "")), bool(args.get("create_dirs", args.get("create_parents", True))))
        elif action == "patch_file":
            result = await self._patch_file(str(args["path"]), str(args["find"]), str(args.get("replace", "")), int(args.get("expected_count", args.get("expected_replacements", 1))))
        elif action == "modify_project":
            result = await self._modify_project(str(args.get("path", ".")), args.get("files"), args.get("changes"), bool(args.get("create_dirs", True)))
        elif action == "self_check":
            result = await self._self_check(
                operation=str(args.get("operation", "autonomous_workflow")), task_id=args.get("task_id"),
                error_text=str(args.get("error_text", "")), expected_paths=args.get("expected_paths"),
                browser_check=bool(args.get("browser_check", False)), auto_search=bool(args.get("auto_search", True)),
                search_domains=args.get("search_domains"), search_max_results=int(args.get("search_max_results", 5)),
            )
        elif action == "search_web":
            result = await self._search_web(str(args["query"]), int(args.get("max_results", 5)), args.get("domains"), int(args.get("timeout_seconds", 20)))
        elif action == "monitor_task":
            result = await self._monitor_task(
                str(args["task_id"]), int(args.get("timeout_seconds", MCP_REQUEST_WAIT_SECONDS)), float(args.get("poll_interval_seconds", 0.5))
            )
        elif action == "wait_task":
            result = await self._wait_task(
                str(args["task_id"]), int(args.get("timeout_seconds", 90)), float(args.get("poll_interval_seconds", 1.0))
            )
        elif action == "read_task_output":
            result = await self._read_task_output(
                str(args["task_id"]), str(args.get("stream", "stdout")), int(args.get("max_chars", 200_000))
            )
        elif action == "retry_task":
            result = await self._retry_task(str(args["task_id"]), bool(args.get("confirm", True)))
        elif action == "python":
            result = await self._run_python_script(
                script_path=args.get("script_path"), args=args.get("args") or [], cwd=str(args.get("cwd", ".")),
                timeout=args.get("timeout"), code=args.get("code"),
            )
        elif action == "powershell":
            result = await self._run_powershell_workflow(
                workflow=str(args.get("workflow", "custom")), commands=args.get("commands") or [str(args.get("command", args.get("code", "")))],
                cwd=str(args.get("cwd", ".")), timeout=args.get("timeout"),
            )
        elif action == "cmd":
            result = await self._run_cmd_workflow(
                workflow=str(args.get("workflow", "custom")), commands=args.get("commands") or [str(args.get("command", args.get("code", "")))],
                cwd=str(args.get("cwd", ".")), timeout=args.get("timeout"),
            )
        elif action == "terminal":
            result = await self._run_terminal_workflow(
                shell=str(args.get("shell", "powershell")), workflow=str(args.get("workflow", "custom")),
                commands=args.get("commands") or [str(args.get("command", args.get("code", "")))], cwd=str(args.get("cwd", ".")),
                distribution=args.get("distribution"), timeout=args.get("timeout"),
            )
        elif action == "wsl":
            result = await self._run_wsl_workflow(
                workflow=str(args.get("workflow", "custom")), commands=args.get("commands") or [str(args.get("command", args.get("code", "")))],
                cwd=str(args.get("cwd", ".")), distribution=args.get("distribution"), timeout=args.get("timeout"),
            )
        elif action == "project_build":
            result = await self._run_project_build(str(args.get("build_type", "python")), str(args.get("cwd", ".")), args.get("timeout"))
        elif action == "md_simulation":
            result = await self._run_md_simulation(
                args.get("script_path"),
                args.get("args") or [],
                str(args.get("cwd", ".")),
                args.get("timeout"),
                bool(args.get("require_production", True)),
                float(args.get("production_duration_seconds", 300)),
            )
        elif action == "production_md":
            result = await self._run_production_md(
                system_dir=args.get("system_dir") or args.get("system_root"),
                target_root=str(args.get("target_root", args.get("cwd", "."))),
                duration_seconds=float(args.get("duration_seconds", 300)),
                work_id=str(args.get("work_id", "1")),
                script_path=args.get("script_path") or args.get("production_script"),
                mdp=args.get("mdp"),
                gpu=args.get("gpu"),
                threads=int(args.get("threads", args.get("ntomp", 4))),
                timeout=args.get("timeout"),
            )
        elif action == "run_workflow_until_complete":
            result = await self._run_workflow_until_complete(
                args.get("steps") or [], args.get("final_checks"), int(args.get("max_attempts", 1)),
                args.get("overall_timeout"), args.get("wait_timeout"), int(args.get("poll_interval_ms", 500)), bool(args.get("fresh_outputs", True)),
            )
        elif action == "sleep":
            seconds = max(0.0, min(float(args.get("seconds", 1.0)), 120.0))
            await asyncio.sleep(seconds)
            result = {"slept_seconds": seconds}
        else:  # pragma: no cover - whitelist above is exhaustive
            raise ValueError(f"Unsupported autonomous action: {action}")

        # A Worker can outlive the per-poll MCP wait window.  Waiting for the
        # first window and then handing back ``detached`` was another hidden
        # continuation boundary: the web model had to remember to monitor the
        # task.  Consume that wait internally while the one-shot supervisor
        # still has time left.  Only an actual supervisor timeout is allowed
        # to escape as a terminal timeout diagnostic.
        if isinstance(result, dict) and result.get("detached") and result.get("task_id"):
            autonomous_deadline = float(context.get("__autonomous_deadline", time.monotonic() + MCP_REQUEST_WAIT_SECONDS))
            while result.get("detached") and time.monotonic() < autonomous_deadline:
                remaining = max(1, int(autonomous_deadline - time.monotonic()))
                result = await self._monitor_task(
                    str(result["task_id"]),
                    timeout_seconds=min(MCP_REQUEST_WAIT_SECONDS, remaining),
                    poll_interval_seconds=0.5,
                ) or result
            if isinstance(result, dict) and result.get("detached"):
                result = {
                    **result,
                    "autonomous_wait_exhausted": True,
                    "error": result.get("error") or "The autonomous workflow deadline was reached while waiting for the Worker",
                }
        return action, args, result if isinstance(result, dict) else {"result": result}

    async def _run_direct_workflow(
        self,
        workflow: dict[str, Any] | None,
        acceptance: list[dict[str, Any]] | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Execute a submitted finite batch without inventing any actions.

        Runner-only batches use the durable existing Worker. Short tool-only
        batches stay bounded to one request. Mixed batches must be split by
        the host so an asynchronous runner cannot silently skip later steps.
        """
        steps = workflow.get("steps") if isinstance(workflow, dict) else None
        if not isinstance(steps, list) or not steps:
            return {"status": "needs_chatgpt_planning", "terminal": False, "completed": False,
                    "message": "ChatGPT must choose concrete tool calls or supply a finite workflow.steps list. No task was started.",
                    "suggested_next_tool": "goal_start"}
        if len(steps) > 48 or any(not isinstance(step, dict) for step in steps):
            raise ValueError("workflow.steps must contain 1 to 48 step objects")
        checks = acceptance if acceptance is not None else workflow.get("acceptance", workflow.get("acceptance_checks", workflow.get("final_checks", [])))
        checks = _normalize_acceptance_specs(checks)
        if any(not isinstance(check, dict) for check in checks):
            raise ValueError("acceptance checks must be objects")
        profiles = {"python": "python_script", "powershell": "powershell_workflow", "cmd": "cmd_workflow",
                    "terminal": "terminal_workflow", "wsl": "wsl_workflow", "project_build": "project_build",
                    "md_simulation": "md_simulation", "production_md": "production_md"}
        safe_tools = {"list_directory", "get_path_info", "create_directory", "read_file", "write_file", "patch_file",
                      "copy_path", "move_path", "delete_path", "search_file", "find_paths", "inspect_project"}
        safe_tools.update(name for name in self.specs if name.startswith("browser_"))
        tool_aliases = {"browser_read": "browser_read_page"}
        planned: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        runner_steps: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if any(key in step for key in ("on_error", "repair", "recovery_steps")):
                raise ValueError("Direct batches do not infer repair branches; submit any repair as an explicit next tool call")
            if step.get("profile"):
                profile = normalize_runner_profile(str(step["profile"]))
                if profile not in set(profiles.values()):
                    raise ValueError(f"Unsupported direct runner profile: {profile}")
                payload = step.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("Runner payload must be an object")
                runner_steps.append({"profile": profile, "payload": payload, "label": str(step.get("label", step.get("id", f"step-{index + 1}"))), "retries": step.get("retries", 0)})
                continue
            action = _compat_action(step.get("action", step.get("kind", step.get("name", step.get("tool", "")))))
            args = _compat_args(step)
            for metadata_key in ("kind", "name", "tool"):
                args.pop(metadata_key, None)
            if action in profiles:
                payload = dict(args)
                if action in {"powershell", "cmd", "terminal", "wsl"}:
                    if "commands" not in payload and ("command" in payload or "code" in payload):
                        payload["commands"] = [str(payload.pop("command", payload.pop("code", "")))]
                        payload.setdefault("workflow", "custom")
                if action == "python" and "script" in payload:
                    payload.setdefault("code", payload.pop("script"))
                runner_steps.append({"profile": profiles[action], "payload": payload,
                                     "label": str(step.get("label", step.get("id", f"step-{index + 1}"))), "retries": step.get("retries", 0)})
                continue
            tool = tool_aliases.get(action, action)
            if tool not in safe_tools:
                raise ValueError(f"Unsupported direct batch action: {action}; call its dedicated tool explicitly")
            inspect.signature(getattr(self, f"_{tool}")).bind(**args)
            planned.append((tool, args, step))
        if runner_steps:
            if planned:
                raise ValueError("Split mixed runner/tool batches into host tool calls; no operation was started")
            return await self._run_workflow_until_complete(runner_steps, checks, overall_timeout=timeout_seconds)
        records: list[dict[str, Any]] = []
        context: dict[str, Any] = {"steps": {}, "last": {}}
        deadline = asyncio.get_running_loop().time() + max(1, min(int(timeout_seconds), 30))
        for index, (tool, args, step) in enumerate(planned):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {"status": "failed", "terminal": True, "completed": False, "error_code": "batch_timeout",
                        "error": "Direct batch exceeded its 30 second request budget; inspect completed steps before retrying.", "steps": records}
            step_id = str(step.get("id", step.get("label", f"step-{index + 1}")))
            try:
                resolved = _resolve_compat_value(args, context)
                result = await asyncio.wait_for(self.invoke(tool, resolved), timeout=remaining)
                records.append({"id": step_id, "action": tool, "result": result})
                context["steps"][step_id] = result
                context["last"] = result
                if isinstance(result, dict) and (result.get("status") == "failed" or result.get("error")):
                    return {"status": "failed", "terminal": True, "completed": False, "steps": records,
                            "error": result.get("error"), "error_code": result.get("error_code", "batch_step_failed")}
            except Exception as exc:
                return {"status": "failed", "terminal": True, "completed": False, "steps": records,
                        "failed_step": step_id, "error_code": "batch_step_failed", "error": _redact_compat_text(exc),
                        "message": "Inspect recorded state before retrying a side effect; no subsequent step was executed."}
        checked = [await self._compat_check(_resolve_compat_value(check, context), context) for check in checks]
        passed = all(check.get("passed") for check in checked)
        return {"status": "success" if passed else "failed", "terminal": True, "completed": passed,
                "steps": records, "acceptance": checked, "evidence": {"checks": checked},
                "error": None if passed else "Submitted batch acceptance failed"}

    async def _run_autonomous_workflow(
        self,
        goal: str,
        workflow: dict[str, Any] | None = None,
        acceptance: list[dict[str, Any]] | None = None,
        max_cycles: int = AUTONOMOUS_MAX_CYCLES,
        timeout_seconds: int = 3_600,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Execute a complete goal inside one MCP request.

        The connected ChatGPT model is used as the planner/reasoner through
        MCP sampling.  The server owns the action loop and executes actions
        itself, so a runner failure or browser transition never becomes a
        user-visible intermediate turn.
        """

        if self.chatgpt_direct:
            return await self._run_direct_workflow(workflow, acceptance, timeout_seconds)
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal is required")
        try:
            cycle_limit = max(1, min(int(max_cycles), AUTONOMOUS_MAX_CYCLES))
            timeout_limit = max(1, min(int(timeout_seconds), 6 * 60 * 60))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_cycles and timeout_seconds must be integers") from exc
        workflow = dict(workflow) if isinstance(workflow, dict) else None
        acceptance_value = acceptance if acceptance is not None else (workflow or {}).get("acceptance", (workflow or {}).get("acceptance_checks", []))
        acceptance_checks = _normalize_acceptance_specs(acceptance_value)
        if any(not isinstance(item, dict) for item in acceptance_checks):
            raise ValueError("acceptance must be a list or object of check objects")

        started = time.monotonic()
        queue = self._autonomous_initial_steps(workflow)
        had_initial_steps = bool(queue)
        md_goal, md_markers = _autonomous_goal_requires_md(goal, workflow, [])
        production_goal, production_markers, production_duration = _autonomous_goal_requires_production(
            goal, workflow, []
        )
        # A natural-language GLYCAM/MD request has a mandatory browser
        # prerequisite.  Inject it before any sampled planning so a client
        # that only provides one sampling turn cannot stop at diagnostics or
        # at a guessed/stale glycan directory.  User-supplied steps remain in
        # the same queue and execute after this verified prerequisite.
        explicit_browser_build = (
            ("打开" in goal and "glycam" in goal.lower())
            or "open glycam" in goal.lower()
            or "browser" in goal.lower() and "glycam" in goal.lower()
        )
        if md_goal and _requested_sugar_count(goal) is not None and explicit_browser_build and not any(
            _compat_action(
                item.get("action", item.get("kind", item.get("name", item.get("tool", ""))))
            )
            == "browser_build_glycam"
            for item in queue
            if isinstance(item, dict)
        ):
            queue.insert(
                0,
                {
                    "id": "__glycam_browser_prerequisite__",
                    "action": "browser_build_glycam",
                    "args": {"goal": goal},
                    "__server_injected": True,
                },
            )
        context: dict[str, Any] = {
            "steps": {},
            "last": {},
            "goal": goal,
            "__requires_md": md_goal,
            "__requires_production": production_goal,
            "__production_markers": production_markers,
            "__production_duration": production_duration,
            "__glycam_browser_verified": False,
            "__glycam_downloads": [],
            "__glycam_input": None,
            "__glycam_sequence": None,
            "__glycam_target_root": None,
            "__md_step_enqueued": False,
            "__production_step_enqueued": False,
            "__production_verified": False,
            "__md_verified": False,
            "__autonomous_started_epoch": time.time(),
            "__autonomous_deadline": started + timeout_limit,
            "__action_attempts": {},
            "__recovery_pending": False,
            "__diagnosis_pending": False,
        }
        transcript: list[dict[str, Any]] = []
        fallback_after_sampling_error = False
        sampling_error: str | None = None
        last_action: str | None = None
        last_needs_supervision = False
        sampling_actions_started = False
        # This latch survives diagnostic actions.  Previously a failed runner
        # was followed by self_check, which reset ``last_needs_supervision``;
        # the model could then return ``done`` even though the original action
        # had never been repaired or retried.
        recovery_pending = False
        recovery_action: str | None = None
        md_verified = False
        terminal_success_actions = 0
        diagnosis_pending = False
        diagnosis_enqueued: set[str] = set()
        deterministic_recovery_attempts: dict[str, int] = {}
        production_verified = False
        terminal_success_latched = False
        terminal_success_task_id: str | None = None
        terminal_success_system_dir: str | None = None

        async def active_task_checkpoint(reason: str, current_cycle: int) -> dict[str, Any] | None:
            """Return a hard nonterminal checkpoint for a still-running Worker task.

            A model can emit ``done`` after seeing a timeout-looking tool result,
            and an older runner can also leave a task id nested in the last
            action record.  Before *any* supervisor boundary is allowed to
            become user-visible, re-read every task id owned by this workflow
            from the repository.  This is deliberately based on the task table,
            not on the model's prose or on a stale ``status`` field in a prior
            result.
            """

            candidates: list[str] = []
            seen: set[str] = set()

            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    task_id = _task_id_from_result(value)
                    if task_id and task_id not in seen:
                        seen.add(task_id)
                        candidates.append(task_id)
                    for key in ("result", "last", "steps", "task", "task_snapshot"):
                        if key in value:
                            collect(value.get(key))
                elif isinstance(value, list):
                    for item in value:
                        collect(item)

            collect(context.get("last"))
            collect(context.get("steps", {}))

            for task_id in candidates:
                current = await self.tasks.get(task_id)
                if not current:
                    continue
                status = str(current.get("status") or "").lower()
                if status not in {"queued", "running"}:
                    continue
                return {
                    "success": False,
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "final_answer_allowed": False,
                    "detached": True,
                    "continue_required": True,
                    "workflow_status": "in_progress",
                    "task_id": task_id,
                    "status": status,
                    "next_action": "monitor_task",
                    "error": reason,
                    "message": (
                        "The Worker task is still queued/running. This is not a "
                        "user-facing result; keep monitoring it until the task "
                        "record is terminal (success, failed, or cancelled)."
                    ),
                    "task_snapshot": {
                        "task_id": task_id,
                        "status": status,
                        "profile": current.get("profile"),
                        "started_at": current.get("started_at"),
                        "finished_at": current.get("finished_at"),
                    },
                    "cycles": current_cycle,
                }
            return None

        async def evaluate_acceptance(check_specs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            checks: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            try:
                for index, raw_check in enumerate(check_specs or []):
                    checked = await self._compat_check(_resolve_compat_value(raw_check, context), context)
                    checked["index"] = index
                    checks.append(checked)
                failed = [item for item in checks if not item.get("passed")]
            except Exception as exc:
                failed = [{"passed": False, "error": _redact_compat_text(exc)}]
            return checks, failed

        async def emit(message: str) -> None:
            # The MCP wrapper also emits heartbeats.  This best-effort log is
            # useful for local diagnostics and does not create a ChatGPT turn.
            audit(self.ctx.logger, "autonomous_progress", message=message[:500])
            if ctx is not None:
                try:
                    await ctx.info(message, logger_name="xiao-autonomous")
                except Exception:
                    pass

        def sync_supervision_context() -> None:
            context["__recovery_pending"] = recovery_pending
            context["__diagnosis_pending"] = diagnosis_pending

        def enqueue_automatic_diagnosis(action: str, args: dict[str, Any], result: Any, reason: str) -> bool:
            """Inject one read-only diagnosis before the model can retry.

            This is deliberately server-owned.  A web model may still decide
            how to repair the cause, but it cannot skip the evidence/search
            phase merely by emitting another action or ``done``.
            """

            nonlocal diagnosis_pending
            if action in _AUTONOMOUS_ROOT_DIAGNOSIS_ACTIONS:
                return False
            fingerprint = _autonomous_action_fingerprint(action, args)
            if fingerprint in diagnosis_enqueued:
                return False
            diagnosis_enqueued.add(fingerprint)
            diagnosis_pending = True
            sync_supervision_context()
            result_dict = result if isinstance(result, dict) else {"result": result}
            expected_paths: list[str] = []
            for key in ("path", "output_path", "target", "target_root", "glycan_dir", "script_path"):
                value = args.get(key) if isinstance(args, dict) else None
                if isinstance(value, str) and value.strip() and ("/" in value or "\\" in value or "." in value):
                    expected_paths.append(value)
            diagnostic_args = {
                "operation": action,
                "task_id": result_dict.get("task_id"),
                "error_text": _tail(_failure_text(result_dict, {}), 4_000) or f"{action} returned a recoverable failure",
                "expected_paths": expected_paths[:20],
                "browser_check": action.startswith("browser_"),
                "auto_search": True,
                "__automatic": True,
            }
            queue.insert(0, {"action": "self_check", "args": diagnostic_args, "__automatic_diagnosis": True})
            transcript.append(
                {
                    "cycle": cycle,
                    "kind": "automatic_diagnosis_enqueued",
                    "action": action,
                    "fingerprint": fingerprint,
                    "reason": reason,
                }
            )
            return True

        def declared_recovery(raw_step: dict[str, Any]) -> list[dict[str, Any]]:
            """Return a bounded repair branch declared with a workflow step.

            Chat clients that do not support MCP sampling can still submit a
            complete one-call workflow.  ``on_error``/``repair`` makes the
            recovery contract executable rather than relying on the client to
            receive an intermediate error and start a second turn.
            """
            raw_repair = (
                raw_step.get("on_error")
                or raw_step.get("repair")
                or raw_step.get("recovery_steps")
                or []
            )
            if isinstance(raw_repair, dict):
                raw_repair = [raw_repair]
            if not isinstance(raw_repair, list):
                return []
            try:
                attempt = int(raw_step.get("__autonomous_recovery_attempt", 0) or 0)
                max_retries = int(raw_step.get("max_retries", raw_step.get("retries", 1)) or 0)
            except (TypeError, ValueError):
                attempt, max_retries = 0, 1
            if attempt >= max(0, min(max_retries, 3)):
                return []
            repair_steps = [dict(item) for item in raw_repair if isinstance(item, dict)]
            if not repair_steps:
                return []
            retry_step = dict(raw_step)
            retry_step["__autonomous_recovery_attempt"] = attempt + 1
            return repair_steps + [retry_step]

        def enqueue_recovery(raw_step: dict[str, Any], reason: str) -> bool:
            repair_steps = declared_recovery(raw_step)
            if not repair_steps:
                return False
            queue[0:0] = repair_steps
            transcript.append(
                {
                    "cycle": cycle,
                    "kind": "recovery_branch_enqueued",
                    "reason": reason,
                    "step_id": str(raw_step.get("id", raw_step.get("label", ""))),
                    "count": len(repair_steps),
                }
            )
            return True

        def enqueue_production_stage(md_result: Any, current_cycle: int) -> bool:
            """Append the required final stage exactly once after MD setup."""

            if not production_goal or context.get("__production_step_enqueued"):
                return False
            system_dir = _extract_md_system_dir(md_result)
            target_root = str(
                (md_result.get("target_root") if isinstance(md_result, dict) else None)
                or context.get("__glycam_target_root")
                or (str(Path(system_dir).parent) if system_dir else ".")
            ).strip()
            context["__production_system_dir"] = system_dir
            context["__production_step_enqueued"] = True
            remaining_seconds = max(
                120,
                int(float(context.get("__autonomous_deadline", time.monotonic() + 3_600)) - time.monotonic()),
            )
            args: dict[str, Any] = {
                "target_root": target_root,
                "duration_seconds": float(context.get("__production_duration") or production_duration),
                "work_id": "1",
                "timeout": min(6 * 60 * 60, remaining_seconds),
            }
            if system_dir:
                args["system_dir"] = system_dir
            queue.insert(
                0,
                {
                    "id": "__production_after_md__",
                    "action": "production_md",
                    "args": args,
                    "__server_injected": True,
                },
            )
            transcript.append(
                {
                    "cycle": current_cycle,
                    "kind": "production_stage_enqueued",
                    "action": "production_md",
                    "system_dir": system_dir,
                    "target_root": target_root,
                    "duration_seconds": args["duration_seconds"],
                    "markers": production_markers,
                }
            )
            return True

        for cycle in range(1, cycle_limit + 1):
            if time.monotonic() - started >= timeout_limit:
                active = await active_task_checkpoint(
                    "The autonomous deadline was reached while a Worker task was still running.",
                    cycle - 1,
                )
                if active is not None:
                    active["timeout"] = True
                    active["next_action"] = "monitor_task"
                    return active
                return {
                    "success": False, "completed": False, "terminal": True, "user_visible_final": True,
                    "final_answer_allowed": True,
                    "workflow_status": "terminal_timeout", "error": "Autonomous workflow timed out after no active Worker task remained",
                    "terminal_reason": "timeout_without_active_task",
                    "cycles": cycle - 1, "timeout": True, "transcript": transcript[-40:],
                }
            if ctx is not None:
                try:
                    await ctx.report_progress(cycle, cycle_limit, f"autonomous cycle {cycle}/{cycle_limit}")
                except Exception:
                    pass

            # Once the required production task has returned a verified
            # terminal result, discard any stale sampled/diagnostic actions
            # that may have been queued before that result arrived.  A later
            # browser read timeout must not reopen the completed workflow or
            # start a second MD run.
            if terminal_success_latched:
                queue.clear()

            if queue:
                raw_step = queue.pop(0)
                decision = {"done": False, "steps": [raw_step], "source": "initial_workflow"}
            else:
                # The server-injected GLYCAM prerequisite and MD runner form
                # a complete domain adapter.  Once the runner has returned a
                # verified terminal result, close this same MCP request
                # directly; do not ask the web client for an extra sampled
                # "done" turn (that was the second continuation boundary in
                # the original failure).
                if (
                    md_goal
                    and md_verified
                    and (not production_goal or production_verified)
                    and last_action
                    and not recovery_pending
                    and not last_needs_supervision
                ):
                    active = await active_task_checkpoint(
                        "A previous Worker task is still running; completion cannot be reported yet.",
                        cycle,
                    )
                    if active is not None:
                        queue.insert(0, {"action": "monitor_task", "args": {"task_id": active["task_id"], "timeout_seconds": MCP_REQUEST_WAIT_SECONDS}})
                        transcript.append({"cycle": cycle, "kind": "active_task_barrier", "task_id": active["task_id"]})
                        continue
                    checks, failed = await evaluate_acceptance(acceptance_checks)
                    if failed:
                        transcript.append(
                            {
                                "cycle": cycle,
                                "kind": "acceptance_failed",
                                "checks": _redact_compat_value(checks),
                                "failed": _redact_compat_value(failed),
                            }
                        )
                        recovery_pending = True
                        recovery_action = "__acceptance__"
                        last_needs_supervision = True
                        continue
                    return {
                        "success": True,
                        "completed": True,
                        "terminal": True,
                        "user_visible_final": True,
                        "workflow_status": "terminal_success",
                        "finalize_now": True,
                        "answer": (
                            "GLYCAM browser construction, the requested EM/NVT/NPT MD stages, "
                            "and the final production stage completed successfully."
                            if production_goal
                            else "GLYCAM browser construction and the requested EM/NVT/NPT MD stages completed successfully."
                        ),
                        "cycles": cycle,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "acceptance": checks,
                        "terminal_proof": (
                            {
                                "profile": "production_md",
                                "task_id": terminal_success_task_id,
                                "system_dir": terminal_success_system_dir,
                            }
                            if terminal_success_latched
                            else None
                        ),
                        "transcript": transcript[-40:],
                    }
                # A supplied workflow is a complete initial plan.  When all
                # of its actions finished without a pending/error marker, do
                # the acceptance checks here and close the same MCP call.  In
                # particular, do not ask the connected model for an extra
                # decision merely because a simple file operation succeeded.
                # Once sampling has started, however, every sampled action
                # must be followed by another model decision or explicit
                # ``done`` so a sampled multi-step goal cannot stop early.
                if (
                    had_initial_steps
                    and not sampling_actions_started
                    and last_action
                    and not recovery_pending
                    and not (md_goal and not md_verified)
                    and not (production_goal and not production_verified)
                ):
                    active = await active_task_checkpoint(
                        "The initial workflow still owns an active Worker task; completion cannot be reported yet.",
                        cycle,
                    )
                    if active is not None:
                        queue.insert(0, {"action": "monitor_task", "args": {"task_id": active["task_id"], "timeout_seconds": MCP_REQUEST_WAIT_SECONDS}})
                        transcript.append({"cycle": cycle, "kind": "active_task_barrier", "task_id": active["task_id"]})
                        continue
                    contract_error = _acceptance_contract_error(acceptance_checks)
                    if contract_error and not terminal_success_actions:
                        transcript.append(
                            {
                                "cycle": cycle,
                                "kind": "acceptance_contract_missing",
                                "reason": contract_error,
                            }
                        )
                        if ctx is None:
                            return {
                                "success": False,
                                "completed": False,
                                "terminal": True,
                                "user_visible_final": True,
                                "workflow_status": "terminal_failure",
                                "error": contract_error,
                                "acceptance": [],
                                "cycles": cycle,
                                "transcript": transcript[-40:],
                            }
                    else:
                        checks, failed = await evaluate_acceptance(acceptance_checks)
                        if failed:
                            transcript.append(
                                {
                                    "cycle": cycle,
                                    "kind": "acceptance_failed",
                                    "checks": _redact_compat_value(checks),
                                    "failed": _redact_compat_value(failed),
                                }
                            )
                            last_needs_supervision = True
                            if ctx is None:
                                return {
                                    "success": False,
                                    "completed": False,
                                    "terminal": True,
                                    "user_visible_final": True,
                                    "workflow_status": "terminal_failure",
                                    "error": "Initial workflow acceptance checks failed",
                                    "acceptance": checks,
                                    "diagnostic_evidence": failed,
                                    "cycles": cycle,
                                    "transcript": transcript[-40:],
                                }
                        elif not last_needs_supervision:
                            return {
                                "success": True,
                                "completed": True,
                                "terminal": True,
                                "user_visible_final": True,
                                "workflow_status": "terminal_success",
                                "finalize_now": True,
                                "answer": "Initial workflow completed and acceptance checks passed.",
                                "cycles": cycle,
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                "acceptance": checks,
                                "transcript": transcript[-40:],
                            }
                decision, sampling_error = await self._autonomous_sample(goal, transcript, ctx, workflow)
                if decision is None:
                    # The web ChatGPT client does not always advertise MCP
                    # sampling/createMessage.  For the mandatory GLYCAM/MD
                    # adapter, do not turn that capability gap into a
                    # user-visible stop after a transient browser/runner
                    # error.  The action already supplied machine-readable
                    # evidence; run the bounded self-check/retry branch in
                    # this same MCP request.  A retry_nonce changes the
                    # action identity while the underlying runner receives
                    # the same verified inputs.
                    if md_goal and recovery_pending and recovery_action in {
                        "browser_build_glycam",
                        "md_simulation",
                    }:
                        failed_action = str(recovery_action)
                        retry_number = int(deterministic_recovery_attempts.get(failed_action, 0) or 0) + 1
                        if retry_number <= 2:
                            deterministic_recovery_attempts[failed_action] = retry_number
                            retry_args = dict(context.get("__last_action_args") or {})
                            retry_args["retry_nonce"] = f"{failed_action}-{retry_number}-{uuid.uuid4().hex[:8]}"
                            retry_step = {
                                "id": f"__deterministic_retry_{failed_action}_{retry_number}__",
                                "action": failed_action,
                                "args": retry_args,
                                "__server_injected": True,
                            }
                            # Automatic diagnosis is normally already queued
                            # after the failed action. If it has run, keep a
                            # single fresh retry; otherwise let the queued
                            # diagnosis/search precede that retry.
                            if not queue and last_action not in _AUTONOMOUS_DIAGNOSTIC_ACTIONS:
                                queue.append(
                                    {
                                        "id": f"__deterministic_self_check_{failed_action}_{retry_number}__",
                                        "action": "self_check",
                                        "args": {
                                            "operation": failed_action,
                                            "error_text": _tail(_failure_text(context.get("last", {}), {}), 4_000),
                                            "browser_check": failed_action.startswith("browser_"),
                                            "auto_search": True,
                                            "__automatic": True,
                                        },
                                        "__automatic_diagnosis": True,
                                    }
                                )
                            queue.append(retry_step)
                            transcript.append(
                                {
                                    "cycle": cycle,
                                    "kind": "deterministic_recovery_enqueued",
                                    "action": failed_action,
                                    "retry_number": retry_number,
                                    "reason": sampling_error or "sampling unavailable",
                                }
                            )
                            await emit(
                                f"autonomous deterministic recovery queued: {failed_action} retry {retry_number}/2"
                            )
                            continue
                    if not fallback_after_sampling_error:
                        fallback_after_sampling_error = True
                        # If a client lacks server sampling, still perform one
                        # deterministic diagnostic pass after a failed action.
                        if last_action and context.get("last") and last_action not in _AUTONOMOUS_DIAGNOSTIC_ACTIONS:
                            last_result = context["last"]
                            if last_action.startswith("browser_"):
                                tab_id = None
                                if isinstance(last_result, dict):
                                    tab_id = last_result.get("tab_id")
                                    if not tab_id and isinstance(last_result.get("tab"), dict):
                                        tab_id = last_result["tab"].get("tab_id")
                                queue.extend(
                                    [
                                        {"action": "browser_list_tabs", "args": {}},
                                        {"action": "browser_read", "args": {"tab_id": tab_id}} if tab_id else {"action": "browser_list_tabs", "args": {}},
                                    ]
                                )
                                continue
                            queue.append(
                                {
                                    "action": "self_check",
                                    "args": {
                                        "operation": last_action,
                                        "error_text": _failure_text({"result": last_result}, {}),
                                        "auto_search": True,
                                    },
                                }
                            )
                            continue
                    # A sampling callback that times out is not a successful
                    # terminal boundary.  Returning ``terminal=true`` here
                    # let a web client turn the error into a friendly-looking
                    # final sentence (the exact failure seen in the browser
                    # verification).  Hand the evidence back as a
                    # nonterminal repair checkpoint instead; the outer MCP
                    # guard will raise ToolError and the connected web model
                    # must continue with self_check/changed repair actions.
                    return {
                        "success": False,
                        "completed": False,
                        "terminal": False,
                        "user_visible_final": False,
                        "do_not_reply": True,
                        "final_answer_allowed": False,
                        "repair_required": True,
                        "recoverable_error": True,
                        "workflow_status": "repair_required",
                        "error": sampling_error or "No valid supervisor decision",
                        "error_class": "sampling_unavailable",
                        "sampling_required": True,
                        "self_check_required": bool(recovery_pending or last_needs_supervision),
                        "next_action": "self_check" if (recovery_pending or last_needs_supervision) else "choose_next_action",
                        "recovery_action": recovery_action,
                        "diagnostic_evidence": _redact_compat_value(context.get("last", {})),
                        "cycles": cycle,
                        "transcript": transcript[-40:],
                    }
                if decision.get("invalid"):
                    transcript.append({"cycle": cycle, "kind": "invalid_sampling_decision", "raw": _redact_compat_value(decision.get("raw")), "sampling_text": decision.get("sampling_text", "")})
                    continue
                if decision.get("done"):
                    active = await active_task_checkpoint(
                        "The requested workflow still owns an active Worker task; the done decision is rejected until it reaches a terminal status.",
                        cycle,
                    )
                    if active is not None:
                        transcript.append({"cycle": cycle, "kind": "premature_done_rejected", "reason": "active_worker_task", "task_id": active["task_id"]})
                        queue.insert(0, {"action": "monitor_task", "args": {"task_id": active["task_id"], "timeout_seconds": MCP_REQUEST_WAIT_SECONDS}})
                        await emit(f"autonomous done rejected: active Worker task {active['task_id']}")
                        continue
                    if (
                        recovery_pending
                        or last_needs_supervision
                        or (md_goal and not md_verified)
                        or (production_goal and not production_verified)
                    ):
                        reason = "an earlier action still needs repair/retry"
                        if md_goal and not md_verified:
                            reason = "the dedicated md_simulation runner has not returned verified terminal success"
                        elif production_goal and not production_verified:
                            reason = "the requested final/production stage has not returned verified terminal success"
                        transcript.append(
                            {
                                "cycle": cycle,
                                "kind": "premature_done_rejected",
                                "reason": reason,
                                "last_action": last_action,
                                "recovery_pending": recovery_pending,
                                "md_required": md_goal,
                                "md_verified": md_verified,
                                "production_required": production_goal,
                                "production_verified": production_verified,
                            }
                        )
                        await emit(f"autonomous done rejected: {reason}")
                        continue
                    decision_checks = _normalize_acceptance_specs(decision.get("acceptance"))
                    check_specs = decision_checks or acceptance_checks
                    contract_error = None if terminal_success_actions else _acceptance_contract_error(check_specs)
                    if contract_error:
                        transcript.append(
                            {
                                "cycle": cycle,
                                "kind": "premature_done_rejected",
                                "reason": contract_error,
                                "last_action": last_action,
                            }
                        )
                        await emit(f"autonomous done rejected: {contract_error}")
                        continue
                    checks, failed = await evaluate_acceptance(check_specs)
                    if failed:
                        transcript.append({"cycle": cycle, "kind": "acceptance_failed", "checks": _redact_compat_value(checks), "failed": _redact_compat_value(failed)})
                        recovery_pending = True
                        recovery_action = "__acceptance__"
                        last_needs_supervision = True
                        await emit("autonomous acceptance failed; asking the supervisor for a repair")
                        continue
                    return {
                        "success": True, "completed": True, "terminal": True, "user_visible_final": True,
                        "workflow_status": "terminal_success", "finalize_now": True, "answer": decision.get("answer", ""),
                        "cycles": cycle, "elapsed_seconds": round(time.monotonic() - started, 3),
                        "acceptance": checks, "transcript": transcript[-40:],
                    }
                steps = decision.get("steps") or []
                if not steps:
                    transcript.append({"cycle": cycle, "kind": "empty_sampling_decision", "sampling_text": decision.get("sampling_text", "")})
                    continue
                raw_step = steps[0]
                sampling_actions_started = True
                queue[0:0] = [dict(item) for item in steps[1:] if isinstance(item, dict)]

            if not isinstance(raw_step, dict):
                transcript.append({"cycle": cycle, "kind": "invalid_action", "raw": _redact_compat_value(raw_step)})
                continue
            step_id = str(raw_step.get("id", raw_step.get("label", f"cycle_{cycle}")))
            try:
                sync_supervision_context()
                action, args, result = await self._autonomous_execute_action(raw_step, context)
                last_action = action
                last_needs_supervision = _autonomous_result_needs_supervision(result)
                previous_recovery_pending = recovery_pending
                previous_recovery_action = recovery_action
                if action.startswith("browser_") and _browser_result_has_verified_glycam(result):
                    context["__glycam_browser_verified"] = True
                if action.startswith("browser_"):
                    known_downloads = context.setdefault("__glycam_downloads", [])
                    seen_downloads = {
                        (str(item.get("id")), str(item.get("file_path") or item.get("filename")))
                        for item in known_downloads
                        if isinstance(item, dict)
                    }
                    for download in _browser_download_records(result):
                        identity = (str(download.get("id")), str(download.get("file_path") or download.get("filename")))
                        if identity not in seen_downloads:
                            known_downloads.append(download)
                            seen_downloads.add(identity)
                if action == "browser_build_glycam" and isinstance(result, dict):
                    sequence = str(result.get("glycam_sequence") or result.get("sequence") or "").strip()
                    if sequence:
                        context["__glycam_sequence"] = sequence
                    if _autonomous_terminal_success(result):
                        current_input = str(result.get("glycan_input") or "").strip()
                        if current_input:
                            context["__glycam_input"] = current_input
                        current_target_root = str(result.get("target_root") or "").strip()
                        if current_target_root:
                            context["__glycam_target_root"] = current_target_root
                        if md_goal and not context.get("__md_step_enqueued"):
                            glycan_input = str(result.get("glycan_input") or "").strip()
                            target_root = str(result.get("target_root") or "").strip()
                            if not glycan_input or not target_root or not sequence:
                                result = {
                                    **result,
                                    "success": False,
                                    "completed": False,
                                    "terminal": False,
                                    "user_visible_final": False,
                                    "do_not_reply": True,
                                    "finalize_now": False,
                                    "workflow_status": "repair_required",
                                    "repair_required": True,
                                    "recoverable_error": True,
                                    "error_class": "glycam_stage_contract_incomplete",
                                    "error": "The browser build completed without a usable glycan_input, target_root, or sequence for the MD runner.",
                                }
                            else:
                                context["__glycam_browser_verified"] = True
                                context["__md_step_enqueued"] = True
                                remaining_seconds = max(120, int(float(context.get("__autonomous_deadline", time.monotonic() + 3_600)) - time.monotonic()))
                                queue.insert(
                                    0,
                                    {
                                        "id": "__md_after_glycam__",
                                        "action": "md_simulation",
                                        "args": {
                                            "script_path": None,
                                            "cwd": target_root,
                                             "timeout": min(6 * 3_600, remaining_seconds),
                                            "args": [
                                                "--glycan-dir",
                                                glycan_input,
                                                "--target-root",
                                                target_root,
                                                "--sequence",
                                                sequence,
                                            ],
                                        },
                                        "__server_injected": True,
                                    },
                                )
                        else:
                            context["__glycam_browser_verified"] = True
                    last_needs_supervision = _autonomous_result_needs_supervision(result)

                # The compatibility MD runner intentionally returns a
                # nonterminal setup checkpoint after a successful EM/NVT/NPT
                # task.  Promote that checkpoint into the same server-owned
                # production stage used by the full supervisor.  This closes
                # the direct-runner escape hatch that previously let a web
                # model see PIPELINE_SUCCESS and answer before timed MD.
                md_setup_checkpoint = (
                    action == "md_simulation"
                    and isinstance(result, dict)
                    and result.get("error_class") == "md_setup_complete_production_required"
                )
                if md_setup_checkpoint:
                    production_goal = True
                    context["__requires_production"] = True
                    context["__md_verified"] = True
                    context["__production_duration"] = result.get(
                        "production_duration_seconds", production_duration
                    )
                    try:
                        production_duration = max(
                            1.0,
                            min(float(result.get("production_duration_seconds", production_duration)), 24 * 60 * 60),
                        )
                    except (TypeError, ValueError):
                        production_duration = 300.0
                    context["__production_markers"] = production_markers or [
                        "PRODUCTION_MD_SUCCESS",
                        "PRODUCTION_MD_RUNTIME_SECONDS",
                        "PRODUCTION_MD_REQUESTED_WALL_SECONDS",
                    ]
                    md_verified = True
                    recovery_pending = False
                    recovery_action = None
                    diagnosis_pending = False
                    last_needs_supervision = False
                    enqueue_production_stage(result, cycle)

                if _autonomous_terminal_success(result):
                    terminal_success_actions += 1
                if action == "md_simulation" and _autonomous_terminal_success(result):
                    md_verified = True
                    context["__md_verified"] = True
                    recovery_pending = False
                    recovery_action = None
                    diagnosis_pending = False
                    # A successful setup is not the end when the original
                    # goal also asks for a final/timed production stage.
                    # Queue that stage from the exact current run evidence
                    # before any sampled ``done`` decision can be accepted.
                    enqueue_production_stage(result, cycle)
                elif action == "production_md" and _autonomous_terminal_success(result):
                    production_verified = True
                    context["__production_verified"] = True
                    terminal_success_latched = True
                    terminal_success_task_id = str(result.get("task_id") or "") or None
                    terminal_success_system_dir = str(
                        result.get("system_dir")
                        or (result.get("payload") or {}).get("system_dir")
                        or ""
                    ).strip() or None
                    queue.clear()
                    recovery_pending = False
                    recovery_action = None
                    diagnosis_pending = False
                    last_needs_supervision = False
                elif action in {"monitor_task", "wait_task"} and _autonomous_terminal_success(result):
                    # Long-running production actions can cross the MCP
                    # response window.  The web model then polls the child
                    # task with monitor_task.  Treat that terminal child
                    # result exactly like the original production_md action;
                    # otherwise the supervisor would keep sampling browser
                    # checks and could retry GLYCAM after a transient page
                    # read failure even though production already passed.
                    monitored_profile = str(result.get("profile") or "").strip().lower()
                    if not monitored_profile and result.get("task_id"):
                        monitored_task = await self.tasks.get(str(result["task_id"]))
                        monitored_profile = str((monitored_task or {}).get("profile") or "").strip().lower()
                    if monitored_profile == "production_md":
                        production_verified = True
                        context["__production_verified"] = True
                        terminal_success_latched = True
                        terminal_success_task_id = str(result.get("task_id") or "") or None
                        terminal_success_system_dir = str(
                            result.get("system_dir")
                            or (result.get("payload") or {}).get("system_dir")
                            or ""
                        ).strip() or None
                        queue.clear()
                        recovery_pending = False
                        recovery_action = None
                        diagnosis_pending = False
                        last_needs_supervision = False
                elif action in _AUTONOMOUS_DIAGNOSTIC_ACTIONS:
                    if action == "self_check" and isinstance(result, dict) and result.get("self_check_completed"):
                        diagnosis_pending = False
                    elif action == "search_web" and isinstance(result, dict):
                        diagnosis_pending = False
                    if previous_recovery_pending:
                        # Evidence, waiting, navigation, and search cannot
                        # repair the original failed action by themselves.
                        recovery_pending = True
                        recovery_action = previous_recovery_action
                    else:
                        recovery_pending = last_needs_supervision
                        recovery_action = action if last_needs_supervision else None
                elif last_needs_supervision:
                    recovery_pending = True
                    recovery_action = action
                    diagnosis_pending = True
                elif recovery_pending and action not in _AUTONOMOUS_DIAGNOSTIC_ACTIONS:
                    # A successful changed execution action is the only thing
                    # allowed to clear a prior failure latch.  Read-only
                    # diagnosis/search/wait actions deliberately leave it set.
                    if not diagnosis_pending:
                        recovery_pending = False
                        recovery_action = None
                sync_supervision_context()
                context["steps"][step_id] = result
                context["__last_action"] = action
                context["__last_action_args"] = dict(args)
                context["last"] = result
                record = {
                    "cycle": cycle, "kind": "action", "id": step_id, "action": action,
                    "arguments": _redact_compat_value(args), "result": _redact_compat_value(result),
                }
                transcript.append(record)
                if last_needs_supervision:
                    await emit(f"autonomous action requires recovery: {action}")
                else:
                    await emit(f"autonomous action completed: {action}")
                if last_needs_supervision:
                    enqueue_recovery(raw_step, "action returned a recoverable/nonterminal result")
                    enqueue_automatic_diagnosis(action, args, result, "recoverable action result")
            except Exception as exc:  # noqa: BLE001 - evidence is fed to the supervisor
                error = f"{type(exc).__name__}: {exc}"
                last_action = _compat_action(str(raw_step.get("action", raw_step.get("name", "unknown"))))
                last_needs_supervision = True
                recovery_pending = True
                recovery_action = last_action
                diagnosis_pending = True
                context["last"] = {"status": "failed", "error": error}
                transcript.append({
                    "cycle": cycle, "kind": "action_error", "id": step_id,
                    "action": last_action, "arguments": _redact_compat_value(raw_step), "error": error,
                })
                await emit(f"autonomous action failed; supervisor must repair: {last_action}")
                enqueue_recovery(raw_step, error)
                enqueue_automatic_diagnosis(last_action, {}, context["last"], error)
                sync_supervision_context()

        active = await active_task_checkpoint(
            "The autonomous cycle limit was reached while a Worker task was still running.",
            cycle_limit,
        )
        if active is not None:
            active["cycle_limit"] = cycle_limit
            active["next_action"] = "monitor_task"
            return active
        return {
            "success": False,
            "completed": False,
            "terminal": True,
            "user_visible_final": True,
            "final_answer_allowed": True,
            "workflow_status": "terminal_cycle_limit",
            "terminal_reason": "cycle_limit_without_active_task",
            "error": "Autonomous workflow reached its cycle limit after no active Worker task remained",
            "cycles": cycle_limit,
            "cycle_limit": cycle_limit,
            "transcript": transcript[-40:],
        }

    async def _compat_execute_action(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one legacy workflow action through the current Xiaoruo1 APIs."""

        action = _compat_action(action)
        if action == "browser_open_read":
            started = await self._browser_start(args.get("url"))
            tabs = started.get("tabs") if isinstance(started, dict) else []
            tab_id = args.get("tab_id") or (tabs[0].get("tab_id") if tabs and isinstance(tabs[0], dict) else None)
            if not tab_id:
                raise ValueError("browser_open_read did not return a tab_id")
            page = await self._browser_read(str(tab_id), int(args.get("max_chars", 20_000)))
            return {"opened": True, "tab_id": str(tab_id), "started": started, "page": page}
        if action == "browser_start":
            return await self._browser_start(args.get("url"))
        if action == "browser_list_tabs":
            return await self._browser_list_tabs()
        if action == "browser_read":
            return await self._browser_read(args.get("tab_id"), int(args.get("max_chars", 20_000)))
        if action == "browser_navigate":
            return await self._browser_navigate(str(args["tab_id"]), str(args["url"]), int(args.get("timeout", args.get("timeout_ms", 30_000))))
        if action in {"browser_click", "browser_type", "browser_press", "browser_select_option"}:
            tab_id = str(args.get("tab_id") or "")
            if not tab_id:
                raise ValueError(f"{action} requires tab_id")
            by = args.get("by")
            selector = args.get("selector")
            if args.get("role") and not selector:
                by, selector = "role", args.get("role")
            elif args.get("text") and not selector:
                by, selector = "text", args.get("text")
            if action == "browser_click":
                return await self._browser_click(tab_id, by, selector, args.get("name"), bool(args.get("exact", False)), int(args.get("timeout", args.get("timeout_ms", 15_000))))
            if action == "browser_type":
                return await self._browser_type(tab_id, str(args.get("text", "")), by, selector, args.get("name"), bool(args.get("exact", False)), bool(args.get("clear", True)), int(args.get("timeout", args.get("timeout_ms", 15_000))))
            if action == "browser_press":
                return await self._browser_press(tab_id, str(args["key"]), by, selector, args.get("name"), bool(args.get("exact", False)), int(args.get("timeout", args.get("timeout_ms", 15_000))))
            return await self._browser_select_option(tab_id, str(args["option"]), str(args.get("option_by", "label")), by, selector, args.get("name"), bool(args.get("exact", False)), int(args.get("timeout", args.get("timeout_ms", 15_000))))
        if action == "browser_wait":
            return await self._browser_wait(str(args["tab_id"]), int(args.get("milliseconds", 1_000)))
        if action == "browser_wait_until":
            deadline = asyncio.get_running_loop().time() + max(1, min(int(args.get("timeout_seconds", 90)), 900))
            last: dict[str, Any] = {}
            while asyncio.get_running_loop().time() < deadline:
                last = await self._browser_wait(str(args["tab_id"]), int(args.get("interval_ms", 1_000)))
                page = last.get("after_wait") if isinstance(last, dict) else None
                if isinstance(page, dict) and page.get("workflow_status") == "terminal_success":
                    return last
                if args.get("text") and isinstance(page, dict) and str(args["text"]).lower() in str(page.get("text", "")).lower():
                    return last
            return {**last, "terminal": False, "completed": False, "timed_out": True, "follow_up_required": True}
        if action == "browser_screenshot":
            return await self._browser_screenshot(str(args["tab_id"]), bool(args.get("full_page", False)))
        if action == "browser_close_tab":
            return await self._browser_close_tab(str(args["tab_id"]))
        if action == "discover_environment":
            return await self._discover_environment(
                request=str(args.get("request", "")),
                cwd=args.get("cwd"),
                distribution=args.get("distribution"),
                roots=args.get("roots"),
                max_results=int(args.get("max_results", 120)),
                read_limit=int(args.get("read_limit", 6_000)),
                max_depth=int(args.get("max_depth", 32)),
            )
        if action == "write_file":
            return await self._write_file(str(args["path"]), str(args.get("content", "")), bool(args.get("create_parents", args.get("create_dirs", True))))
        if action == "patch_file":
            expected = args.get("expected_count", args.get("expected_replacements", 1))
            return await self._patch_file(str(args["path"]), str(args["find"]), str(args.get("replace", "")), int(expected))
        if action == "read_file":
            return await self._read_file(str(args["path"]), args.get("start_line"), args.get("end_line"))
        if action == "list_directory":
            return await self._list_directory(str(args.get("path", ".")), bool(args.get("recursive", False)), int(args.get("limit", 500)))
        if action == "get_path_info":
            return await self._get_path_info(str(args["path"]))
        if action == "sleep":
            seconds = max(0.0, min(float(args.get("seconds", 1.0)), 120.0))
            await asyncio.sleep(seconds)
            return {"slept_seconds": seconds}
        if action == "python":
            return await self._run_python_script(code=str(args.get("code", args.get("script", ""))), cwd=str(args.get("cwd", ".")), timeout=int(args.get("timeout", 120)))
        if action == "powershell":
            return await self._run_powershell_workflow(workflow="custom", commands=[str(args.get("code", args.get("command", "")))], cwd=str(args.get("cwd", ".")), timeout=int(args.get("timeout", 120)))
        if action == "cmd":
            return await self._run_cmd_workflow(workflow="custom", commands=[str(args.get("command", args.get("code", "")))], cwd=str(args.get("cwd", ".")), timeout=int(args.get("timeout", 120)))
        if action == "terminal":
            return await self._run_terminal_workflow(shell=str(args.get("shell", "powershell")), workflow="custom", commands=[str(args.get("command", args.get("code", "")))], cwd=str(args.get("cwd", ".")), distribution=args.get("distribution"), timeout=int(args.get("timeout", 120)))
        if action == "wsl":
            return await self._run_wsl_workflow(workflow="custom", commands=[str(args.get("command", args.get("code", "")))], cwd=str(args.get("cwd", ".")), distribution=args.get("distribution"), timeout=int(args.get("timeout", 120)))
        raise ValueError(f"不支持的兼容工作流动作：{action}")

    async def _compat_check(self, check: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        check_type = str(check.get("type", check.get("kind", check.get("check", "")))).strip().lower()
        if check_type in {"all", "all_of", "any", "any_of"}:
            raw_children = check.get("checks", check.get("items", []))
            children = _normalize_acceptance_specs(raw_children)
            if not children:
                raise ValueError(f"{check_type} requires a non-empty checks list")
            records: list[dict[str, Any]] = []
            for child in children:
                if not isinstance(child, dict):
                    raise ValueError("nested acceptance checks must be objects")
                records.append(await self._compat_check(_resolve_compat_value(child, context), context))
            passed = all(item.get("passed") for item in records) if check_type in {"all", "all_of"} else any(item.get("passed") for item in records)
            return {"type": check_type, "passed": passed, "checks": records}

        def source_for_check() -> Any:
            source_id = str(check.get("step_id", check.get("from_step", check.get("source", ""))))
            return context.get("steps", {}).get(source_id, {}) if source_id else context.get("last", {})

        def nested_value(source: Any, path: Any) -> Any:
            current = source
            tokens = path if isinstance(path, list) else str(path or "").replace("[", ".").replace("]", "").split(".")
            for token in tokens:
                token = str(token).strip()
                if not token:
                    continue
                if isinstance(current, dict):
                    current = current.get(token)
                elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                    current = current[int(token)]
                else:
                    return None
            return current

        if check_type in {"result_success", "step_success", "action_success", "action_succeeded"}:
            source = source_for_check()
            passed = isinstance(source, dict) and not _autonomous_result_needs_supervision(source) and str(source.get("status", "success")).lower() not in {"failed", "cancelled", "error"}
            return {"type": check_type, "passed": passed, "source": _redact_compat_value(source)}

        if check_type in {"result_field_equals", "field_equals", "result_field_contains", "field_contains"}:
            source = source_for_check()
            actual = nested_value(source, check.get("field", check.get("path", "")))
            expected = check.get("value", check.get("expected", check.get("text", "")))
            if check_type in {"result_field_contains", "field_contains"}:
                passed = str(expected).lower() in str(actual).lower()
            else:
                passed = actual == expected or str(actual) == str(expected)
            return {"type": check_type, "passed": passed, "field": check.get("field", check.get("path")), "actual": _redact_compat_value(actual), "expected": _redact_compat_value(expected)}

        if check_type in {"result_field_exists", "field_exists"}:
            source = source_for_check()
            actual = nested_value(source, check.get("field", check.get("path", "")))
            return {"type": check_type, "passed": actual is not None, "field": check.get("field", check.get("path")), "actual": _redact_compat_value(actual)}

        if check_type in {"task_succeeded", "task_terminal_success", "task_status"}:
            source = source_for_check()
            expected_status = str(check.get("status", check.get("value", "success"))).lower()
            actual_status = str(source.get("status", source.get("terminal_status", ""))).lower() if isinstance(source, dict) else ""
            passed = (
                _autonomous_terminal_success(source)
                if check_type == "task_terminal_success"
                else actual_status == expected_status
                if check_type == "task_status"
                else isinstance(source, dict) and source.get("success") is True and not _autonomous_result_needs_supervision(source)
            )
            return {"type": check_type, "passed": passed, "actual_status": actual_status, "expected_status": expected_status}

        if check_type in {"file_exists", "path_exists"}:
            target = resolve_workspace_path(str(check["path"]), self.ctx.settings.allowed_workspaces)
            passed = target.exists()
            return {"type": check_type, "passed": passed, "path": str(target), "exists": passed}
        if check_type == "directory_exists":
            target = resolve_workspace_path(str(check["path"]), self.ctx.settings.allowed_workspaces)
            passed = target.is_dir()
            return {"type": check_type, "passed": passed, "path": str(target), "directory": passed}
        if check_type == "file_nonempty":
            target = resolve_workspace_path(str(check["path"]), self.ctx.settings.allowed_workspaces)
            passed = target.is_file() and target.stat().st_size > 0
            return {"type": check_type, "passed": passed, "path": str(target), "size": target.stat().st_size if target.exists() else 0}
        if check_type == "file_contains":
            read = await self._read_file(str(check["path"]))
            text = str(check.get("text", ""))
            passed = text in str(read.get("content", ""))
            return {"type": check_type, "passed": passed, "path": read.get("path"), "text": text, "content_chars": len(str(read.get("content", "")))}
        if check_type in {"page_text_contains", "page_contains", "browser_page_contains"}:
            source = source_for_check()
            text_value = json.dumps(source, ensure_ascii=False, default=str) if not isinstance(source, dict) else str(source.get("text", source))
            expected = str(check.get("text", ""))
            return {"type": check_type, "passed": expected.lower() in text_value.lower(), "text": expected}
        if check_type in {"python_stdout_contains", "worker_stdout_contains"}:
            source = source_for_check()
            text_value = json.dumps(source, ensure_ascii=False, default=str)
            expected = str(check.get("text", ""))
            return {"type": check_type, "passed": expected in text_value, "text": expected}
        raise ValueError(f"不支持的工作流验收类型：{check_type}")

    async def _write_compat_manifest(self, payload: dict[str, Any]) -> str:
        manifest_root = self.ctx.settings.task_output_root / "workflow_manifests"
        manifest_root.mkdir(parents=True, exist_ok=True)
        path = manifest_root / f"workflow_{uuid.uuid4().hex}.json"
        safe_payload = _redact_compat_value(payload)
        await asyncio.to_thread(path.write_text, json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str), "utf-8")
        return str(path)

    async def _run_workflow(self, workflow: dict[str, Any], timeout_seconds: int = 3_600) -> dict[str, Any]:
        """Run a compatibility workflow through the closed-loop supervisor.

        Keep the historical implementation below as a reference for the
        older manifest contract, but do not expose its early
        ``repair_required`` return path to callers.  REST callers and cached
        MCP clients that invoke this registry method directly must receive the
        same one-call terminal contract as the new public tool.
        """

        acceptance = workflow.get("acceptance", workflow.get("acceptance_checks", workflow.get("final_checks", []))) if isinstance(workflow, dict) else []
        return await self._run_autonomous_workflow(
            str(workflow.get("name") or "Execute the compatibility workflow") if isinstance(workflow, dict) else "Execute the compatibility workflow",
            workflow=workflow if isinstance(workflow, dict) else None,
            acceptance=acceptance if isinstance(acceptance, list) else [acceptance],
            timeout_seconds=timeout_seconds,
            ctx=None,
        )

        started = time.monotonic()
        attempt = int(workflow.get("repair_attempt", 0)) if isinstance(workflow, dict) else 0
        steps = workflow.get("steps", []) if isinstance(workflow, dict) else []
        if isinstance(steps, dict):
            steps = [dict(value, id=str(key)) if isinstance(value, dict) else value for key, value in steps.items()]
        acceptance = workflow.get("acceptance", workflow.get("acceptance_checks", workflow.get("final_checks", []))) if isinstance(workflow, dict) else []
        if isinstance(acceptance, dict):
            acceptance = [acceptance]
        context: dict[str, Any] = {"steps": {}, "last": {}}
        step_records: list[dict[str, Any]] = []
        acceptance_records: list[dict[str, Any]] = []
        failure: dict[str, Any] | None = None
        if not isinstance(steps, list) or not steps:
            failure = {"phase": "workflow_shape", "error": "steps must be a non-empty list"}
            steps = []
        md_steps = [item for item in steps if isinstance(item, dict)]
        is_md, markers = _looks_like_md_workflow(md_steps, acceptance if isinstance(acceptance, list) else [])
        if is_md and failure is None:
            failure = {"phase": "wrong_runner_for_md", "error": "GLYCAM/GROMACS/MD workflows must use run_md_simulation", "markers": markers}
        deadline = started + max(1, min(int(timeout_seconds), 21_600))
        if failure is None:
            for index, raw in enumerate(steps):
                if not isinstance(raw, dict):
                    failure = {"phase": f"step_{index + 1}", "error": "Each workflow step must be an object"}
                    break
                if time.monotonic() >= deadline:
                    failure = {"phase": "timeout", "error": "Workflow timed out before all steps completed", "timed_out": True}
                    break
                step_id = str(raw.get("id", raw.get("label", f"step_{index + 1}")))
                action = ""
                try:
                    action = _compat_action(raw.get("action", raw.get("kind", "")))
                    args = _resolve_compat_value(_compat_args(raw), context)
                    if not action and raw.get("profile"):
                        result = await self._run_workflow_until_complete(
                            [raw],
                            [],
                            max_attempts=max(1, min(int(raw.get("retries", 0)) + 1, 3)),
                            overall_timeout=max(1, int(deadline - time.monotonic())),
                            wait_timeout=max(1, min(int(deadline - time.monotonic()), MCP_REQUEST_WAIT_SECONDS)),
                        )
                        action = "runner_profile"
                    else:
                        result = await self._compat_execute_action(action, args)
                    record = {"id": step_id, "step": index + 1, "action": action, "result": result}
                    step_records.append(record)
                    context["steps"][step_id] = result
                    context["last"] = result
                    if isinstance(result, dict) and (result.get("repair_required") or result.get("recoverable_error") or result.get("continue_required") or (result.get("terminal") is False and result.get("completed") is False)):
                        failure = {"phase": action or f"step_{index + 1}", "error": result.get("error") or result.get("message") or "Step returned a nonterminal checkpoint", "evidence": result}
                        break
                except Exception as exc:  # noqa: BLE001 - return evidence, not an opaque exception
                    failure = {"phase": action if "action" in locals() else f"step_{index + 1}", "error": _redact_compat_text(exc), "evidence": {"step": raw}}
                    break
        if failure is None:
            if not isinstance(acceptance, list):
                failure = {"phase": "acceptance_shape", "error": "acceptance must be a list or object"}
            else:
                for index, raw_check in enumerate(acceptance):
                    try:
                        if not isinstance(raw_check, dict):
                            raise ValueError("Each acceptance check must be an object")
                        resolved_check = _resolve_compat_value(raw_check, context)
                        checked = await self._compat_check(resolved_check, context)
                        checked["index"] = index
                        acceptance_records.append(checked)
                        if not checked.get("passed"):
                            failure = {"phase": "acceptance", "error": f"Acceptance check {index + 1} failed", "evidence": checked}
                            break
                    except Exception as exc:  # noqa: BLE001 - retain the exact failed check
                        failure = {"phase": "acceptance", "error": _redact_compat_text(exc), "evidence": {"index": index, "check": raw_check}}
                        break
        manifest_payload = {
            "workflow": workflow,
            "steps": step_records,
            "acceptance": acceptance_records,
            "failure": failure,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        try:
            manifest_path = await self._write_compat_manifest(manifest_payload)
        except OSError as exc:
            manifest_path = ""
            if failure is None:
                failure = {"phase": "manifest", "error": f"Unable to write workflow manifest: {exc}"}
        if failure is not None:
            error = str(failure.get("error") or "Workflow failed")
            evidence = failure.get("evidence", failure)
            return {
                "name": str(workflow.get("name", "Xiaoruo1 compatibility workflow")) if isinstance(workflow, dict) else "Xiaoruo1 compatibility workflow",
                "success": False,
                "terminal": True,
                "completed": False,
                "user_visible_final": False,
                "workflow_status": "repair_required",
                "error": error,
                "evidence": evidence,
                "diagnostic_evidence": evidence,
                "manifest_path": manifest_path,
                "steps": step_records,
                "acceptance": acceptance_records,
                "repair": build_repair_contract(success=False, attempt=attempt, failed_phase=str(failure.get("phase", "unknown")), error=error, evidence=evidence, manifest_path=manifest_path, timed_out=bool(failure.get("timed_out"))),
            }
        return {
            "name": str(workflow.get("name", "Xiaoruo1 compatibility workflow")),
            "success": True,
            "terminal": True,
            "completed": True,
            "user_visible_final": True,
            "workflow_status": "terminal_success",
            "steps": step_records,
            "acceptance": acceptance_records,
            "manifest_path": manifest_path,
            "repair": build_repair_contract(success=True, attempt=attempt, manifest_path=manifest_path),
        }

    async def _wait_task(self, task_id: str, timeout_seconds: int = 90, poll_interval_seconds: float = 1.0) -> dict[str, Any] | None:
        if self.chatgpt_direct:
            deadline = asyncio.get_running_loop().time() + max(0, min(float(timeout_seconds), 5.0))
            while True:
                result = await self._monitor_task(task_id, 0, poll_interval_seconds)
                if result is None or result.get("terminal") or asyncio.get_running_loop().time() >= deadline:
                    return result
                await asyncio.sleep(min(max(0.1, float(poll_interval_seconds)), 0.5))
        result = await self._monitor_task(task_id, timeout_seconds, poll_interval_seconds)
        if result is None:
            return None
        if result.get("terminal") and result.get("completed"):
            result["repair"] = build_repair_contract(success=True)
        elif result.get("terminal") is False or result.get("repair_required") or not result.get("completed"):
            result["repair"] = build_repair_contract(success=False, error=result.get("error") or result.get("message"), evidence=result, failed_phase="task_wait")
        return result

    async def _monitor_task(self, task_id: str, timeout_seconds: int = MCP_REQUEST_WAIT_SECONDS, poll_interval_seconds: float = 0.5) -> dict[str, Any] | None:
        """Poll briefly without allowing the remote MCP request to hang."""
        if self.chatgpt_direct:
            current = await self.tasks.get(task_id)
            if current is None:
                raise FileNotFoundError(task_id)
            if current["status"] in {"success", "failed", "cancelled"}:
                return await self._finished_task_result(str(current["profile"]), current.get("payload") or {}, current)
            return {**current, "task_id": task_id, "terminal": False, "completed": False,
                    "suggested_next_tool": "task_status"}
        deadline = asyncio.get_running_loop().time() + max(1, min(int(timeout_seconds), MCP_REQUEST_WAIT_SECONDS))
        interval = max(0.2, min(float(poll_interval_seconds), 10.0))
        terminal_statuses = {"success", "failed", "cancelled"}
        current = await self.tasks.get(task_id)
        if current is None:
            return None
        while current["status"] not in terminal_statuses:
            if asyncio.get_running_loop().time() >= deadline:
                return {
                    **current,
                    "completed": False,
                    "terminal": False,
                    "user_visible_final": False,
                    "do_not_reply": True,
                    "detached": True,
                    "continue_required": True,
                    "next_action": "monitor_task",
                    "message": "任务仍在运行；Worker 未停止。必须继续调用 monitor_task，直到得到终态。",
                }
            await asyncio.sleep(interval)
            current = await self.tasks.get(task_id)
            if current is None:
                return None
        return await self._finished_task_result(
            str(current.get("profile") or "workflow"),
            current.get("payload") or {},
            current,
        )

    async def _list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self.tasks.list(max(1, min(int(limit), 500)))

    async def _read_task_output(
        self,
        task_id: str,
        stream: str = "stdout",
        max_chars: int = 200_000,
        *,
        tail: bool = False,
        cursor: int = 0,
    ) -> dict[str, Any]:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        task = await self.tasks.get(task_id)
        if not task:
            raise FileNotFoundError(task_id)
        expected_name = f"{stream}.txt"
        output_path = next((Path(item) for item in task.get("output_files", []) if str(item).lower().endswith(expected_name)), None)
        if output_path is None or not output_path.exists():
            raise FileNotFoundError(f"No {stream} output is available for task {task_id}")
        output_root = self.ctx.settings.task_output_root.resolve()
        try:
            output_path.resolve().relative_to(output_root)
        except ValueError as exc:
            raise PermissionError("Task output path is outside the configured output root") from exc
        if self.chatgpt_direct:
            max_chars = max(1, min(int(max_chars), 64_000))
            if type(cursor) is not int or cursor < 0:
                raise ValueError("cursor must be a nonnegative offset returned by this tool")
            if tail:
                with output_path.open("rb") as handle:
                    handle.seek(max(0, output_path.stat().st_size - max_chars * 4))
                    bounded = handle.read(max_chars * 4).decode("utf-8", errors="replace")[-max_chars:]
                return {"task_id": task_id, "stream": stream, "content": bounded, "truncated": output_path.stat().st_size > len(bounded.encode('utf-8')), "position": "tail", "next_cursor": None}
            with output_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(cursor)
                bounded = handle.read(max_chars)
                position = handle.tell()
                more = bool(handle.read(1))
            return {"task_id": task_id, "stream": stream, "content": bounded, "truncated": more,
                    "position": "page", "cursor": cursor, "next_cursor": position if more else None,
                    "file_path": str(output_path)}
        max_chars = max(1, min(int(max_chars), 1_000_000))
        content = output_path.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > max_chars
        bounded = content[-max_chars:] if tail and truncated else content[:max_chars]
        return {
            "task_id": task_id,
            "stream": stream,
            "content": bounded,
            "truncated": truncated,
            "position": "tail" if tail else "head",
        }

    async def _retry_task(self, task_id: str, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise PermissionError("retry_task requires confirm=true")
        task = await self.tasks.get(task_id)
        if not task:
            raise FileNotFoundError(task_id)
        if task["status"] in {"queued", "running"}:
            raise ValueError("Only a finished task can be retried")
        return await self._create_task(task["profile"], task.get("payload") or {})

    async def _cancel_task(self, task_id: str) -> dict[str, Any] | None:
        return await self.tasks.request_cancel(task_id)

    async def _list_conversations(self) -> list[dict[str, Any]]:
        return await self.conversations.list()

    async def _get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return await self.conversations.get(conversation_id)

    async def _create_canvas(self, title: str, content: str = "", kind: str = "markdown", status: str = "draft") -> dict[str, Any]:
        return await self.ctx.canvas.create(title, content, kind, status)

    async def _get_canvas(self, canvas_id: str, revision: int | None = None) -> dict[str, Any] | None:
        return await self.ctx.canvas.get(canvas_id, revision)

    async def _update_canvas(self, canvas_id: str, content: str, title: str | None = None, kind: str | None = None, status: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return await self.ctx.canvas.update(canvas_id, content, title, kind, status, metadata)

    async def _list_canvas_history(self, canvas_id: str) -> list[dict[str, Any]]:
        return await self.ctx.canvas.history(canvas_id)

    async def _restore_canvas_revision(self, canvas_id: str, revision_number: int) -> dict[str, Any] | None:
        return await self.ctx.canvas.restore(canvas_id, revision_number)