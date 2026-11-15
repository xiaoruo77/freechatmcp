"""Preserve UU's shared user receiver and reject the legacy private-client route.

An idle homepage or tray client is still the user's permanent incoming-control
receiver. There is no verified independent controller instance in this route,
so neither an existing GUI nor its absence authorizes private replacement.
"""
from __future__ import annotations

import math
from pathlib import Path


def _result(code: str, message: str, pids=(), **extra) -> dict:
    return {"ok": False, "error_code": code, "message": message,
            "ready_for_private_launch": False, "handoff_performed": False,
            "closed_pids": [], "preserved_gui_pids": sorted(set(pids)),
            "preserved_service": True, "service_processes_touched": False,
            "foreground_unchanged": True, "foreground_state_modified": False,
            "waited_for_user_focus": False, "host_input_used": False,
            "foreground_input_used": False, **extra}


def prepare_idle_client(executable, cli_status, private_windows, timeout=10) -> dict:
    """Keep the shared receiver intact; do not query, close, or replace its UI.

    The callbacks remain in the interface for compatibility but are deliberately
    not invoked: current connection state does not make a permanent receiver
    disposable. This legacy private-GUI route must not report launch readiness.
    """
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 25:
        return _result("uu_handoff_timeout_invalid", "Use a finite handoff timeout between 0 and 25 seconds")
    try:
        launcher = Path(executable)
        if not launcher.is_absolute() or launcher.name.casefold() != "gameviewer.exe":
            raise ValueError("invalid executable")
        launcher = launcher.resolve()
        gui = (launcher.parent / "bin" / "GameViewer.exe").resolve()
        if not launcher.is_file() or not gui.is_file():
            raise ValueError("missing executable")
    except (OSError, TypeError, ValueError):
        return _result("uu_handoff_executable_invalid", "Use the existing installed UU root GameViewer.exe and its bin client")

    paths = {str(launcher).casefold(), str(gui).casefold()}
    preserved = []
    identity_unknown = False
    try:
        import psutil
        for process in psutil.process_iter(["pid", "exe", "name"]):
            try:
                info = process.info
                executable = info.get("exe")
                pid = info.get("pid", process.pid)
                if type(pid) is not int or pid <= 0:
                    continue
                if executable:
                    if str(Path(executable).resolve()).casefold() in paths:
                        preserved.append(pid)
                elif str(info.get("name", "")).casefold() == "gameviewer.exe":
                    # An unreadable GUI identity never authorizes a replacement.
                    preserved.append(pid)
                    identity_unknown = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError, TypeError, ValueError):
                identity_unknown = True
    except Exception:
        return _result("uu_receiver_state_unknown", "UU receiver identity could not be inspected; no client was closed or launched", preserved)

    if preserved:
        return _result("uu_shared_receiver_preserved",
            "The existing UU client is a shared incoming-control receiver and was preserved. A verified independent outgoing controller is required before background use",
            preserved, identity_verified=not identity_unknown)
    return _result("uu_receiver_isolation_required",
        "No verified independent UU controller is available. This legacy route cannot create a private client that replaces or disrupts the local incoming-control receiver",
        identity_verified=not identity_unknown)
