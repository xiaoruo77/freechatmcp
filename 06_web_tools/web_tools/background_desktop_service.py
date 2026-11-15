"""Authenticated loopback service for the same-machine non-input desktop.

Runs in the logged-in user's session. Only BackgroundDesktopHost owns UI
objects; request threads never inject keyboard/mouse input or activate windows.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError, URLError

from web_tools.background_desktop_host import BackgroundDesktopHost

MAX_BODY = 2 * 1024 * 1024


def serve(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    port = int(config["port"])
    if port != 48911:
        raise ValueError("The background desktop service uses its dedicated loopback port")
    idle_timeout=config.get('idle_timeout_seconds',120)
    if type(idle_timeout) is not int or not 5<=idle_timeout<=3600:
        raise ValueError('Idle timeout must be an integer from 5 to 3600 seconds')
    token_path = Path(config["token_file"])
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("A private background desktop credential is required")
    desktop = BackgroundDesktopHost(data_root=Path(config["data_root"]),
                                    allowed_apps={name: Path(path) for name, path in config["allowed_apps"].items()})
    opener = build_opener(ProxyHandler({}))
    last_activity=[time.monotonic()]
    active_requests=[0]
    activity_lock=threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never copy request headers, tokens or document content to logs.

        def reply(self, status: int, value: dict):
            data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.client_address[0] != "127.0.0.1" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.reply(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path not in {"/request", "/vscode"}:
                self.reply(404, {"ok": False, "error": "not_found"})
                return
            with activity_lock:
                active_requests[0]+=1
                last_activity[0]=time.monotonic()
            try:
                last_activity[0]=time.monotonic()
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("Request size is outside the supported limit")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
                    raise ValueError("An explicit action is required")
                if self.path == "/vscode":
                    # An explicit workspace request may start its dedicated
                    # installed editor on demand. A status read never launches.
                    if payload["action"] != "status":
                        probe = Request("http://127.0.0.1:48912/status", headers={"Authorization": "Bearer " + token})
                        try:
                            with opener.open(probe, timeout=1) as response:
                                response.read(64)
                        except URLError as exc:
                            if isinstance(exc, HTTPError):
                                raise
                            desktop.execute("create", {})
                            launched = desktop.execute("launch_app", {"app": "vscode", "timeout": 10})
                            if launched.get("ok") is not True:
                                raise ValueError("The dedicated editor could not start; host desktop was not used")
                            deadline = time.monotonic() + 10
                            while True:
                                try:
                                    with opener.open(probe, timeout=1) as response:
                                        response.read(64)
                                    break
                                except URLError:
                                    if time.monotonic() >= deadline:
                                        raise ValueError("The dedicated editor API is not ready; host desktop was not used") from None
                                    time.sleep(0.25)
                    # One fixed loopback application endpoint, never a caller URL.
                    status_request = payload["action"] == "status"
                    request = Request("http://127.0.0.1:48912/" + ("status" if status_request else "request"),
                        method="GET" if status_request else "POST",
                        data=None if status_request else json.dumps(payload).encode("utf-8"), headers={
                            "Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    with opener.open(request, timeout=20) as response:
                        data = response.read(4 * 1024 * 1024 + 1)
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("Application response exceeds the supported limit")
                    value = json.loads(data)
                    if isinstance(value, dict) and value.get("ok") is True and "result" in value:
                        value = value["result"]
                else:
                    action = payload["action"]
                    arguments = payload.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    if action not in {"status", "create", "list_apps", "list_windows", "launch_app", "snapshot", "screenshot", "edge", "wps", "uu", "todesk", "cleanup", "click", "type", "selection", "scroll", "toggle", "expand", "collapse", "set_range"}:
                        raise ValueError("This action is unavailable on the non-input desktop")
                    if action not in {"status", "create"}:
                        desktop.execute("create", {})
                    value = desktop.execute(action, arguments)
                if isinstance(value, dict) and value.get("ok") is False:
                    self.reply(200, {"ok": False, "backend": "local_background", "host_input_used": False,
                                     "error": value, "result": value})
                else:
                    self.reply(200, {"ok": True, "backend": "local_background", "host_input_used": False, "result": value})
                if self.path=="/request" and payload["action"]=="cleanup" and value.get("cleanup_complete"):
                    threading.Thread(target=server.shutdown,daemon=True).start()
            except HTTPError as exc:
                # Application errors are bounded JSON; never expose auth headers.
                try:
                    value = json.loads(exc.read(32768))
                except Exception:
                    value = {"error": "Application request failed"}
                self.reply(200, {"ok": False, "backend": "local_background", "error": value})
            except Exception as exc:
                self.reply(200, {"ok": False, "backend": "local_background",
                                 "error": {"code": type(exc).__name__, "message": str(exc)[:1500]}})
            finally:
                with activity_lock:
                    active_requests[0]-=1
                    last_activity[0]=time.monotonic()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    def expire_idle():
        while True:
            time.sleep(5)
            with activity_lock:
                if active_requests[0] or time.monotonic()-last_activity[0] < idle_timeout:continue
            result=desktop.execute("cleanup",{}) if desktop.execute("status",{}).get("created") else {"cleanup_complete":True}
            report=Path(config['data_root'])/'background_desktop'/'last-cleanup.json'
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
            if result.get('cleanup_complete'):
                server.shutdown()
                return
            last_activity[0]=time.monotonic()
    threading.Thread(target=expire_idle,name="BackgroundIdleCleanup",daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        desktop.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    serve(parser.parse_args().config)
