"""Local desktop routing preference and explicit foreground availability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_MAX_CONFIG_BYTES = 16 * 1024
FOREGROUND_REQUEST_MESSAGE = (
    "当前默认使用后台桌面；本次使用前台需要明确指定 execution_mode=foreground，"
    "并在用户授权后设置 confirm=true。"
)
ISOLATION_MESSAGE = (
    "主桌面 GUI 已禁用；当前尚未配置适用的本机后台应用接口或独立后台桌面后端，"
    "主桌面不可占用，不会自动回退到当前用户桌面。文件、Python、Notebook 和受限的浏览器 "
    "native_background 操作仍可在后台使用；本机 VS Code 接口、Word COM 是待配置的应用后台候选。"
)


class DesktopPolicyError(ValueError):
    """A local policy cannot be interpreted safely; no desktop transport may run."""


def _policy(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "host_input_allowed": mode == "interactive",
        "foreground_available_on_request": mode in {"interactive", "background_preferred"},
        "isolated_desktop_ready": False,
        "backend": "interactive" if mode == "interactive" else "unconfigured",
        "background_alternatives": [
            "file", "python", "notebook", "native_background_browser",
            "vscode_api", "word_com",
        ],
        "pending_application_backends": {
            "vscode_api": {"available": False, "status": "unconfigured"},
            "word_com": {"available": False, "status": "unconfigured"},
        },
    }


def load_desktop_policy(settings: Any) -> dict[str, Any]:
    """Read one bounded local configuration, defaulting only missing files.

    An optional trusted Settings.extra override isolates test/deployment roots.
    No backend declaration is treated as proof that an isolated desktop exists.
    """
    override = (getattr(settings, "extra", None) or {}).get("desktop_policy_config")
    root = getattr(settings, "project_root", None)
    if override is None and root is None:
        return _policy("isolated_only")
    path = Path(override) if override is not None else Path(root) / "07_web_config" / "desktop_policy.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        if path.is_symlink():
            raise DesktopPolicyError("Desktop policy configuration target is unavailable; host desktop access is blocked")
        return _policy("isolated_only")
    except OSError as exc:
        raise DesktopPolicyError("Desktop policy configuration cannot be read; host desktop access is blocked") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise DesktopPolicyError("Desktop policy configuration exceeds its size limit; host desktop access is blocked")
    try:
        config = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise DesktopPolicyError("Desktop policy configuration is invalid; host desktop access is blocked") from exc
    mode = config.get("mode") if isinstance(config, dict) else None
    if not isinstance(mode, str) or mode not in {"interactive", "isolated_only", "background_preferred"}:
        raise DesktopPolicyError("Desktop policy mode must be interactive, isolated_only or background_preferred; host desktop access is blocked")
    return _policy(mode)


def isolated_control_status(policy: dict[str, Any]) -> dict[str, Any]:
    """An honest local status, without connecting to the interactive desktop."""
    return {
        "ok": True,
        "status": "completed",
        "terminal": True,
        "task_id": None,
        "operation": "windows_controlstatus",
        "result": {
            "summary": FOREGROUND_REQUEST_MESSAGE if policy["mode"] == "background_preferred" else ISOLATION_MESSAGE,
            "data": {"state": "isolated_unavailable", **policy},
        },
        "desktop_policy": policy,
        "artifacts": [],
        "error": None,
        "error_code": None,
        "retryable": False,
        "evidence": {
            "tool": "ControlStatus",
            "transport": "local_policy",
            "checks": ["desktop_policy_loaded"],
            "action_may_have_run": False,
            "verification_required": False,
        },
    }
