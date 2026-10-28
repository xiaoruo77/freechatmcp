"""Date-scoped video project paths, separate from MCP source and data.

This module only resolves/optionally creates a directory. It never generates,
moves, overwrites or removes media and never starts an external application.
"""
from __future__ import annotations

import os
import re
from datetime import date as CalendarDate, datetime, timedelta, timezone
from pathlib import Path


VIDEO_ROOT = Path(os.getenv("WEB_AGENT_VIDEO_ROOT", str(Path.home() / "Videos" / "FreeChatMCP")))
MCP_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_REPARSE_POINT = 0x400


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _day(value: str | None) -> CalendarDate:
    if value is None:
        return datetime.now(SHANGHAI).date()
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("date must use YYYY-MM-DD")
    try:
        return CalendarDate.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc


def video_project_path(date: str | None = None, create: bool = False) -> dict[str, object]:
    """Return an authorized ``yyyy_MM/dd`` path; create it only on request.

    The public API accepts no arbitrary base directory. Existing links/junctions
    along the owned root/date hierarchy are rejected before directory creation.
    """
    day = _day(date)
    if type(create) is not bool:
        raise ValueError("create must be a boolean")
    root = VIDEO_ROOT
    month = root / f"{day.year:04d}_{day.month:02d}"
    target = month / f"{day.day:02d}"
    # Check the configured root and each parent too: an ancestor junction can
    # redirect an otherwise innocent-looking dated path into an unsafe tree.
    for part in (root, *root.parents, month, target):
        if part.is_symlink():
            raise ValueError("video project paths cannot use links or reparse points")
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & _REPARSE_POINT:
            raise ValueError("video project paths cannot use links or reparse points")
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not _inside(resolved_target, resolved_root):
        raise ValueError("video project path escaped the authorized root")
    forbidden = MCP_ROOT.resolve()
    if _inside(resolved_root, forbidden) or _inside(resolved_target, forbidden):
        raise ValueError("video projects cannot be stored inside the MCP project")
    if root.exists() and not root.is_dir():
        raise ValueError("video root is not a directory")
    if target.exists() and not target.is_dir():
        raise ValueError("video project target is not a directory")
    existed = target.is_dir()
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return {
        "path": str(resolved_target),
        "date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "exists": target.is_dir(),
        "created": create and not existed,
    }