"""Call exactly one explicitly selected public tool on the existing local MCP.

Arguments and evidence stay under validation_runs. This helper contains no
planning, browser context, automatic action selection, or action retry loop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATION_ROOT = (ROOT / "10_web_deploy/validation_runs").resolve()
ALLOWED_TOOLS = frozenset({
    "browser_list_tabs", "browser_list_downloads", "browser_open_tab", "browser_start",
    "browser_navigate", "browser_read_page", "browser_read", "browser_screenshot",
    "browser_click", "browser_type", "browser_press", "browser_select_option",
    "browser_wait", "browser_close_tab", "browser_upload_file", "browser_upload_status",
    "video_project_path", "browser_instances", "browser_control", "browser_debug_control",
})


def validation_path(value: str, *, must_exist: bool) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve(strict=must_exist)
    candidate.relative_to(VALIDATION_ROOT)
    if candidate.suffix.lower() != ".json":
        raise ValueError("Arguments and output must be JSON files inside validation_runs")
    if must_exist and (not candidate.is_file() or candidate.stat().st_size > 4 * 1024 * 1024):
        raise ValueError("Arguments must be a bounded regular JSON file")
    return candidate


def without_inline_images(value):
    """Keep structured evidence while omitting any inline image payloads."""
    if isinstance(value, dict):
        return {key: without_inline_images(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_inline_images(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        header, _, payload = value.partition(",")
        return {"omitted_inline_image": True, "mime_type": header[5:].split(";")[0],
                "encoded_characters": len(payload)}
    return value


async def call_one(tool: str, arguments_file: Path, output: Path) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    arguments = json.loads(arguments_file.read_text(encoding="utf-8-sig"))
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    token = (ROOT / "08_web_data/web_agent.token").read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("Production MCP credential is unavailable")
    async with streamablehttp_client("http://127.0.0.1:48271/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Exactly one user/model-selected tool call. No automatic retries.
            reply = await session.call_tool(tool, arguments)
    structured = reply.structuredContent
    texts = []
    images = []
    for block in reply.content:
        if block.type == "image":
            images.append({"mime_type": block.mimeType, "encoded_characters": len(block.data)})
        elif block.type == "text" and structured is None:
            texts.append(block.text)
    if structured is None and len(texts) == 1:
        try:
            structured = json.loads(texts[0])
            texts = []
        except json.JSONDecodeError:
            pass
    evidence = {"tool": tool, "isError": bool(reply.isError),
                "structuredContent": without_inline_images(structured), "images": images}
    if texts:
        evidence["text_content"] = without_inline_images(texts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    data = structured if isinstance(structured, dict) else {}
    error = data.get("error")
    code = data.get("error_code") or (error.get("code") if isinstance(error, dict) else None)
    ok = not bool(reply.isError) and data.get("ok") is not False
    print(json.dumps({"ok": ok, "status": data.get("status", "completed" if ok else "failed"),
                      "output": str(output), "error_code": code}, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, choices=sorted(ALLOWED_TOOLS))
    parser.add_argument("--arguments-file", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    try:
        arguments_file = validation_path(options.arguments_file, must_exist=True)
        output = validation_path(options.output, must_exist=False)
        if arguments_file == output:
            raise ValueError("Output must not overwrite the supplied arguments file")
        return asyncio.run(call_one(options.tool, arguments_file, output))
    except Exception as exc:
        # Exception strings may contain transport details; do not print them.
        print(json.dumps({"ok": False, "status": "failed", "error_code": "mcp_helper_" + type(exc).__name__,
                          "output": options.output}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())