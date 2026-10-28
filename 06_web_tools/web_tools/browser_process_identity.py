"""Identify an authenticated loopback extension peer by its existing process.

No browser is started and no profile, cookie store, or credential is read. If
Windows denies process/network inspection, the peer remains unclassified.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import uuid

XIAORUO_EXE = Path(os.getenv("WEB_AGENT_BROWSER_EXE", str(Path.home() / "FreeChatMCP" / "xiaoruo-browser.exe")))
LEGACY_ACTIONS = ["status", "list_tabs", "open_tab", "close_tab", "navigate", "read", "click", "type", "press", "select", "wait", "screenshot", "downloads"]


def _endpoint(value):
    if not value:
        return None
    if hasattr(value, "ip") and hasattr(value, "port"):
        return str(value.ip), int(value.port)
    return str(value[0]), int(value[1])


def identify_existing_browser_peer(host: str, port: int, *, psutil_api=None) -> dict[str, Any] | None:
    if host not in {"127.0.0.1", "::1"} or type(port) is not int or not 1 <= port <= 65535:
        return None
    try:
        if psutil_api is None:
            import psutil as psutil_api
        candidates = set()
        for item in psutil_api.net_connections(kind="tcp"):
            local, remote = _endpoint(item.laddr), _endpoint(item.raddr)
            if (local == (host, port) and remote and remote[0] in {"127.0.0.1", "::1"}
                    and remote[1] == 48272 and item.pid and item.status == "ESTABLISHED"):
                candidates.add(int(item.pid))
        if len(candidates) != 1:
            return None
        peer = psutil_api.Process(next(iter(candidates)))
        chain = [peer, *peer.parents()[:20]]
        exact = []
        expected = os.path.normcase(str(XIAORUO_EXE)).casefold()
        for process in chain:
            if os.path.normcase(str(process.exe())).casefold() == expected:
                exact.append(process)
        if not exact:
            return None
        root = exact[-1]
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, expected + "|" + str(root.pid) + "|" + repr(root.create_time())))
        argv = root.cmdline()
        automated = any(str(argument).casefold().startswith("--mcp-stdio") for argument in argv)
        return {"instance_id": identity, "browser_kind": "xiaoruo_browser",
            "browser_family": "chromium-other", "identity_source": "verified_loopback_process",
            "manual_instance": not automated, "process_id": root.pid,
            "supported_actions": list(LEGACY_ACTIONS), "engine": "cef",
            "limitations": ["Legacy extension: no scoped frames/shadow actions, scroll or debugger advertised", "Identity lasts for this existing process; browser restart requires fresh discovery"]}
    except Exception:
        # AccessDenied, disappearing processes, unsupported endpoints and
        # ambiguous ancestry never turn into a guessed browser identity.
        return None