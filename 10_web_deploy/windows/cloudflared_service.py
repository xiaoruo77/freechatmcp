from __future__ import annotations

import shutil
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import win32serviceutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from service_common import ManagedProcessService, ROOT


cloudflared = shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"


class CloudflaredService(ManagedProcessService):
    _svc_name_ = "web_agent_cloudflared"
    _svc_display_name_ = "FreeChatMCP Cloudflare Tunnel"
    _svc_description_ = "Cloudflare Tunnel for the local FreeChatMCP service"
    _health_check_interval_seconds = 30.0
    _health_check_failures_before_restart = 6
    _health_check_timeout_seconds = 8.0
    _health_log_path = ROOT / "09_web_logs" / "cloudflared_watchdog.log"
    _metrics_url = "http://127.0.0.1:20241/metrics"
    _health_opener = build_opener(ProxyHandler({}))
    _proxy_environment_keys = {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    _environment = {
        # These values affect only the cloudflared child process.  They do not
        # modify Clash or the user's interactive shell/browser proxy.
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
        "no_proxy": "*",
    }
    _command_template = [
        cloudflared,
        "--config",
        str(ROOT / "10_web_deploy" / "cloudflared" / "config.yml"),
        "--loglevel",
        "info",
        "--logfile",
        str(ROOT / "09_web_logs" / "cloudflared.log"),
        "--edge-ip-version",
        "4",
        "--protocol",
        # QUIC/UDP 7844 fails on this network during the edge TLS handshake;
        # HTTP/2/TCP 7844 is the transport that successfully registers.
        "http2",
        "--metrics",
        "127.0.0.1:20241",
        "tunnel",
        "run",
        "codex",
    ]

    def _start_child(self):
        if self._child_env is not None:
            # Remove any machine-level proxy variables from this child only.
            # The user's proxy application and all other processes are left
            # untouched.
            for key in tuple(self._child_env):
                if key.lower() in self._proxy_environment_keys:
                    self._child_env.pop(key, None)
            self._child_env["NO_PROXY"] = "*"
            self._child_env["no_proxy"] = "*"
        # The current machine uses Meta's TUN as the working transport path.
        # Do not bind the child to WLAN: that bypasses Meta's fake-IP mapping
        # and sends cloudflared to unroutable 198.18.x.x addresses.
        self._command = list(self._command_template)
        super()._start_child()

    def _health_check(self) -> bool:
        # cloudflared owns edge reconnects and can legitimately report /ready
        # as 503 while it is retrying a blocked or changing network path. The
        # watchdog must not restart it merely for having zero edge
        # connections, because that resets cloudflared's backoff each time.
        # A missing/unreadable metrics endpoint still indicates a dead child.
        metrics_request = Request(
            self._metrics_url,
            headers={"User-Agent": "FreeChatMCP-cloudflared-watchdog/1.0"},
        )
        with self._health_opener.open(metrics_request, timeout=self._health_check_timeout_seconds) as response:
            metrics = response.read(256 * 1024).decode("utf-8", "replace")
        active_connections = None
        for line in metrics.splitlines():
            if line.startswith("cloudflared_tunnel_ha_connections "):
                try:
                    active_connections = float(line.rsplit(" ", 1)[1])
                except (TypeError, ValueError):
                    return False
                # Keep the variable parsed for validation and future logging;
                # zero is a transient edge state, not a dead process.
                return active_connections >= 0
        return False


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(CloudflaredService)