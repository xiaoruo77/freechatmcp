"""Resolve a task's Python runtime without launching interpreters or scanning disks."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from web_mcp.paths import WorkspaceViolation, resolve_workspace_path

if TYPE_CHECKING:
    from web_config.settings import Settings


_PYTHON_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", re.IGNORECASE)


def _jsonc(text: str) -> Any:
    """Remove JSONC comments/trailing commas while preserving quoted strings."""
    output: list[str] = []
    quoted = escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1:index + 2]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            output.append(char)
            index += 1
        elif char == "/" and following == "/":
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end
        elif char == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated JSONC comment")
            # Whitespace prevents comments from joining adjacent JSON tokens.
            output.append(" " + "\n" * text[index:end + 2].count("\n"))
            index = end + 2
        else:
            output.append(char)
            index += 1
    stripped = "".join(output)
    output = []
    quoted = escaped = False
    for index, char in enumerate(stripped):
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif char == ",":
            next_index = index + 1
            while next_index < len(stripped) and stripped[next_index].isspace():
                next_index += 1
            if next_index >= len(stripped) or stripped[next_index] not in "}]":
                output.append(char)
        else:
            output.append(char)
    return json.loads("".join(output))


def _read_config(path: Path, settings: Settings) -> dict[str, Any]:
    checked = resolve_workspace_path(path, settings.allowed_workspaces, must_exist=True)
    if not checked.is_file():
        raise ValueError(f"Python runtime configuration is not a file: {path}")
    if checked.stat().st_size > 1024 * 1024:
        raise ValueError(f"Python runtime configuration exceeds 1 MiB: {path}")
    try:
        data = _jsonc(checked.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Unable to read Python runtime configuration: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Python runtime configuration must be an object: {path}")
    return data


def _interpreter(
    value: Any,
    settings: Settings,
    source: str,
    *,
    project: Path | None = None,
) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source} Python interpreter must be a non-empty path")
    raw = value.strip()
    if any(char in raw for char in ("\x00", "\n", "\r")) or raw.startswith(('"', "'")):
        raise ValueError(f"{source} Python interpreter must be a path, not a shell command")
    project_relative = project is not None and (
        not Path(raw).is_absolute() or "${workspaceFolder}" in raw
    )
    if project is not None:
        raw = raw.replace("${workspaceFolder}", str(project))
    if "${" in raw:
        raise ValueError(f"{source} Python interpreter contains an unsupported variable")
    candidate = Path(raw)
    if not candidate.is_absolute():
        if project is None:
            raise ValueError(f"{source} Python interpreter requires an absolute path")
        candidate = project / candidate
    checked = resolve_workspace_path(candidate, settings.allowed_workspaces, must_exist=True)
    if project_relative:
        try:
            checked.relative_to(project)
        except ValueError:
            raise WorkspaceViolation(f"{source} relative Python interpreter escapes its project: {value}")
    if checked.is_dir():
        names = ("Scripts/python.exe", "bin/python", "bin/python3") if os.name == "nt" else (
            "bin/python", "bin/python3", "Scripts/python.exe",
        )
        for name in names:
            target = checked / name
            if target.exists():
                checked = resolve_workspace_path(target, settings.allowed_workspaces, must_exist=True)
                break
        else:
            raise ValueError(f"{source} Python environment has no interpreter: {checked}")
    if not checked.is_file() or not _PYTHON_NAME.fullmatch(checked.name):
        raise ValueError(f"{source} Python interpreter is not a Python executable: {checked}")
    if os.name != "nt" and not os.access(checked, os.X_OK):
        raise ValueError(f"{source} Python interpreter is not executable: {checked}")
    return {"executable": str(checked), "source": source}


def _project_ancestors(cwd: Path, settings: Settings) -> list[Path]:
    current = resolve_workspace_path(cwd, settings.allowed_workspaces, must_exist=True)
    if not current.is_dir():
        raise ValueError(f"Python task cwd is not a directory: {current}")
    ancestors = []
    while True:
        ancestors.append(current)
        if current.parent == current:
            break
        try:
            current = resolve_workspace_path(current.parent, settings.allowed_workspaces, must_exist=True)
        except WorkspaceViolation:
            break
    return ancestors


def resolve_python_runtime(payload: dict[str, Any], settings: Settings, cwd: Path) -> dict[str, str]:
    """Select explicit, project, configured, then service Python in that order.

    All newly selected paths must be inside allowed workspaces. The already
    running service interpreter is a trusted compatibility fallback and can
    also be explicitly requested with ``python_executable="service"``.
    """
    ancestors = _project_ancestors(cwd, settings)
    if "python_executable" in payload and payload["python_executable"] is not None:
        value = payload["python_executable"]
        if value == "service":
            return {"executable": sys.executable, "source": "service"}
        return _interpreter(value, settings, "explicit")

    service_root = settings.project_root.resolve()
    projects = [project for project in ancestors if project != service_root]
    for project in projects:
        config = project / ".vscode" / "settings.json"
        if not config.exists() and not config.is_symlink():
            continue
        data = _read_config(config, settings)
        for key in ("python.defaultInterpreterPath", "python.pythonPath"):
            if key in data:
                return _interpreter(data[key], settings, "vscode", project=project)

    for project in projects:
        for name in (".venv", "venv"):
            environment = project / name
            if environment.exists() or environment.is_symlink():
                return _interpreter(str(environment), settings, "project_venv")

    override = (getattr(settings, "extra", None) or {}).get("python_runtime_config")
    config = Path(override) if override is not None else (
        settings.project_root / "07_web_config" / "python_runtime.json"
    )
    if config.exists() or config.is_symlink():
        data = _read_config(config, settings)
        result = _interpreter(data.get("default_python_executable"), settings, "configured_default")
        if "default_python_user_base" in data:
            value = data["default_python_user_base"]
            if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
                raise ValueError("default_python_user_base requires an absolute directory path")
            user_base = resolve_workspace_path(value, settings.allowed_workspaces, must_exist=True)
            if not user_base.is_dir():
                raise ValueError(f"default_python_user_base is not a directory: {user_base}")
            result["user_base"] = str(user_base)
        return result
    return {"executable": sys.executable, "source": "service"}
