from __future__ import annotations

from pathlib import Path


class WorkspaceViolation(ValueError):
    pass


def resolve_workspace_path(path_value: str | Path, roots: list[Path], *, must_exist: bool = False) -> Path:
    raw = Path(path_value or ".")
    candidates: list[Path]
    if raw.is_absolute():
        candidates = [raw]
    else:
        candidates = [root / raw for root in roots]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        for root in roots:
            root_resolved = root.expanduser().resolve()
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                continue
            if must_exist and not resolved.exists():
                raise FileNotFoundError(str(resolved))
            return resolved
    raise WorkspaceViolation(f"Path is outside configured workspace: {path_value}")


def relative_display(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            return str(path.resolve().relative_to(root.resolve())) or "."
        except ValueError:
            continue
    return str(path)
