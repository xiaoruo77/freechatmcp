"""Real loopback MCP protocol probe. This is not ChatGPT web end-to-end proof."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
for folder in ("02_web_mcp", "03_web_worker", "04_web_canvas", "05_web_memory", "06_web_tools", "07_web_config", "09_web_logs"):
    sys.path.insert(0, str(ROOT / folder))


async def main():
    run = ROOT / "10_web_deploy" / "validation_runs" / ("direct-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace = run / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    os.environ.update(WEB_AGENT_HOME=str(ROOT), WEB_AGENT_DATA=str(run / "data"), WEB_AGENT_LOGS=str(run / "logs"),
        WEB_AGENT_WORKSPACE=str(workspace), WEB_AGENT_WORKSPACES=str(workspace), WEB_AGENT_TOKEN=token,
        WEB_AGENT_ORCHESTRATION_MODE="chatgpt_direct", WEB_AGENT_OAUTH_ENABLED="false", WEB_AGENT_EMBEDDED_WORKER="true")
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from web_config.settings import Settings
    from web_mcp.server import create_app
    from web_mcp.tool_registry import ToolRegistry

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Sampling/implicit handoff must never occur")
    for name in ("_autonomous_sample", "_maybe_handoff_runner_goal", "_maybe_handoff_browser_goal", "_infer_latest_chat_goal"):
        setattr(ToolRegistry, name, forbidden)
    settings = replace(Settings.load(), task_poll_interval=0.2, windows_mcp_enabled=False, windows_mcp_control_enabled=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    report = {"client": "Synthetic MCP ClientSession; not a ChatGPT web conversation", "host_web_verified": False,
        "transport": "real loopback Streamable HTTP", "sampling_allowed": False, "trace": [], "workspace": str(workspace)}
    try:
        for _ in range(200):
            if server.started:
                break
            if serving.done():
                await serving
            await asyncio.sleep(0.05)
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                report["tools"] = [tool.name for tool in listed.tools]

                async def call(name, args=None):
                    reply = await session.call_tool(name, args or {})
                    result = reply.structuredContent
                    assert result is not None and result["operation"] == name
                    report["trace"].append({"tool": name, "arguments": args or {}, "status": result["status"], "isError": reply.isError,
                        "task_id": result.get("task_id"), "evidence": result.get("evidence")})
                    return result

                goal = await call("goal_start", {"goal": "Find and repair one wrong file among three", "acceptance": ["all three files say valid"]})
                goal_id = goal["goal_id"]
                report["goal_id"] = goal_id
                await call("create_directory", {"path": "probe"})
                for name, text in (("one.txt", "valid"), ("two.txt", "wrong"), ("three.txt", "valid")):
                    await call("write_file", {"path": "probe/" + name, "content": text})
                listing = await call("list_directory", {"path": "probe"})
                for entry in listing["result"]["data"]["entries"]:
                    value = await call("read_file", {"path": entry["path"]})
                    text = value["result"]["data"]["content"]
                    if text != "valid":
                        await call("patch_file", {"path": entry["path"], "find": text, "replace": "valid"})
                        reread = await call("read_file", {"path": entry["path"]})
                        assert reread["result"]["data"]["content"] == "valid"
                missing = await call("read_file", {"path": "nonexistent.txt"})
                assert missing["error_code"] == "not_found"
                assert (await call("read_file", {"path": listing["result"]["data"]["entries"][0]["path"]}))["ok"]
                queued = await call("run_python_script", {"cwd": str(workspace), "code": "print('real-worker-verified')"})
                for _ in range(80):
                    state = await call("task_status", {"task_id": queued["task_id"]})
                    if state["terminal"]:
                        break
                    await asyncio.sleep(0.1)
                assert state["status"] == "completed", state
                output = await call("read_task_output", {"task_id": queued["task_id"]})
                assert "real-worker-verified" in output["result"]["data"]["content"]
                await call("goal_checkpoint", {"goal_id": goal_id, "step_id": "verified", "idempotency_key": "verified-once",
                    "task_id": queued["task_id"], "observation": {"files": "all valid", "worker_return_code": state["result"]["data"]["result"]["return_code"]}})
                done = await call("goal_finish", {"goal_id": goal_id, "outcome": "completed", "evidence": {"checks": [
                    {"criterion": "all three files say valid", "passed": True, "evidence": {"directory": str(workspace / "probe"), "content": "valid"}}]}})
                assert done["status"] == "completed"
                assert all(path.read_text() == "valid" for path in (workspace / "probe").glob("*.txt"))
                report["passed"] = True
    finally:
        server.should_exit = True
        await serving
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report.get("passed", False), "calls": len(report["trace"]), "tools": len(report["tools"]), "report": str(run / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())