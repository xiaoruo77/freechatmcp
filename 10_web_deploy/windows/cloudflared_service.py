from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import win32serviceutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from service_common import ManagedProcessService, ROOT
from tunnel_health import TunnelObservation, TunnelReadinessPolicy, observe_metrics


cloudflared = shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"


class CloudflaredService(ManagedProcessService):
    _svc_name_ = "web_agent_cloudflared"
    _svc_display_name_ = "Xiaoruo1 Cloudflare Tunnel"
    _svc_description_ = "Cloudflare Tunnel for the local Xiaoruo1 service"
    _health_check_interval_seconds = 30.0
    # Readiness policy below opens the parent's restart gate only when due.
    _health_check_failures_before_restart = sys.maxsize
    _health_check_timeout_seconds = 8.0
    _readiness_grace_seconds = 180.0
    _readiness_restart_cooldown_seconds = 900.0
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

    def __init__(self, args):
        super().__init__(args)
        self._readiness = TunnelReadinessPolicy(
            self._readiness_grace_seconds, self._readiness_restart_cooldown_seconds
        )
        self._child_has_started = False
        self._health_check_failures_before_restart = sys.maxsize

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
        self._readiness.child_started(time.monotonic(), restarted=self._child_has_started)
        self._child_has_started = True
        self._health_check_failures_before_restart = sys.maxsize

    def _health_check(self) -> bool:
        # Report readiness honestly even while the metrics process is alive.
        # Only sustained loss permits a restart; the cooldown preserves the
        # child's reconnect backoff during a prolonged network outage.
        metrics_request = Request(
            self._metrics_url,
            headers={"User-Agent": "Xiaoruo1-cloudflared-watchdog/1.0"},
        )
        try:
            with self._health_opener.open(metrics_request, timeout=self._health_check_timeout_seconds) as response:
                metrics = response.read(256 * 1024).decode("utf-8", "replace")
            observation = observe_metrics(metrics)
        except Exception:
            # Do not put request details or raw exception text in service logs.
            observation = TunnelObservation(False, False, "metrics_unreachable")
        decision = self._readiness.evaluate(observation, time.monotonic())
        for event in decision.events:
            self._write_health_log(event)
        # The parent reads this threshold after every False result. Keep the
        # result False during the grace/cooldown, but inhibit its restart branch.
        # A crashed/exited child still follows the parent's separate retry path.
        self._health_check_failures_before_restart = 1 if decision.restart_allowed else sys.maxsize
        return decision.ready


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(CloudflaredService)