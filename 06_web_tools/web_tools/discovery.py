"""Bounded, read-only discovery for the Xiaoruo1 compatibility surface."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


_IGNORED_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "browser_profile",
    "task_outputs",
    "generated_scripts",
}
_DOC_NAMES = {"readme", "usage", "install", "guide", "manual", "instructions"}
_SCRIPT_SUFFIXES = {".py", ".ps1", ".sh", ".bash", ".js", ".cmd", ".bat"}
_SENSITIVE = re.compile(
    r"(?i)(token|secret|password|authorization|cookie|api[_ -]?key)\s*([=:])\s*([^\s,;&]+)"
)


def _redact(text: str) -> str:
    return _SENSITIVE.sub(r"\1\2[REDACTED]", str(text))


def _inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _safe_roots(settings: Any, cwd: str | None, roots: list[str] | None) -> list[Path]:
    raw_roots = roots or ([cwd] if cwd else [str(settings.workspace_root)])
    result: list[Path] = []
    for raw in raw_roots:
        if not raw:
            continue
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = settings.workspace_root / candidate
        candidate = candidate.resolve()
        if _inside(candidate, settings.allowed_workspaces):
            result.append(candidate)
    return list(dict.fromkeys(result)) or [settings.workspace_root.resolve()]


def _candidate_priority(path: Path, request: str, *, document: bool = False) -> int:
    """Rank useful project assets before returning a bounded discovery result.

    A recursive walk can find thousands of files.  Returning the first files
    yielded by ``os.walk`` made a real nested driver such as
    ``AUTO_file/automated_scripts/md.py`` disappear behind backups, caches and
    unrelated training artifacts.  Discovery remains bounded, but the result
    is now biased toward executable entry points and files matching the
    requested operation.
    """

    lowered = str(path).replace("\\", "/").casefold()
    name = path.name.casefold()
    request_text = str(request or "").casefold()
    score = 0
    if not document:
        if name == "main.py":
            score += 300
        if name in {"md.py", "nvt.py", "npt.py", "em.py"}:
            score += 260
        if any(token in name for token in ("production", "simulate", "simulation", "mdrun")):
            score += 220
        if "/automated_scripts/" in lowered or "/group_scripts/" in lowered:
            score += 140
        if "/tool_file/" in lowered:
            score += 40
        if any(token in request_text for token in ("md", "gromacs", "分子动力学", "production", "模拟")):
            if any(token in lowered for token in ("md", "gromacs", "simulation", "automated_scripts")):
                score += 180
    else:
        if name in {"readme.md", "usage.md", "install.md", "guide.md", "manual.md", "instructions.md"}:
            score += 100
    # Prefer shallower paths only after the semantic/entry-point score.  This
    # keeps a nested real driver ahead of a shallow but irrelevant document.
    return score


def _walk_candidates(
    roots: list[Path],
    limit: int,
    request: str = "",
    max_depth: int = 32,
) -> tuple[list[Path], list[Path], dict[str, Any]]:
    """Recursively collect candidates without the old one-project-depth trap.

    ``discover_environment`` is intentionally bounded for a drive-level root,
    but the old hard-coded depth of eight was silently truncating legitimate
    nested repositories.  The walk now uses an explicit depth/entry/time
    budget and reports those bounds to the caller instead of pretending that a
    partial scan was complete.
    """

    docs: list[Path] = []
    scripts: list[Path] = []
    directories_seen = 0
    files_seen = 0
    truncated = False
    deadline = time.monotonic() + 30.0
    bounded_depth = max(0, min(int(max_depth), 64))
    bounded_limit = max(1, min(int(limit), 1_000))
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            directories_seen += 1
            if time.monotonic() >= deadline or directories_seen >= 20_000 or files_seen >= 100_000:
                truncated = True
                break
            current_path = Path(current)
            try:
                relative_parts = current_path.relative_to(root).parts
            except ValueError:
                continue
            dirs[:] = [item for item in dirs if item not in _IGNORED_PARTS and item != "migration_backups"]
            if len(relative_parts) >= bounded_depth:
                dirs[:] = []
                if len(relative_parts) >= bounded_depth:
                    truncated = True
            for name in files:
                files_seen += 1
                path = current_path / name
                lowered = name.casefold()
                if path.suffix.casefold() in {".md", ".txt", ".rst"} or lowered.split(".", 1)[0] in _DOC_NAMES:
                    docs.append(path)
                if path.suffix.casefold() in _SCRIPT_SUFFIXES or lowered.startswith(("run", "start", "prepare", "workflow", "main")):
                    scripts.append(path)
            if len(docs) >= bounded_limit and len(scripts) >= bounded_limit:
                truncated = True
                break
        if truncated and (time.monotonic() >= deadline or directories_seen >= 20_000 or files_seen >= 100_000):
            break

    def order(items: list[Path], document: bool) -> list[Path]:
        unique = list(dict.fromkeys(items))
        unique.sort(
            key=lambda item: (
                -_candidate_priority(item, request, document=document),
                len(item.parts),
                str(item).casefold(),
            )
        )
        return unique[:bounded_limit]

    return order(docs, True), order(scripts, False), {
        "recursive": True,
        "max_depth": bounded_depth,
        "directories_seen": directories_seen,
        "files_seen": files_seen,
        "truncated": truncated,
    }


def _read_sources(paths: list[Path], limit: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in paths[:12]:
        try:
            content = _redact(path.read_text(encoding="utf-8", errors="replace")[:limit])
            sources.append({"path": str(path), "read": True, "chars": len(content), "content": content})
        except OSError as exc:
            sources.append({"path": str(path), "read": False, "chars": 0, "content": "", "error": str(exc)[:500]})
    return sources


def _wsl_probe(distribution: str | None) -> dict[str, Any]:
    executable = shutil.which("wsl.exe") or shutil.which("wsl")
    if not executable:
        return {"available": False, "distribution": distribution, "error": "wsl.exe not found"}
    try:
        completed = subprocess.run(
            [executable, "--list", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        names = [line.strip().replace("\x00", "") for line in output.splitlines() if line.strip()]
        return {
            "available": completed.returncode == 0 and bool(names),
            "distribution": distribution,
            "distributions": names[:30],
            "returncode": completed.returncode,
            "error": _redact(output[-1_000:]) if completed.returncode != 0 else "",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "distribution": distribution, "error": str(exc)[:500]}


def discover_environment(
    settings: Any,
    request: str = "",
    cwd: str | None = None,
    distribution: str | None = None,
    roots: list[str] | None = None,
    max_results: int = 120,
    read_limit: int = 6_000,
    max_depth: int = 32,
) -> dict[str, Any]:
    """Inspect the current Windows project and runtime without executing scripts."""

    bounded_results = max(20, min(int(max_results), 300))
    bounded_read = max(500, min(int(read_limit), 12_000))
    selected_roots = _safe_roots(settings, cwd, roots)
    docs, scripts, scan = _walk_candidates(selected_roots, bounded_results, request, max_depth)
    executable_names = ("python", "powershell", "pwsh", "node", "npm", "git", "wsl.exe", "cloudflared")
    executables = {name: shutil.which(name) for name in executable_names if shutil.which(name)}
    bridge_url = str(getattr(settings, "browser_bridge_url", ""))
    return {
        "success": True,
        "terminal": True,
        "read_only": True,
        "request": str(request)[:2_000],
        "cwd": str(cwd or settings.workspace_root),
        "roots": [str(item) for item in selected_roots],
        "identity": {"platform": platform.platform(), "python": sys.executable, "user": os.environ.get("USERNAME", "")},
        "runtime": {"project_root": str(settings.project_root), "workspace_root": str(settings.workspace_root), "mcp_port": settings.port, "browser_bridge": bridge_url},
        "executables": executables,
        "instruction_candidates": [str(item) for item in docs],
        "script_candidates": [str(item) for item in scripts],
        # Read more than the old four-script slice.  The first shallow files
        # are not sufficient evidence for a nested automation project; the
        # prioritized list puts md.py/nvt.py/npt.py ahead of backups while
        # preserving a bounded response size.
        "sources_read": _read_sources(docs[:6] + scripts[:12], bounded_read),
        "scan": scan,
        "wsl": _wsl_probe(distribution or settings.wsl_distribution),
        "next": "Use the returned files and runtime facts to construct the next bounded workflow; discovery made no changes.",
    }