"""One reversible real Windows-MCP input probe in an owned Notepad window.

Uses temporary settings. Input comes exclusively from Windows-MCP Type and
Shortcut. Native Win32 calls observe HWND ownership and close only the exact
classic Notepad process created here; they never type or force focus.
Each invocation writes a new report and preserves prior failed probes.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "06_web_tools"))
from web_tools.windows_mcp import ALLOWED_ACTIONS, WindowsMCPBridge, interactive_desktop_state


class ProbeBlocked(RuntimeError):
    pass


def own_tree_lines(snapshot: str, marker: str) -> list[str]:
    """Select the observed test window, excluding unrelated UI text."""
    if "UI Tree:" not in snapshot:
        return []
    selected = False
    lines = []
    for line in snapshot.split("UI Tree:", 1)[1].splitlines():
        if 'window "' in line:
            selected = marker in line
        if selected:
            lines.append(line)
    return lines


def own_edit_points(snapshot: str, marker: str) -> list[list[int]]:
    points = []
    for line in own_tree_lines(snapshot, marker):
        match = re.search(r"\((-?\d+),\s*(-?\d+)\)\s+(?:edit|document|文档|编辑框|编辑)(?=\s)", line, re.I)
        if match:
            point = [int(match.group(1)), int(match.group(2))]
            if point not in points:
                points.append(point)
    return points


def own_text_readback(snapshot: str, marker: str, text: str) -> bool:
    return any(text in line and ('value:' in line or any(kind in line for kind in ("edit ", "document ", "文档 ", "编辑 ", "编辑框 ")))
               for line in own_tree_lines(snapshot, marker))


def native_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # UIA/Windows-MCP coordinates are physical pixels. Unaware Python callers
    # otherwise receive DPI-virtualized GetWindowRect values and can reject a
    # perfectly valid observed editor on a scaled monitor. Scope the change to
    # this test's thread, without changing system or application settings.
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if not user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
        raise ProbeBlocked("physical_pixel_coordinate_context_unavailable")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32


def window_details(api, handle: int) -> dict:
    title, class_name = ctypes.create_unicode_buffer(4096), ctypes.create_unicode_buffer(256)
    api.GetWindowTextW(handle, title, len(title))
    api.GetClassNameW(handle, class_name, len(class_name))
    pid, rect = wintypes.DWORD(), wintypes.RECT()
    api.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    api.GetWindowRect(handle, ctypes.byref(rect))
    return {"hwnd": int(handle), "pid": pid.value, "title": title.value, "class": class_name.value,
            "rect": [rect.left, rect.top, rect.right, rect.bottom], "visible": bool(api.IsWindowVisible(handle))}


def test_windows(api, marker: str) -> list[dict]:
    matches = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(handle, _):
        details = window_details(api, handle)
        if marker in details["title"] and details["visible"]:
            matches.append(details)
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows(callback, 0)
    return matches


def foreground_evidence(api, target: dict | None, marker: str) -> dict:
    handle = api.GetForegroundWindow()
    if not handle:
        return {"matches_exact_hwnd": False, "foreground_missing": True}
    details = window_details(api, handle)
    # Never record the title/PID of another user's foreground window.
    return {"matches_exact_hwnd": bool(target and int(handle) == target["hwnd"]),
            "matches_test_marker": marker in details["title"], "foreground_class": details["class"]}


async def main():
    runtime = Path(__file__).resolve().parent / "windows_mcp_runtime"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    nonce = uuid.uuid4().hex[:12]
    output = runtime / "smoke" / ("notepad-" + stamp + "-" + nonce)
    output.mkdir(parents=True, exist_ok=False)
    marker = "Xiaoruo1-Windows-MCP-smoke-" + nonce
    text = "Xiaoruo1 desktop input readback " + nonce
    test_file = output / (marker + ".txt")
    test_file.write_text("", encoding="utf-8")
    config = SimpleNamespace(data_root=output, windows_mcp_enabled=True, windows_mcp_control_enabled=True,
                             windows_mcp_transport="stdio", windows_mcp_command=str(runtime / "venv" / "Scripts" / "windows-mcp.exe"),
                             windows_mcp_args=["serve", "--transport", "stdio", "--tools", ",".join(sorted(ALLOWED_ACTIONS))], windows_mcp_timeout=45)
    bridge = WindowsMCPBridge(config)
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "preflight": interactive_desktop_state(),
              "formal_service_changed": False, "test_settings_only": True, "test_file": str(test_file),
              "marker": marker, "test_text": text, "tests": [], "focus_observations": [],
              "native_coordinate_space": "physical_pixels_per_monitor_v2_test_thread_only",
              "control_attempt_limits": {"App_switch": 1, "Type": 1, "automatic_retry": False},
              "cleanup": {"own_window_closed": False}}
    target, launcher, saved = None, None, False
    input_attempted = False
    api = native_api()

    async def call(action, arguments):
        result = await bridge.call(action, arguments)
        report["tests"].append({"tool": action, "ok": result["ok"], "error_code": result.get("error_code")})
        if not result["ok"]:
            raise ProbeBlocked(result.get("error_code") or "desktop_action_rejected")
        return result

    def require_exact_focus(stage: str):
        observation = foreground_evidence(api, target, marker)
        report["focus_observations"].append({"stage": stage, **observation})
        if not observation.get("matches_exact_hwnd") or not observation.get("matches_test_marker"):
            raise ProbeBlocked("test_target_lost_focus_at_" + stage)

    try:
        if not report["preflight"]["available"]:
            raise ProbeBlocked("interactive_desktop_unavailable")
        # Warm up MCP before opening Notepad to distinguish startup focus effects.
        status = await call("ControlStatus", {})
        report["initial_control_status"] = status["result"]["data"]
        # Exact argv only creates the fixture. MCP input remains mandatory.
        launcher = subprocess.Popen([str(Path("C:/Windows/System32/notepad.exe")), str(test_file)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        candidates = []
        for _ in range(40):
            candidates = test_windows(api, marker)
            if candidates:
                break
            await asyncio.sleep(.25)
        if len(candidates) != 1:
            report["target_candidate_count"] = len(candidates)
            raise ProbeBlocked("unique_owned_test_window_unavailable")
        target = candidates[0]
        report["test_target"] = {key: value for key, value in target.items() if key != "title"}
        report["launcher_pid"] = launcher.pid
        report["focus_observations"].append({"stage": "after_launch", **foreground_evidence(api, target, marker)})
        await call("App", {"mode": "switch", "name": marker})  # Exactly one focus request.
        require_exact_focus("after_App")
        snapshot = (await call("Snapshot", {"use_vision": False}))["result"]["summary"]
        require_exact_focus("after_Snapshot")
        focused = snapshot.split("Focused Window:", 1)[-1].split("Opened Windows:", 1)[0]
        report["snapshot_focused_target"] = marker in focused
        if not report["snapshot_focused_target"]:
            raise ProbeBlocked("snapshot_did_not_confirm_test_window")
        points = own_edit_points(snapshot, marker)
        report["observed_edit_candidate_count"] = len(points)
        if len(points) != 1:
            raise ProbeBlocked("ambiguous_test_edit_target")
        target = window_details(api, target["hwnd"])
        report["observed_target_rect_before_Type"] = target["rect"]
        left, top, right, bottom = target["rect"]
        if not (left <= points[0][0] < right and top <= points[0][1] < bottom):
            raise ProbeBlocked("observed_edit_outside_exact_test_window")
        report["observed_edit_point"] = points[0]
        require_exact_focus("before_Type")
        input_attempted = True
        await call("Type", {"text": text, "loc": points[0], "clear": True})  # Exactly once.
        require_exact_focus("after_Type")
        snapshot = (await call("Snapshot", {"use_vision": False}))["result"]["summary"]
        report["ui_text_readback"] = own_text_readback(snapshot, marker, text)
        require_exact_focus("before_Save")
        await call("Shortcut", {"shortcut": "ctrl+s"})
        for _ in range(20):
            if test_file.read_text(encoding="utf-8-sig") == text:
                saved = True
                break
            await asyncio.sleep(.2)
        report["saved_file_readback"] = saved
        if not report["ui_text_readback"] or not saved:
            raise ProbeBlocked("input_or_saved_readback_not_verified")
    except Exception as exc:
        report["probe_error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, ProbeBlocked) else "probe_failed_no_raw_screen_logged"}
        # Owner query diagnoses takeover without reacquiring focus or retrying.
        try:
            owner = await bridge.call("ControlStatus", {})
            report["failure_control_status"] = owner["result"]["data"] if owner["ok"] else {"error_code": owner.get("error_code")}
        except Exception:
            report["failure_control_status"] = {"unavailable": True}
    finally:
        report["input_attempted"] = input_attempted
        try:
            # In a reused modern Notepad window, close only the exact test tab
            # with MCP if it remains the observed foreground. No App retry.
            if target and (not input_attempted or saved) and foreground_evidence(api, target, marker).get("matches_test_marker"):
                close_result = await bridge.call("Shortcut", {"shortcut": "ctrl+w"})
                report["cleanup"]["exact_test_tab_MCP_close"] = close_result["ok"]
                for _ in range(20):
                    if not test_windows(api, marker):
                        break
                    await asyncio.sleep(.1)
            # Never WM_CLOSE reused Notepad windows containing other user tabs.
            can_close = target and launcher and target["pid"] == launcher.pid and target["class"] == "Notepad" and (not input_attempted or saved)
            if can_close and api.IsWindow(target["hwnd"]):
                fresh = window_details(api, target["hwnd"])
                if fresh["pid"] == launcher.pid and marker in fresh["title"]:
                    report["cleanup"]["exact_owned_WM_CLOSE_posted"] = bool(api.PostMessageW(target["hwnd"], 0x0010, 0, 0))
                    for _ in range(20):
                        if not api.IsWindow(target["hwnd"]):
                            break
                        await asyncio.sleep(.1)
            report["cleanup"]["own_window_closed"] = bool(target and not test_windows(api, marker))
            report["cleanup"]["remaining_test_window_count"] = len(test_windows(api, marker))
        except Exception:
            report["cleanup"]["inspection_failed"] = True
        await bridge.close()
    report["not_verified"] = ["App launch by Start Menu name", "manual takeover", "locked desktop", "UAC", "DPI/multiple displays", "authenticated HTTP from LocalSystem"]
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "ui_text_readback": report.get("ui_text_readback"),
                      "saved_file_readback": report.get("saved_file_readback"), "input_attempted": input_attempted,
                      "cleanup": report["cleanup"], "probe_error": report.get("probe_error")}, ensure_ascii=False))
    return 0 if not report.get("probe_error") and report.get("ui_text_readback") and saved and report["cleanup"]["own_window_closed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))