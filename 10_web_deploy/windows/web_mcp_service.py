from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import win32serviceutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from service_common import ManagedProcessService, PYTHON, ROOT


class WebMcpService(ManagedProcessService):
    _svc_name_ = "web_mcp"
    _svc_display_name_ = "Xiaoruo1 MCP API"
    _svc_description_ = "Local Xiaoruo1 FastAPI and Streamable HTTP MCP service"
    _health_check_interval_seconds = 15.0
    _health_check_failures_before_restart = 3
    _health_check_timeout_seconds = 3.0
    _health_log_path = ROOT / "09_web_logs" / "web_mcp_watchdog.log"
    _health_url = "http://127.0.0.1:48271/health"
    # Never hard-code real domains, wide file access or privileged settings.
    _environment = {
        "WEB_AGENT_DISPLAY_NAME": "Xiaoruo1",
        "WEB_AGENT_OAUTH_ENABLED": os.getenv("WEB_AGENT_OAUTH_ENABLED", "0"),
        "WEB_AGENT_PUBLIC_URL": os.getenv("WEB_AGENT_PUBLIC_URL", ""),
        "WEB_AGENT_OAUTH_ISSUER_URL": os.getenv("WEB_AGENT_OAUTH_ISSUER_URL", ""),
        "WEB_AGENT_MCP_RESOURCE_URL": os.getenv("WEB_AGENT_MCP_RESOURCE_URL", ""),
        "WEB_AGENT_WORKSPACE": os.getenv("WEB_AGENT_WORKSPACE", str(ROOT)),
        "WEB_AGENT_WORKSPACES": os.getenv("WEB_AGENT_WORKSPACES", str(ROOT)),
        "WEB_AGENT_ADMIN_MODE": os.getenv("WEB_AGENT_ADMIN_MODE", "false"),
        "WEB_AGENT_ALLOW_CUSTOM_SHELL_WORKFLOW": os.getenv("WEB_AGENT_ALLOW_CUSTOM_SHELL_WORKFLOW", "0"),
        "WEB_AGENT_WSL_USER_CONTEXT": os.getenv("WEB_AGENT_WSL_USER_CONTEXT", "0"),
    }
    _command = [str(PYTHON), str(ROOT / "run_server.py")]

    def _health_check(self) -> bool:
        request = Request(self._health_url, headers={"User-Agent": "Xiaoruo1-web-mcp-watchdog/1.0"})
        with urlopen(request, timeout=self._health_check_timeout_seconds) as response:
            return response.status == 200 and b'"status"' in response.read(2048)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(WebMcpService)