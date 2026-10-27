"""Verify the running production MCP without changing auth or service settings.

Creates only its own test workspace, one goal and one deterministic test task.
This is an MCP client probe, not ChatGPT web product acceptance.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[1]


async def main():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    origin = "http://127.0.0.1:48271"
    async with httpx.AsyncClient(timeout=10) as client:
        health = (await client.get(origin + "/health")).json()
        if health.get("orchestration_mode") != "chatgpt_direct":
            raise RuntimeError("Running MCP is not in chatgpt_direct mode; deployment has not completed")
        unauth = await client.post(origin + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert unauth.status_code == 401
        discovery = await client.get(origin + "/.well-known/oauth-authorization-server")
        discovery.raise_for_status()
        oauth = discovery.json()
        assert "S256" in oauth["code_challenge_methods_supported"]
        assert "web_agent" in oauth["scopes_supported"]
    run = ROOT / "10_web_deploy" / "validation_runs" / ("production-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace = run / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    token = (ROOT / "08_web_data" / "web_agent.token").read_text(encoding="utf-8").strip()
    report = {"client": "MCP ClientSession (not ChatGPT web)", "host_web_verified": False,
              "health": health, "oauth_discovery": {k: oauth[k] for k in ("issuer", "scopes_supported", "code_challenge_methods_supported")},
              "unauthenticated_mcp_status": unauth.status_code, "trace": []}
    async with streamablehttp_client(origin + "/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            report["direct_instructions"] = "current ChatGPT conversation owns" in (initialized.instructions or "")
            assert report["direct_instructions"]
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {"goal_start", "goal_status", "goal_checkpoint", "goal_finish", "task_status", "windows_snapshot"} <= names
            report["tool_count"] = len(names)

            async def call(name, args=None):
                start = time.monotonic()
                raw = await session.call_tool(name, args or {})
                result = raw.structuredContent
                assert result is not None and result["operation"] == name
                report["trace"].append({"tool": name, "status": result["status"], "isError": raw.isError,
                                        "elapsed_seconds": round(time.monotonic() - start, 3)})
                return result

            goal = await call("goal_start", {"goal": "Production direct-mode smoke", "acceptance": ["three test files are valid"], "workspace": str(workspace)})
            goal_id = goal["goal_id"]
            report["goal_id"] = goal_id
            await call("create_directory", {"path": str(workspace / "probe")})
            for name, text in (("one.txt", "valid"), ("two.txt", "wrong"), ("three.txt", "valid")):
                await call("write_file", {"path": str(workspace / "probe" / name), "content": text})
            listing = await call("list_directory", {"path": str(workspace / "probe")})
            for entry in listing["result"]["data"]["entries"]:
                path = workspace / "probe" / entry["name"]
                read = await call("read_file", {"path": str(path)})
                text = read["result"]["data"]["content"]
                if text != "valid":
                    assert (await call("patch_file", {"path": str(path), "find": text, "replace": "valid"}))["ok"]
                    assert (await call("read_file", {"path": str(path)}))["result"]["data"]["content"] == "valid"
            assert (await call("read_file", {"path": str(workspace / "missing.txt")}))["error_code"] == "not_found"
            assert (await call("read_file", {"path": str(workspace / "probe" / "one.txt")}))["ok"]
            planned = await call("run_autonomous_workflow", {"goal": "Natural language without explicit steps"})
            assert planned["status"] == "needs_chatgpt_planning" and not planned["terminal"]
            queued = await call("run_python_script", {"cwd": str(workspace), "code": "print('production-direct-worker-ok')", "timeout": 30})
            assert queued["status"] in {"queued", "running"} and queued["task_id"] and not queued["terminal"]
            report["task_id"] = queued["task_id"]
            for _ in range(80):
                state = await call("task_status", {"task_id": queued["task_id"]})
                if state["terminal"]:
                    break
                await asyncio.sleep(0.5)
            assert state["status"] == "completed", state.get("error")
            assert state["result"]["data"]["result"]["return_code"] == 0
            output = await call("read_task_output", {"task_id": queued["task_id"], "max_chars": 2000})
            assert "production-direct-worker-ok" in output["result"]["data"]["content"]
            await call("goal_checkpoint", {"goal_id": goal_id, "step_id": "verified", "task_id": queued["task_id"], "idempotency_key": "verified-once", "observation": {"files": "all valid", "return_code": 0}})
            finished = await call("goal_finish", {"goal_id": goal_id, "outcome": "completed", "evidence": {"checks": [
                {"criterion": "three test files are valid", "passed": True, "evidence": {"directory": str(workspace / "probe"), "content": "valid"}}]}})
            assert finished["status"] == "completed"
            assert (await call("windows_snapshot"))["error_code"] == "desktop_disabled"
            assert all(path.read_text() == "valid" for path in (workspace / "probe").glob("*.txt"))
    preflight = ROOT / "10_web_deploy/migration_backups/20261026-141209-chatgpt-direct/deployment-preflight.json"
    if preflight.exists():
        expected = json.loads(preflight.read_text(encoding="utf-8"))["protected_files"]
        report["protected_files_unchanged"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in expected.items()}
        assert all(report["protected_files_unchanged"].values())
    report["passed"] = True
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "calls": len(report["trace"]), "tool_count": report["tool_count"], "report": str(run / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())