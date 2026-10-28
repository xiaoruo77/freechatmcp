"""Real file-input acceptance through the existing OAuth-protected Xiaoruo1 MCP.

Does not launch a browser, navigate a tab, read cookies, or use Playwright to
control a separate context. The caller must provide the already-open Edge
fixture tab. All browser operations below use public Xiaoruo1 tools.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "http://127.0.0.1:48272/upload-test/fixture.html"


async def main(tab_id: str, file_path: Path, fixture_url: str = FIXTURE) -> int:
    if fixture_url not in {FIXTURE, "https://mcp.example.com/upload-test/fixture.html"}:
        raise ValueError("Only the two fixed test fixture origins are allowed")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    file_path = file_path.resolve(strict=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = ROOT / "10_web_deploy" / "validation_runs" / f"upload-fixture-{stamp}-{uuid.uuid4().hex[:8]}"
    run.mkdir(parents=True, exist_ok=False)
    token = (ROOT / "08_web_data/web_agent.token").read_text(encoding="utf-8").strip()
    report = {"passed": False, "kind": "existing Edge tab through production MCP",
              "fixture_url": fixture_url, "tab_id": tab_id, "local_file": str(file_path),
              "file_size": file_path.stat().st_size,
              "file_sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(), "trace": []}

    def save():
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async with streamablehttp_client("http://127.0.0.1:48271/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool, arguments):
                result = await session.call_tool(tool, arguments)
                value = result.structuredContent
                if not isinstance(value, dict):
                    text = next((c.text for c in result.content if c.type == "text"), "{}")
                    value = json.loads(text)
                data = value.get("result", {}).get("data", value)
                # Only retry an explicitly rejected, never-dispatched selection.
                # Observe the original takeover controller; never clear or bypass it.
                if tool == "browser_upload_file" and data.get("status") == "rejected" and data.get("redispatched") is False and (data.get("error") or {}).get("code") == "upload_desktop_control_unavailable":
                    deadline = asyncio.get_running_loop().time() + 45
                    report.setdefault("safety_waits", []).append({"reason": "user_control_before_dispatch", "idempotency_key": arguments["idempotency_key"]})
                    while asyncio.get_running_loop().time() < deadline:
                        status = await session.call_tool("windows_control_status", {})
                        state = status.structuredContent or {}
                        state_data = state.get("result", {}).get("data")
                        if not isinstance(state_data, dict):
                            try: state_data = json.loads(state.get("result", {}).get("summary", "{}"))
                            except ValueError: state_data = {}
                        if state.get("ok") and state_data.get("state") in {"ready", "ai"}:
                            retry = await session.call_tool(tool, arguments)
                            retried = retry.structuredContent or {}
                            data = retried.get("result", {}).get("data", retried)
                            break
                        await asyncio.sleep(1)
                report["trace"].append({"tool": tool, "result": data})
                save()
                return data

            try:
                tabs = await call("browser_list_tabs", {})
                matches = [t for t in tabs.get("tabs", []) if str(t.get("tab_id")) == tab_id]
                assert len(matches) == 1 and matches[0]["url"] == fixture_url, "Explicit existing fixture tab required"
                assert matches[0].get("browser_family") == "edge", "Fixture must be in Edge"
                page = await call("browser_read_page", {"tab_id": tab_id, "max_chars": 10000, "include_frames": True})
                inputs = page.get("file_inputs", [])
                targets = [i for i in inputs if i.get("id") == "single"]
                assert len(targets) == 1 and not targets[0].get("selected_files"), "Use an empty fresh fixture"
                selector = targets[0]["selector"]
                base = {"tab_id": tab_id, "selector": selector, "file_path": str(file_path), "confirm": True, "timeout": 30000}
                key = "fixture-" + uuid.uuid4().hex
                selected = await call("browser_upload_file", {**base, "idempotency_key": key})
                assert selected.get("status") == "selected", selected
                assert selected["selected_file"]["name"] == file_path.name
                assert selected["selected_file"]["size"] == file_path.stat().st_size
                assert selected["selected_file"]["sha256"] == report["file_sha256"]
                assert selected["stages"]["server_received"] is None, "Selection must not claim a server upload"
                readback = await call("browser_read_page", {"tab_id": tab_id, "max_chars": 10000})
                current = [i for i in readback["file_inputs"] if i.get("id") == "single"]
                assert current[0]["selected_files"][0]["name"] == file_path.name
                assert current[0]["selected_files"][0]["size"] == report["file_size"]
                assert '"changes": 1' in readback.get("text", ""), "Fixture must observe exactly one change event"
                duplicate = await call("browser_upload_file", {**base, "idempotency_key": key})
                assert duplicate.get("duplicate_call") is True and duplicate.get("redispatched") is False
                negative = [
                    {**base, "confirm": False, "idempotency_key": "fixture-" + uuid.uuid4().hex},
                    {**base, "selector": 'input[name="buploader"]', "idempotency_key": "fixture-" + uuid.uuid4().hex},
                    {**base, "file_path": str(ROOT / "08_web_data/web_agent.token"), "idempotency_key": "fixture-" + uuid.uuid4().hex},
                    {**base, "file_path": str(file_path.parent / "does-not-exist.mp4"), "idempotency_key": "fixture-" + uuid.uuid4().hex},
                ]
                for arguments in negative:
                    denied = await call("browser_upload_file", arguments)
                    assert denied.get("status") in {"rejected", "failed"} and denied.get("redispatched") is not True
                # Exercise same-name controls independently using their observed
                # unique tokens, never a generic selector or guessed element.
                repeated = [i for i in readback["file_inputs"] if i.get("name") == "buploader"]
                assert len(repeated) == 2 and len({i["selector"] for i in repeated}) == 2
                for item in repeated:
                    value = await call("browser_upload_file", {**base, "selector": item["selector"], "idempotency_key": "fixture-" + uuid.uuid4().hex})
                    assert value.get("status") == "selected"
                final = await call("browser_read_page", {"tab_id": tab_id, "max_chars": 10000})
                assert '"changes": 3' in final.get("text", ""), "Same-name controls must receive exactly one selection each"
                assert all(i.get("selected_files") for i in final["file_inputs"] if not i.get("disabled"))
                receipt = await call("browser_upload_status", {"idempotency_key": key})
                assert receipt.get("status") == "selected"
                report["passed"] = True
                report["limits"] = ["local file selection only; no Bilibili upload implied", "native file-picker cancellation not applicable to byte delivery", "timeout and cancellation covered separately in unit tests"]
            except Exception as exc:
                report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1500]}
            finally:
                save()
    print(json.dumps({"passed": report["passed"], "report": str(run / "report.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tab-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--fixture-url", default=FIXTURE)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.tab_id, arguments.file, arguments.fixture_url)))