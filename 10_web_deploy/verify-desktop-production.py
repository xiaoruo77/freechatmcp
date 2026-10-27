"""Reversible desktop acceptance through the real OAuth-protected MCP API.

Uses only one owned Notepad tab. All input flows through the production MCP,
then the authenticated loopback bridge. Native helpers observe identity/focus.
"""
from __future__ import annotations

import asyncio
import argparse
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_probe_helpers", ROOT / "10_web_deploy/windows/windows_mcp_notepad_smoke.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def control_state_ready(data):
    return isinstance(data, dict) and data.get("state") in {"ready", "ai"}


def native_idle_ms():
    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    value = LastInput(ctypes.sizeof(LastInput), 0)
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    if not user32.GetLastInputInfo(ctypes.byref(value)):
        return None
    return ((kernel32.GetTickCount64() & 0xFFFFFFFF) - value.dwTime) & 0xFFFFFFFF


def initial_idle_ready(data, idle_ms):
    return control_state_ready(data) and isinstance(idle_ms, int) and idle_ms >= 10_000


def protect_private_report(path):
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
    sid = next(csv.reader(result.stdout.splitlines()))[1]
    if not sid.startswith("S-1-"):
        raise helpers.ProbeBlocked("private_report_identity_unavailable")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


async def main(resume_report=None):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nonce = uuid.uuid4().hex[:12]
    run = ROOT / "10_web_deploy/validation_runs" / ("desktop-production-" + stamp + "-" + nonce)
    run.mkdir(parents=True, exist_ok=False)
    protect_private_report(run)
    marker = "Xiaoruo1-Production-Desktop-" + nonce
    text = "Production MCP desktop verified " + nonce
    target_file = run / (marker + ".txt")
    if resume_report:
        previous = Path(resume_report).resolve()
        previous.relative_to((ROOT / "10_web_deploy/validation_runs").resolve())
        prior = json.loads(previous.read_text(encoding="utf-8"))
        target_file = Path(prior["test_file"]).resolve()
        target_file.relative_to((ROOT / "10_web_deploy/validation_runs").resolve())
        marker = target_file.stem
        if not marker.startswith("Xiaoruo1-Production-Desktop-") or target_file.read_text(encoding="utf-8-sig") != "":
            raise helpers.ProbeBlocked("resume_target_is_not_owned_empty_fixture")
    else:
        target_file.write_text("", encoding="utf-8")
    token = (ROOT / "08_web_data/web_agent.token").read_text().strip()
    report = {"production_mcp": "http://127.0.0.1:48271/mcp", "desktop_transport": "authenticated loopback streamable-http",
              "started_at": datetime.now(timezone.utc).isoformat(), "test_file": str(target_file), "text": text,
              "trace": [], "focus": [], "passed": False, "cleanup": {}}
    if resume_report:
        report["resumed_empty_fixture_from"] = str(previous)
    api = helpers.native_api()
    target = None
    saved = False
    attempted = False

    def checkpoint():
        # Keep bounded waiting and real tool results inspectable even if the
        # host or terminal disconnects before the final acceptance summary.
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint()
    async with httpx.AsyncClient(timeout=5) as client:
        report["unauthenticated_desktop_status"] = (await client.post("http://127.0.0.1:48888/mcp", json={})).status_code
        assert report["unauthenticated_desktop_status"] == 401
    async with streamablehttp_client(report["production_mcp"], headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            idle_deadline = asyncio.get_running_loop().time() + 180

            async def call(name, arguments=None):
                started_call = time.monotonic()
                reply = await session.call_tool(name, arguments or {})
                value = reply.structuredContent
                if value is None:
                    raise helpers.ProbeBlocked("missing_production_envelope")
                report["trace"].append({"tool": name, "ok": value["ok"], "error_code": value.get("error_code"),
                                        "transport": value.get("evidence", {}).get("transport"),
                                        "duration_seconds": round(time.monotonic() - started_call, 3),
                                        "image_blocks": sum(block.type == "image" for block in reply.content)})
                if name == "windows_app":
                    report.setdefault("private_app_diagnostics", []).append({"arguments": arguments,
                        "summary": value.get("result", {}).get("summary"), "error": value.get("error"),
                        "evidence": value.get("evidence")})
                report["last_observed_tool"] = name
                checkpoint()
                if not value["ok"]:
                    raise helpers.ProbeBlocked(value.get("error_code") or "production_action_failed")
                return value

            async def idle(initial=False):
                while asyncio.get_running_loop().time() < idle_deadline:
                    status = await call("windows_control_status")
                    data = status["result"]["data"]
                    report.setdefault("control_wait", []).append(data)
                    os_idle = native_idle_ms() if initial else None
                    if initial:
                        report.setdefault("initial_native_idle_ms", []).append(os_idle)
                    checkpoint()
                    if initial_idle_ready(data, os_idle) if initial else control_state_ready(data):
                        print(json.dumps({"stage": "desktop_control_ready", "state": data["state"]}), flush=True)
                        return
                    delay = min(15, max(1, float(data.get("wait_seconds", 2)) + .3)) if isinstance(data, dict) else 2
                    await asyncio.sleep(min(delay, max(0, idle_deadline - asyncio.get_running_loop().time())))
                raise helpers.ProbeBlocked("desktop_control_not_ready_within_180_seconds")

            def focus(stage):
                observed = helpers.foreground_evidence(api, target, marker)
                report["focus"].append({"stage": stage, **observed})
                if not observed.get("matches_exact_hwnd") or not observed.get("matches_test_marker"):
                    raise helpers.ProbeBlocked("owned_test_tab_lost_focus")

            try:
                # A restarted bridge has no historical hook events. Require a
                # real ten-second native idle window once before the first GUI
                # action. Later AI-generated input must not block itself here.
                await idle(initial=True)
                picture = await call("windows_screenshot")
                report["private_screenshot_artifacts"] = picture.get("artifacts", [])
                assert picture.get("artifacts") and report["trace"][-1]["image_blocks"] >= 1
                # Reopening the exact owned fixture can activate its existing
                # Notepad tab when the user has selected another tab. It never
                # launches arbitrary argv or targets a user document.
                if not resume_report or not helpers.test_windows(api, marker):
                    subprocess.Popen(["C:/Windows/System32/notepad.exe", str(target_file)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    report["fixture_opened_to_activate_owned_tab"] = True
                for _ in range(50):
                    windows = helpers.test_windows(api, marker)
                    if len(windows) == 1:
                        break
                    await asyncio.sleep(.2)
                if len(windows) != 1:
                    raise helpers.ProbeBlocked("unique_test_window_unavailable")
                target = windows[0]
                report["owned_target"] = {key: value for key, value in target.items() if key != "title"}
                await idle()
                already_foreground = helpers.foreground_evidence(api, target, marker)
                report["focus"].append({"stage": "before_optional_App", **already_foreground})
                if already_foreground.get("matches_exact_hwnd") and already_foreground.get("matches_test_marker"):
                    report["App_switch_verified"] = False
                    report["App_switch_skipped_reason"] = "owned_test_tab_already_exact_foreground"
                else:
                    await call("windows_app", {"mode": "switch", "name": target["title"], "confirm": True})
                    focus("after_App")
                    report["App_switch_verified"] = True
                snap = await call("windows_snapshot", {"use_vision": False})
                focus("after_Snapshot")
                points = helpers.own_edit_points(snap["result"]["summary"], marker)
                if len(points) != 1:
                    raise helpers.ProbeBlocked("unique_observed_editor_unavailable")
                rect = helpers.window_details(api, target["hwnd"])["rect"]
                if not (rect[0] <= points[0][0] < rect[2] and rect[1] <= points[0][1] < rect[3]):
                    raise helpers.ProbeBlocked("editor_outside_test_window")
                focus("before_Type")
                attempted = True
                await call("windows_type", {"text": text, "loc": points[0], "clear": True, "confirm": True})
                focus("after_Type")
                snap = await call("windows_snapshot", {"use_vision": False})
                report["ui_readback"] = helpers.own_text_readback(snap["result"]["summary"], marker, text)
                assert report["ui_readback"]
                focus("before_Save")
                await call("windows_shortcut", {"shortcut": "ctrl+s", "confirm": True})
                for _ in range(30):
                    saved = target_file.read_text(encoding="utf-8-sig") == text
                    if saved:
                        break
                    await asyncio.sleep(.2)
                report["file_readback"] = saved
                assert saved
                report["passed"] = True
            except Exception as exc:
                report["error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, helpers.ProbeBlocked) else "acceptance_failed"}
            finally:
                if target and (not attempted or saved) and helpers.foreground_evidence(api, target, marker).get("matches_test_marker"):
                    try:
                        await call("windows_shortcut", {"shortcut": "ctrl+w", "confirm": True})
                        for _ in range(30):
                            if not helpers.test_windows(api, marker):
                                break
                            await asyncio.sleep(.1)
                    except Exception:
                        report["cleanup"]["close_not_verified"] = True
                report["cleanup"]["remaining_owned_tabs"] = len(helpers.test_windows(api, marker))
                report["cleanup"]["inventory_scope"] = "top_level_window_titles; inactive tabs are not enumerated"
                report["passed"] &= report["cleanup"]["remaining_owned_tabs"] == 0
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "report": str(run / "report.json"),
                      "ui_readback": report.get("ui_readback"), "file_readback": report.get("file_readback"),
                      "cleanup": report["cleanup"], "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-report", help="Reuse only an empty fixture owned by a previous production probe")
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.resume_report)))