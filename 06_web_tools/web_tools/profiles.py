from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from web_mcp.paths import WorkspaceViolation, resolve_workspace_path
from web_config.settings import Settings


@dataclass(slots=True)
class RunnerCommand:
    command: list[str]
    cwd: Path
    timeout: int
    description: str


def _bounded_timeout(payload: dict[str, Any], settings: Settings) -> int:
    try:
        requested = int(payload.get("timeout", settings.default_task_timeout))
    except (TypeError, ValueError):
        requested = settings.default_task_timeout
    return max(1, min(requested, 24 * 60 * 60))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _md_pipeline_timeout(payload: dict[str, Any], settings: Settings) -> int:
    """Return a safe timeout for the maintained GLYCAM/GROMACS entry point."""

    if _truthy(payload.get("allow_short_validation")):
        return _bounded_timeout(payload, settings)
    try:
        requested = int(payload.get("timeout") or settings.default_task_timeout)
    except (TypeError, ValueError):
        requested = settings.default_task_timeout
    return max(MD_PIPELINE_MIN_TIMEOUT_SECONDS, min(requested, 24 * 60 * 60))


# A maintained GLYCAM -> GROMACS run contains real EM/NVT/NPT stages before
# the optional production stage.  The generic one-hour default is not a safe
# limit for that pipeline: it can kill a valid run during EM and leave the
# browser with a misleading partial result.  The cap remains 24 hours in the
# generic runner; this is only the minimum used when the caller did not
# explicitly opt into a short validation run.
MD_PIPELINE_MIN_TIMEOUT_SECONDS = 6 * 60 * 60


def _safe_args(payload: dict[str, Any]) -> list[str]:
    args = payload.get("args", [])
    if not isinstance(args, list) or len(args) > 100:
        raise ValueError("args must be a list with at most 100 items")
    result = []
    for item in args:
        value = str(item)
        if len(value) > 2000:
            raise ValueError("Each argument is limited to 2000 characters")
        result.append(value)
    return result


def _slug(value: str) -> str:
    """Return a stable filesystem-safe label for staged input directories."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return cleaned.strip("._-") or "glycan"


def _argument_value(args: list[str], flag: str) -> tuple[int, str] | None:
    """Return the position and value for a simple ``--flag value`` pair."""
    for index, value in enumerate(args[:-1]):
        if value == flag:
            return index + 1, args[index + 1]
    return None


def _host_path_argument(value: str) -> str:
    """Accept the Windows and WSL path spellings models commonly return."""
    raw = str(value or "").strip().strip('"\'')
    match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", raw.replace("\\", "/"))
    if match:
        suffix = (match.group(2) or "").replace("/", "\\")
        return f"{match.group(1).upper()}:\\{suffix}" if suffix else f"{match.group(1).upper()}:\\"
    return raw


def _glycan_family(value: str) -> str:
    """Normalize a GLYCAM download stem so PDB and ZIP downloads can pair."""
    stem = Path(value).stem
    stem = re.sub(r"(?i)(?:_structure)?_min-gas$", "", stem)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    return re.sub(r"[^a-z0-9]+", "", stem.lower())


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a GLYCAM bundle without allowing ZIP path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as handle:
        for member in handle.infolist():
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"GLYCAM ZIP contains an unsafe path: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _stage_glycan_file(source: Path, target_root: Path) -> Path:
    """Turn a browser-downloaded GLYCAM file into the directory main.py needs.

    The browser commonly returns only ``*_structure_min-gas.pdb`` while the
    maintained entry point also needs the matching ``parm7`` and ``rst7`` from
    the sibling GLYCAM ZIP.  Stage those inputs under a fingerprinted hidden
    directory instead of treating the PDB path as a directory or reusing an
    unrelated old run.
    """
    source = source.resolve()
    if source.is_dir():
        return source
    if not source.is_file():
        raise FileNotFoundError(f"GLYCAM input does not exist: {source}")
    if source.suffix.lower() not in {".pdb", ".zip"}:
        raise ValueError("GLYCAM input must be a directory, PDB file, or ZIP bundle")

    sibling_archives: list[Path] = []
    if source.suffix.lower() == ".pdb":
        family = _glycan_family(source.name)
        sibling_archives = sorted(
            (
                item
                for item in source.parent.glob("*.zip")
                if _glycan_family(item.name) == family
            ),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        if not sibling_archives:
            raise FileNotFoundError(
                f"No matching GLYCAM ZIP bundle was found beside {source.name}; "
                "the bundle containing parm7/rst7 is required"
            )
        archive = sibling_archives[0]
    else:
        archive = source

    source_stat = source.stat()
    archive_stat = archive.stat()
    fingerprint = hashlib.sha256(
        f"{source}|{source_stat.st_size}|{source_stat.st_mtime_ns}|"
        f"{archive}|{archive_stat.st_size}|{archive_stat.st_mtime_ns}".encode()
    ).hexdigest()[:12]
    stage_root = target_root / ".xiaoruo1_glycan_inputs"
    stage = stage_root / f"{_slug(source.stem)}_{fingerprint}"
    stage.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == ".pdb":
        shutil.copy2(source, stage / "min-gas.pdb")
    bundle_root = stage / "_bundle"
    if not bundle_root.exists() or not any(bundle_root.rglob("*")):
        _safe_extract_zip(archive, bundle_root)
    for pattern, destination_name in (
        ("*.parm7", "unminimized-gas.parm7"),
        ("*.rst7", "unminimized-gas.rst7"),
    ):
        candidates = sorted(bundle_root.rglob(pattern))
        if candidates:
            shutil.copy2(candidates[0], stage / destination_name)

    if source.suffix.lower() == ".zip" and not (stage / "min-gas.pdb").is_file():
        pdb_candidates = sorted(bundle_root.rglob("*.pdb"))
        if pdb_candidates:
            shutil.copy2(pdb_candidates[0], stage / "min-gas.pdb")

    missing = [name for name in ("min-gas.pdb", "unminimized-gas.parm7", "unminimized-gas.rst7") if not (stage / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "GLYCAM download is incomplete after staging; missing: " + ", ".join(missing)
        )
    provenance = {
        "source": str(source),
        "bundle": str(archive),
        "staged_directory": str(stage),
    }
    (stage / "source.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return stage


def _normalize_md_glycan_arg(payload: dict[str, Any], settings: Settings, task_id: str) -> list[str]:
    """Normalize model-provided GLYCAM file/dir arguments before WSL execution."""
    args = _safe_args(payload)
    located = _argument_value(args, "--glycan-dir")
    if located is None:
        return args
    value_index, raw_value = located
    source = resolve_workspace_path(_host_path_argument(raw_value), settings.allowed_workspaces, must_exist=True)
    if source.is_dir():
        return args
    target_value = _argument_value(args, "--target-root")
    raw_target = target_value[1] if target_value is not None else str(payload.get("cwd", "."))
    target_root = resolve_workspace_path(_host_path_argument(raw_target), settings.allowed_workspaces, must_exist=False)
    target_root.mkdir(parents=True, exist_ok=True)
    args[value_index] = str(_stage_glycan_file(source, target_root))
    return args


def _remove_legacy_short_md_overrides(args: list[str], payload: dict[str, Any]) -> list[str]:
    """Prevent cached web clients from downgrading a real MD request to a smoke test.

    Older tool descriptions taught the model to send ``--nsteps 100``, a
    relaxed EM threshold, and ``--allow-nonconverged-em``.  Those flags are
    valid for an explicitly requested smoke test, but are not valid evidence
    for a user asking for a real EM/NVT/NPT workflow.  The server therefore
    strips only the known legacy short-test values unless the caller opts in
    with ``allow_short_validation``.  Larger explicit nsteps values remain
    untouched.
    """
    if payload.get("allow_short_validation") or payload.get("short_validation"):
        return args
    normalized: list[str] = []
    index = 0
    while index < len(args):
        item = str(args[index])
        if item == "--allow-nonconverged-em":
            index += 1
            continue
        if item in {"--nsteps", "--em-emtol"} and index + 1 < len(args):
            raw_value = str(args[index + 1])
            try:
                numeric = float(raw_value)
            except ValueError:
                numeric = None
            is_legacy_short = (
                item == "--nsteps" and numeric is not None and numeric <= 1000
            ) or (
                item == "--em-emtol" and numeric is not None and numeric >= 5_000
            )
            if is_legacy_short:
                index += 2
                continue
        normalized.append(args[index])
        index += 1
    return normalized


def _normalize_workflow_name(value: Any, default: str = "diagnostics") -> str:
    """Normalize the human-facing workflow name used by shell profiles.

    Chat models commonly emit names such as ``list-directory`` or
    ``list directory`` even though the internal preset is named
    ``list_directory``.  Normalizing here keeps the public tool forgiving
    without allowing an arbitrary command to be selected implicitly.
    """
    normalized = str(value or default).strip().lower()
    normalized = re.sub(r"[\s\-/\\–—−]+", "_", normalized)
    return re.sub(r"_+", "_", normalized).strip("_") or default


def python_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    script_path = payload.get("script_path")
    code = payload.get("code")
    if script_path:
        script = resolve_workspace_path(str(script_path), settings.allowed_workspaces, must_exist=True)
        if script.suffix.lower() != ".py":
            raise ValueError("run_python_script only accepts .py files")
        try:
            script_source = script.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise ValueError(f"Unable to read Python script: {script}") from exc
        if not script_source.strip():
            raise ValueError(f"Python script is empty and cannot be treated as a successful task: {script}")
    elif code:
        if not isinstance(code, str) or len(code) > 500_000:
            raise ValueError("Inline Python code is limited to 500000 characters")
        script = settings.generated_script_root / f"{task_id}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(code, encoding="utf-8")
    else:
        raise ValueError("Provide script_path or code")
    cwd = resolve_workspace_path(str(payload.get("cwd", ".")), settings.allowed_workspaces, must_exist=True)
    return RunnerCommand([sys.executable, str(script), *_safe_args(payload)], cwd, _bounded_timeout(payload, settings), f"Python: {script.name}")


def _windows_to_wsl(path: Path) -> str:
    value = str(path.resolve())
    if len(value) >= 2 and value[1] == ":":
        drive = value[0].lower()
        return "/mnt/" + drive + value[2:].replace("\\", "/")
    return value.replace("\\", "/")


def normalize_wsl_path(value: str, distribution: str | None = None) -> tuple[str | None, str]:
    """Convert a Linux path, WSL UNC path, or Windows drive path to a WSL path."""
    raw = str(value or "/").strip()
    if not raw or raw == ".":
        return distribution, "/"
    normalized = raw.replace("\\", "/")
    lowered = normalized.lower()
    for prefix in ("//wsl.localhost/", "//wsl$/"):
        if lowered.startswith(prefix):
            suffix = normalized[len(prefix):]
            parts = suffix.split("/", 1)
            path_distribution = parts[0]
            linux_path = "/" + (parts[1] if len(parts) > 1 else "")
            if distribution and path_distribution.lower() != distribution.lower():
                raise ValueError("WSL path distribution does not match the distribution argument")
            return path_distribution, linux_path or "/"
    if re.match(r"^[A-Za-z]:($|/)", normalized):
        drive = normalized[0].lower()
        remainder = normalized[2:]
        return distribution, f"/mnt/{drive}{remainder or '/'}"
    if normalized.startswith("/"):
        return distribution, normalized
    raise ValueError("WSL path must be absolute, such as /, /home, /mnt/d, or \\\\wsl$\\Distro\\home")


def wsl_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    commands = payload.get("commands")
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list) or not commands:
        workflow = _normalize_workflow_name(payload.get("workflow", "diagnostics"))
        presets = {
            "diagnostics": ["uname -a", "pwd", "python3 --version || true"],
            "git_status": ["git status --short"],
        }
        commands = presets.get(workflow)
        if commands is None:
            raise ValueError("Unknown workflow. Use diagnostics, git_status, or provide an approved command list")
    if not settings.allow_custom_wsl_workflow and payload.get("commands"):
        raise PermissionError("Custom WSL workflows are disabled by configuration")
    if len(commands) > 50 or any(not isinstance(item, str) or len(item) > 4000 for item in commands):
        raise ValueError("WSL workflow contains too many or too-large commands")
    requested_distribution = str(payload.get("distribution") or "").strip() or None
    host_cwd = settings.workspace_root
    parsed_distribution, linux_cwd = normalize_wsl_path(str(payload.get("cwd", ".")), requested_distribution or settings.wsl_distribution)
    raw_cwd = str(payload.get("cwd", "."))
    if not raw_cwd.strip().startswith(("/", "\\\\wsl$", "\\\\wsl.localhost")) and not re.match(r"^[A-Za-z]:($|[\\/])", raw_cwd.strip()):
        host_cwd = resolve_workspace_path(raw_cwd, settings.allowed_workspaces, must_exist=True)
        linux_cwd = _windows_to_wsl(host_cwd)
    distribution = str(requested_distribution or parsed_distribution or settings.wsl_distribution or "")
    script = "set -e\ncd " + shlex.quote(linux_cwd) + "\n" + "\n".join(commands)
    command = ["wsl.exe"]
    if distribution:
        command.extend(["--distribution", distribution])
    command.extend(["--", "bash", "-lc", script])
    return RunnerCommand(command, host_cwd, _bounded_timeout(payload, settings), f"WSL workflow: {payload.get('workflow', 'custom')}")


def _shell_commands(
    payload: dict[str, Any],
    settings: Settings,
    presets: dict[str, list[str]],
    shell_name: str,
) -> tuple[str, list[str]]:
    commands = payload.get("commands")
    if isinstance(commands, str):
        commands = [commands]
    if commands is None:
        workflow = _normalize_workflow_name(payload.get("workflow", "diagnostics"))
        commands = presets.get(workflow)
        if commands is None:
            available = ", ".join(sorted(presets))
            raise ValueError(f"Unknown {shell_name} workflow. Use {available}, or provide an approved command list")
    else:
        workflow = _normalize_workflow_name(payload.get("workflow", "custom"), "custom")
        if not settings.allow_custom_shell_workflow:
            raise PermissionError("Custom Windows shell workflows are disabled by configuration")

    if not isinstance(commands, list) or not commands or len(commands) > 50:
        raise ValueError("commands must be a non-empty list with at most 50 items")
    normalized: list[str] = []
    for item in commands:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Each shell command must be a non-empty string")
        if len(item) > 4000:
            raise ValueError("Each shell command is limited to 4000 characters")
        normalized.append(item)
    if sum(len(item) for item in normalized) > 200_000:
        raise ValueError("The shell workflow is limited to 200000 characters")
    return workflow, normalized


def _shell_cwd(payload: dict[str, Any], settings: Settings) -> Path:
    return resolve_workspace_path(str(payload.get("cwd", ".")), settings.allowed_workspaces, must_exist=True)


def _powershell_executable() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def powershell_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    workflow, commands = _shell_commands(
        payload,
        settings,
        {
            "diagnostics": [
                "$PSVersionTable.PSVersion.ToString()",
                "Get-Location",
                "Get-Command python,node,wsl -ErrorAction SilentlyContinue | Select-Object Name,Source",
            ],
            "git_status": ["git status --short"],
            "list_directory": ["Get-ChildItem -Force -Name"],
        },
        "PowerShell",
    )
    cwd = _shell_cwd(payload, settings)
    script = "$ErrorActionPreference = 'Stop'\n" + "\n".join(commands)
    command = [
        _powershell_executable(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    return RunnerCommand(command, cwd, _bounded_timeout(payload, settings), f"PowerShell workflow: {workflow}")


def _cmd_executable() -> str:
    return os.environ.get("ComSpec") or shutil.which("cmd.exe") or "cmd.exe"


def cmd_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    workflow, commands = _shell_commands(
        payload,
        settings,
        {
            "diagnostics": ["ver", "cd", "where python", "where node", "where wsl"],
            "git_status": ["git status --short"],
            "list_directory": ["dir /a /b"],
        },
        "CMD",
    )
    cwd = _shell_cwd(payload, settings)
    # Pass one command string to cmd.exe. Using /s with embedded newlines can
    # make cmd.exe stop after the first command when launched by CreateProcess.
    script = " & ".join(commands)
    command = [_cmd_executable(), "/d", "/c", script]
    return RunnerCommand(command, cwd, _bounded_timeout(payload, settings), f"CMD workflow: {workflow}")


def terminal_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    shell = str(payload.get("shell", "powershell")).strip().lower()
    if shell == "powershell":
        return powershell_command(payload, settings, task_id)
    if shell in {"cmd", "command_prompt", "command-prompt"}:
        return cmd_command(payload, settings, task_id)
    if shell == "wsl":
        return wsl_command(payload, settings, task_id)
    raise ValueError("shell must be powershell, cmd, or wsl")


def project_build_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    cwd = resolve_workspace_path(str(payload.get("cwd", ".")), settings.allowed_workspaces, must_exist=True)
    build_type = str(payload.get("build_type", "python"))
    commands = {
        "python": [sys.executable, "-m", "compileall", "-q", str(cwd)],
        "frontend": ["npm", "run", "build"],
    }
    command = commands.get(build_type)
    if not command:
        raise ValueError("build_type must be python or frontend")
    return RunnerCommand(command, cwd, _bounded_timeout(payload, settings), f"Project build: {build_type}")


def md_simulation_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    script = payload.get("script_path")
    if not script:
        # The maintained entry point is stable, while its checkout lives in
        # the user's computational-biology workspace rather than beside the
        # MCP service.  Resolve it from configured workspaces so the web agent
        # does not have to guess a stale drive-root path.
        candidates: list[Path] = []
        relative_candidates = (
            Path("03_computational_biology_work") / "02_md" / "MDS" / "AUTO_file" / "main.py",
            Path("computational_biology_work") / "02_md" / "MDS" / "AUTO_file" / "main.py",
            Path("MDS") / "AUTO_file" / "main.py",
        )
        for workspace in settings.allowed_workspaces:
            candidates.extend((workspace / relative).resolve() for relative in relative_candidates)
        for candidate in candidates:
            try:
                resolved = resolve_workspace_path(str(candidate), settings.allowed_workspaces, must_exist=True)
            except (FileNotFoundError, ValueError):
                continue
            if resolved.is_file() and resolved.name.casefold() == "main.py":
                script = str(resolved)
                break
        if not script:
            raise ValueError(
                "run_md_simulation could not resolve AUTO_file/main.py; use find_paths "
                "to locate the maintained entry point and retry with its exact path"
            )
    # The maintained GLYCAM -> GROMACS entry point depends on the Linux
    # GROMACS/ACPYPE installation in WSL.  A web model may reasonably choose
    # the molecular-dynamics profile instead of spelling out the lower-level
    # WSL profile; route this one known entry point to WSL automatically so a
    # Windows service cannot report a misleading Python-only success.
    resolved_script = resolve_workspace_path(str(script), settings.allowed_workspaces, must_exist=True)
    normalized_parts = {part.casefold() for part in resolved_script.parts}
    if resolved_script.name.casefold() == "main.py" and "auto_file" in normalized_parts:
        cwd = resolve_workspace_path(str(payload.get("cwd", ".")), settings.allowed_workspaces, must_exist=True)
        linux_script = _windows_to_wsl(resolved_script)
        args = _normalize_md_glycan_arg(payload, settings, task_id)
        args = _remove_legacy_short_md_overrides(args, payload)
        command_text = "python3 " + shlex.quote(linux_script)
        if args:
            command_text += " " + shlex.join(args)
        return wsl_command(
            {
                "commands": [command_text],
                "cwd": str(cwd),
                # Never let the generic one-hour default kill a real
                # maintained EM/NVT/NPT run.  Explicit short validation is
                # still available only through the clearly named opt-in
                # payload flag used by tests.
                "timeout": _md_pipeline_timeout(payload, settings),
                "workflow": "glycam_gromacs_main",
            },
            settings,
            task_id,
        )
    return python_command({**payload, "script_path": script}, settings, task_id)


def _production_script_path(payload: dict[str, Any], settings: Settings) -> Path:
    """Resolve the user's existing recursive AUTO_file production driver."""

    requested = payload.get("script_path") or payload.get("production_script")
    if requested:
        script = resolve_workspace_path(_host_path_argument(str(requested)), settings.allowed_workspaces, must_exist=True)
        if not script.is_file() or script.suffix.casefold() != ".py":
            raise ValueError("production_script must be an existing Python file")
        return script

    relative_candidates = (
        Path("03_computational_biology_work") / "02_md" / "MDS" / "AUTO_file" / "automated_scripts" / "md.py",
        Path("computational_biology_work") / "02_md" / "MDS" / "AUTO_file" / "automated_scripts" / "md.py",
        Path("MDS") / "AUTO_file" / "automated_scripts" / "md.py",
    )
    for workspace in settings.allowed_workspaces:
        for relative in relative_candidates:
            candidate = workspace / relative
            try:
                resolved = resolve_workspace_path(str(candidate), settings.allowed_workspaces, must_exist=True)
            except (FileNotFoundError, ValueError):
                continue
            if resolved.is_file() and resolved.name.casefold() == "md.py":
                return resolved

    # The maintained checkout can be renamed or mounted at a different depth.
    # Search only for the distinctive production filename and prefer the
    # AUTO_file/automated_scripts path; this is bounded and does not guess a
    # different arbitrary Python entry point.
    candidates: list[Path] = []
    for workspace in settings.allowed_workspaces:
        try:
            candidates.extend(
                item
                for item in workspace.rglob("md.py")
                if item.is_file() and "auto_file" in {part.casefold() for part in item.parts}
                and "automated_scripts" in {part.casefold() for part in item.parts}
            )
        except OSError:
            continue
    candidates.sort(key=lambda item: (len(item.parts), str(item).casefold()))
    if candidates:
        return resolve_workspace_path(str(candidates[0]), settings.allowed_workspaces, must_exist=True)
    raise ValueError(
        "run_production_md could not find the existing recursive AUTO_file/automated_scripts/md.py; "
        "use find_paths/search_file to locate it and retry with script_path"
    )


def _production_system_dir(payload: dict[str, Any], settings: Settings) -> Path:
    """Resolve a fresh GLYCAM system that already passed NPT."""

    raw_system = payload.get("system_dir") or payload.get("system_root")
    raw_root = payload.get("target_root") or payload.get("cwd") or "."
    if raw_system:
        system = resolve_workspace_path(_host_path_argument(str(raw_system)), settings.allowed_workspaces, must_exist=True)
    else:
        root = resolve_workspace_path(_host_path_argument(str(raw_root)), settings.allowed_workspaces, must_exist=True)
        if root.is_dir() and root.name.casefold().startswith("glycam_system_"):
            system = root
        else:
            candidates = []
            search_roots = [root]
            if root.is_dir():
                # Production continuation must discover nested run folders;
                # checking only the target directory's immediate children
                # silently misses layouts such as 04_temp_file/temp_file/.
                try:
                    search_roots.extend(
                        item
                        for item in root.rglob("GLYCAM_system_*")
                        if item.is_dir()
                    )
                except OSError:
                    pass
            unique_search_roots = list(dict.fromkeys(search_roots))[:2_000]
            for candidate in unique_search_roots:
                core = candidate / "work_1" / "3_core_file"
                if all((core / name).is_file() for name in ("npt.gro", "npt.cpt", "npt.tpr", "index.ndx")):
                    candidates.append(candidate)
            if not candidates:
                raise FileNotFoundError(
                    f"No GLYCAM system with verified NPT inputs was found below {root}; "
                    "inspect the latest run directory and retry with system_dir"
                )
            if len(candidates) > 1:
                listed = ", ".join(str(item) for item in candidates[:20])
                raise ValueError(
                    "Multiple GLYCAM systems with NPT inputs were found below the target root; "
                    "refusing to choose one by modification time because that can reuse a prior "
                    "task. Pass the exact current system_dir selected from its successful "
                    f"run_manifest.json. Candidates: {listed}"
                )
            system = candidates[0]

    core = system / "work_1" / "3_core_file"
    processed = system / "work_1" / "1_processed_done"
    output = system / "work_1" / "4_output"
    required = [
        core / "npt.gro",
        core / "npt.cpt",
        core / "npt.tpr",
        core / "index.ndx",
        processed / "topol.top",
        system / "setting_file.json",
        system / "hardsoft.json",
        system / "run_manifest.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if not output.is_dir():
        missing.append(str(output))
    if missing:
        raise FileNotFoundError(
            "The selected GLYCAM system is not ready for the existing production script; missing: "
            + ", ".join(missing)
        )
    try:
        manifest = json.loads((system / "run_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"The selected GLYCAM system has no readable run_manifest.json: {system}") from exc
    stages = {str(item).casefold() for item in manifest.get("stages", [])}
    if str(manifest.get("status", "")).casefold() != "success" or "npt" not in stages:
        raise ValueError(
            "The selected GLYCAM system is not a verified successful NPT run; "
            "refusing to use residual or partial artifacts: " + str(system)
        )

    # An explicit path is not enough to establish task identity.  In a long
    # ChatGPT conversation the model can copy a valid-looking directory from
    # an earlier turn even though a newer setup has just completed.  Compare
    # the selected manifest with verified sibling systems under the same run
    # root and reject an older one.  This keeps explicit continuation possible
    # when it is the only verified run, while preventing silent reuse whenever
    # a newer task artifact exists.
    def manifest_epoch(candidate: Path, data: dict[str, Any]) -> float:
        raw = str(data.get("created_at") or "").strip()
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                pass
        try:
            return candidate.stat().st_mtime
        except OSError:
            return 0.0

    selected_epoch = manifest_epoch(system, manifest)
    search_roots: list[Path] = [system.parent]
    try:
        resolved_root = resolve_workspace_path(
            _host_path_argument(str(raw_root)), settings.allowed_workspaces, must_exist=True
        )
        if resolved_root.is_dir():
            search_roots.append(resolved_root)
    except (OSError, ValueError):
        pass
    sibling_candidates: list[tuple[Path, float]] = []
    for search_root in list(dict.fromkeys(search_roots)):
        try:
            if search_root.name.casefold().startswith("glycam_system_"):
                discovered = [search_root]
            else:
                discovered = [
                    item
                    for item in search_root.rglob("GLYCAM_system_*")
                    if item.is_dir()
                ][:2_000]
        except OSError:
            continue
        for candidate in discovered:
            try:
                candidate = candidate.resolve()
                if candidate == system.resolve() or not candidate.name.casefold().startswith("glycam_system_"):
                    continue
                candidate_core = candidate / "work_1" / "3_core_file"
                candidate_manifest_path = candidate / "run_manifest.json"
                if not all((candidate_core / name).is_file() for name in ("npt.gro", "npt.cpt", "npt.tpr", "index.ndx")):
                    continue
                candidate_manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
                candidate_stages = {str(item).casefold() for item in candidate_manifest.get("stages", [])}
                if str(candidate_manifest.get("status", "")).casefold() != "success" or "npt" not in candidate_stages:
                    continue
                sibling_candidates.append((candidate, manifest_epoch(candidate, candidate_manifest)))
            except (OSError, ValueError, TypeError):
                continue
    newer = [candidate for candidate, epoch in sibling_candidates if epoch > selected_epoch + 1.0]
    if newer:
        listed = ", ".join(str(item) for item in newer[:20])
        raise ValueError(
            "The selected GLYCAM system is older than a newer verified run; refusing "
            "to reuse prior task artifacts. Pass the exact newest system_dir from "
            f"its current run_manifest.json. Newer candidates: {listed}"
        )
    return system


def production_md_command(payload: dict[str, Any], settings: Settings, task_id: str) -> RunnerCommand:
    """Run the user's existing ``automated_scripts/md.py`` non-interactively.

    This profile deliberately does not reimplement the scientific workflow in
    the MCP.  It resolves the recursively discovered project driver, verifies
    that the current GLYCAM run has a real NPT checkpoint, passes a bounded
    wall-clock duration, and lets that driver produce/validate the artifacts.
    """

    system = _production_system_dir(payload, settings)
    script = _production_script_path(payload, settings)
    work_id = str(payload.get("work_id", "1")).strip() or "1"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", work_id):
        raise ValueError("work_id contains unsafe characters")
    try:
        duration = float(payload.get("duration_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ValueError("duration_seconds must be a number") from exc
    if not 1 <= duration <= 24 * 60 * 60:
        raise ValueError("duration_seconds must be between 1 and 86400")
    try:
        threads = int(payload.get("threads", payload.get("ntomp", 4)))
    except (TypeError, ValueError) as exc:
        raise ValueError("threads must be an integer") from exc
    if not 1 <= threads <= 256:
        raise ValueError("threads must be between 1 and 256")
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id)
    prefix = f"md_5min_{safe_task_id}"
    requested_prefix = str(payload.get("deffnm", "")).strip()
    if requested_prefix:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", requested_prefix):
            raise ValueError("deffnm contains unsafe characters")
        prefix = requested_prefix

    command_text = (
        "python3 "
        + shlex.quote(_windows_to_wsl(script))
        + " "
        + shlex.quote(work_id)
        + " --duration-seconds "
        + shlex.quote(f"{duration:g}")
        + " --deffnm "
        + shlex.quote(prefix)
        + " --ntomp "
        + shlex.quote(str(threads))
    )
    for key in ("mdp", "gpu"):
        if payload.get(key) is not None:
            command_text += " --" + ("mdp" if key == "mdp" else "gpu") + " " + shlex.quote(str(payload[key]))
    try:
        requested_timeout = int(payload.get("timeout", duration + 900))
    except (TypeError, ValueError):
        requested_timeout = int(duration + 900)
    return wsl_command(
        {
            "commands": [command_text],
            "cwd": str(system),
            "timeout": max(int(duration + 600), requested_timeout),
            "workflow": "production_md_existing_script",
        },
        settings,
        task_id,
    )