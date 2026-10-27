from __future__ import annotations

import os
import sys
from pathlib import Path

import win32serviceutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from service_common import ManagedProcessService, PYTHON, ROOT


class WebWorkerService(ManagedProcessService):
    _svc_name_ = "web_worker"
    _svc_display_name_ = "FreeChatMCP Worker"
    _svc_description_ = "FreeChatMCP SQLite task queue worker"
    # Published defaults are local-only; configure optional remote features yourself.
    _environment = {
        "WEB_AGENT_DISPLAY_NAME": "FreeChatMCP",
        "WEB_AGENT_WORKSPACE": os.getenv("WEB_AGENT_WORKSPACE", str(ROOT)),
        "WEB_AGENT_WORKSPACES": os.getenv("WEB_AGENT_WORKSPACES", str(ROOT)),
        "WEB_AGENT_ADMIN_MODE": os.getenv("WEB_AGENT_ADMIN_MODE", "false"),
        "WEB_AGENT_ALLOW_CUSTOM_SHELL_WORKFLOW": os.getenv("WEB_AGENT_ALLOW_CUSTOM_SHELL_WORKFLOW", "0"),
    }
    _command = [str(PYTHON), str(ROOT / "run_worker.py")]


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(WebWorkerService)