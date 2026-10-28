"""Read-only layered MCP diagnosis. No service restarts or network changes.

Run with the existing .venv. Reports contain status codes and hashes, never
response bodies, credentials, source text, or exception messages.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import httpx

ROOT = Path(__file__).resolve().parents[1]


def http_summary(status: int, headers, body: str) -> dict:
    result = {"http_status": status}
    if headers.get("server", "").lower() == "cloudflare":
        result["via_cloudflare"] = True
        if status == 530 and re.search(r"\b1033\b", body):
            result["cloudflare_error"] = 1033
    return result


def connection_count(body: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("cloudflared_tunnel_ha_connections "):
            try:
                count = float(line.split()[-1])
                return count if math.isfinite(count) and count >= 0 else None
            except ValueError:
                return None
    return None


def classify(checks: dict) -> str:
    if not checks["local_mcp"].get("ok"):
        return "local_mcp_unavailable"
    if checks["public_health"].get("cloudflare_error") == 1033:
        return "tunnel_disconnected"
    if checks["public_health"].get("http_status") != 200:
        return "public_entry_unavailable"
    if checks["public_mcp"].get("http_status") != 401:
        return "public_mcp_route_unexpected"
    if checks["tunnel_ready"].get("http_status") != 200 or not checks["tunnel_metrics"].get("active_connections"):
        # A second connector can serve this hostname while this PC is offline.
        return "local_tunnel_not_ready"
    return "local_and_public_transport_healthy"


async def http_probe(client, url: str) -> dict:
    try:
        async with client.stream("GET", url) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk[: max(0, 32768 - len(content))])
                if len(content) >= 32768:
                    break
            body = content.decode("utf-8", "replace")
            result = http_summary(response.status_code, response.headers, body)
            if url.endswith("/metrics"):
                result["active_connections"] = connection_count(body)
            return result
    except Exception as exc:
        # str(exc) can contain request URLs, credentials or raw upstream data.
        return {"error_type": type(exc).__name__}


async def local_mcp_probe(url: str, token: str) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def read():
        async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                reply = await session.call_tool("read_file", {
                    "path": str(ROOT / "README.md"), "start_line": 1, "end_line": 3,
                })
                result = reply.structuredContent or {}
                return {"ok": not bool(reply.isError) and result.get("ok") is True,
                        "tool": "read_file", "source_sha256": hashlib.sha256((ROOT / "README.md").read_bytes()).hexdigest()}
    try:
        return await asyncio.wait_for(read(), timeout=12)
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__}


async def diagnose(settings) -> dict:
    local = f"http://127.0.0.1:{settings.port}"
    public = settings.oauth_issuer_url.rstrip("/")
    parsed = httpx.URL(public)
    if parsed.scheme != "https" or parsed.userinfo or parsed.query or parsed.fragment:
        raise ValueError("Configured OAuth issuer must be a credential-free HTTPS URL")
    endpoints = {
        "local_health": local + "/health", "public_health": public + "/health",
        "public_mcp": public + "/mcp", "tunnel_ready": "http://127.0.0.1:20241/ready",
        "tunnel_metrics": "http://127.0.0.1:20241/metrics",
    }
    async with httpx.AsyncClient(timeout=6, trust_env=False, follow_redirects=False) as client:
        results = await asyncio.gather(*(http_probe(client, url) for url in endpoints.values()),
                                       local_mcp_probe(local + "/mcp", settings.token))
    checks = dict(zip(endpoints, results[:-1]))
    checks["local_mcp"] = results[-1]
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
            "diagnosis": classify(checks),
            "scope": "read_only; no source content, credentials, service restarts or write retries",
            "limits": "Public MCP 401 verifies transport/auth enforcement, not a specific ChatGPT OAuth connection."}


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    from web_config.settings import Settings
    report = asyncio.run(diagnose(Settings.load()))
    folder = ROOT / "10_web_deploy/validation_runs/mcp-connectivity-20261028"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now().strftime("diagnosis-%Y%m%d-%H%M%S") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"diagnosis": report["diagnosis"], "report": str(path)}, ensure_ascii=False))
    return 0 if report["diagnosis"] == "local_and_public_transport_healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())