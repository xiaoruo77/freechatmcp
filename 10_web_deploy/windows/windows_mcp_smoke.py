"""Read-only real Windows-MCP probe; screen content is never printed/logged.

Run with the project's Python 3.13 interpreter after installing the isolated
runtime. Only runtime-local reports and private screenshot artifacts are made.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "06_web_tools"))
from web_tools.windows_mcp import ALLOWED_ACTIONS, WindowsMCPBridge, interactive_desktop_state


async def main():
    runtime = Path(__file__).resolve().parent / "windows_mcp_runtime"
    output = runtime / "smoke"
    output.mkdir(parents=True, exist_ok=True)
    args = ["serve", "--transport", "stdio", "--tools", ",".join(sorted(ALLOWED_ACTIONS))]
    config = SimpleNamespace(data_root=output, windows_mcp_enabled=True, windows_mcp_control_enabled=False,
                             windows_mcp_transport="stdio", windows_mcp_command=str(runtime / "venv" / "Scripts" / "windows-mcp.exe"),
                             windows_mcp_args=args, windows_mcp_timeout=90)
    bridge = WindowsMCPBridge(config)
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "preflight": interactive_desktop_state(),
              "project_python": sys.version.split()[0], "transport": "stdio", "formal_service_changed": False,
              "control_enabled": False, "tests": {}}
    versions = subprocess.check_output([str(runtime / "venv" / "Scripts" / "python.exe"), "-c", "import sys,importlib.metadata as m,json; print(json.dumps({'python':sys.version.split()[0],'windows-mcp':m.version('windows-mcp'),'fastmcp':m.version('fastmcp'),'mcp':m.version('mcp')}))"], text=True)
    report["isolated_runtime"] = json.loads(versions)
    try:
        async with bridge._session() as session:
            await asyncio.wait_for(session.initialize(), 90)
            advertised = await asyncio.wait_for(session.list_tools(), 30)
            report["tools_list"] = [{"name": tool.name, "input_schema_type": tool.inputSchema.get("type")} for tool in advertised.tools]
            report["tests"]["tools_list"] = {"ok": ALLOWED_ACTIONS.issubset({tool.name for tool in advertised.tools})}
        for action, params in [("ControlStatus", {}), ("Snapshot", {"use_vision": False}), ("Screenshot", {})]:
            result = await bridge.call(action, params)
            item = {"ok": result["ok"], "status": result["status"], "error_code": result.get("error_code"),
                    "response_text_chars": len(result["result"]["summary"]), "private_artifacts": result["artifacts"]}
            if action == "ControlStatus":
                # Ownership status contains no application/screen content.
                item["control_state"] = result["result"]["data"]
            report["tests"][action] = item
        report["not_verified"] = ["Notepad control and readback", "manual takeover", "locked desktop", "UAC", "DPI and multiple displays", "LocalSystem-to-user authenticated HTTP"]
    except Exception as exc:
        report["probe_error"] = {"type": type(exc).__name__, "message": "Windows-MCP probe failed; no screen or raw stderr logged"}
    finally:
        await bridge.close()
    report_path = output / "read-only-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "tests": {name: value.get("ok") for name, value in report["tests"].items()}, "probe_error": report.get("probe_error")}, ensure_ascii=False))
    return 0 if report["tests"] and all(test.get("ok") for test in report["tests"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))