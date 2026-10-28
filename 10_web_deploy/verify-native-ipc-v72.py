"""Verify existing v72 through public MCP, real bridge and native named pipe.

Default isolated mode uses authenticated ASGI transports and separate receipts,
logs and screenshots. Production mode uses the existing MCP listener. Neither
mode starts a browser or listener. Only newly created fixture tabs are edited;
the original active tab may be selected once to restore the user's selection.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import time
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "10_web_deploy/validation_runs/native-ipc-v72-20261028"
FIXTURE = "http://127.0.0.1:48272/upload-test/fixture.html"
WRITES = {"open_tab", "close_tab", "activate_tab", "navigate", "click", "type", "press", "select", "scroll"}
UNKNOWN = {"outcome_unknown", "dispatched_unverified", "unknown"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe(value: Any, depth: int = 0) -> Any:
    """Evidence contains no credentials, original URL/title text, or image data."""
    if depth > 15:
        return {"omitted": "depth_limit"}
    if isinstance(value, dict):
        result = {}
        for key, entry in value.items():
            lowered = str(key).casefold()
            if any(word in lowered for word in ("token", "authorization", "pipename", "cookie", "password")):
                continue
            if lowered in {"title", "url", "pendingurl", "pending_url", "committed_url"} and isinstance(entry, str) and entry and not entry.startswith(FIXTURE):
                result[key + "_sha256"] = digest(entry)
            elif lowered in {"data", "data_base64", "data_url"} and isinstance(entry, str) and len(entry) > 1024:
                result[key + "_omitted"] = {"characters": len(entry), "sha256": digest(entry)}
            else:
                result[key] = safe(entry, depth + 1)
        return result
    if isinstance(value, list):
        return [safe(entry, depth + 1) for entry in value[:256]]
    if isinstance(value, str):
        if value.startswith(("data:image/", "iVBORw0KGgo")) or len(value) > 12000:
            return {"characters": len(value), "sha256": digest(value), "omitted": "large_content"}
        # Pipe locations must not be copied out even in unexpected diagnostics.
        if "\\\\.\\pipe\\" in value.casefold() or "authtoken" in value.casefold():
            return "[private IPC diagnostic omitted]"
    return value


def unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return result["data"]
    return value


def structured(reply) -> dict:
    if isinstance(reply, dict):
        value = reply.get("structuredContent")
        content = reply.get("content", [])
    else:
        value, content = reply.structuredContent, reply.content
    if isinstance(value, dict):
        return value
    for item in content:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if isinstance(text, str):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
            except ValueError:
                pass
    raise RuntimeError("mcp_structured_reply_missing")


def find(value: Any, key: str, depth=0):
    if depth > 10:
        return None
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for entry in value.values():
            found = find(entry, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for entry in value:
            found = find(entry, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            return find(json.loads(value), key, depth + 1)
        except ValueError:
            pass
    return None


def scroll_position(response: dict) -> dict | None:
    """Extract observed offsets, never the request's x/y arguments."""
    result = response.get("result") or {}
    def position(value):
        if not isinstance(value, dict):
            return None
        for key in ("after", "actual", "position", "scroll", "offset"):
            found = position(value.get(key))
            if found is not None:
                return found
        if all(type(value.get(key)) in {int, float} for key in ("x", "y")):
            return {"x": value["x"], "y": value["y"]}
        return None
    found = position(result)
    if found is not None:
        return found
    text = str(result.get("text") or "")
    # Lightpanda distinguishes its requested position from its actual offset.
    match = re.search(r"\(window offset x=(-?[0-9.]+),\s*y=(-?[0-9.]+)\)", text)
    if match:
        return {"x": float(match[1]), "y": float(match[2])}
    # CEF returns a JSON object after 'Chromium page scroll:'.
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            found = position(value)
            if found is not None:
                return found
        except ValueError:
            pass
    return None


class ASGISession:
    def __init__(self, client, token):
        self.client, self._token, self.sequence = client, token, 0

    def _rpc(self, method: str, params: dict, notification=False):
        self.sequence += 1
        packet = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            packet["id"] = self.sequence
        response = self.client.post("/mcp", headers={
            "Authorization": "Bearer " + self._token, "Host": "127.0.0.1:48271",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
            json=packet)
        if notification and response.status_code in {200, 202, 204}:
            return {}
        if response.status_code != 200:
            raise RuntimeError("authenticated_asgi_mcp_http_" + str(response.status_code))
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            packets = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
            packet = next((entry for entry in reversed(packets) if entry.get("id") == self.sequence), {})
        else:
            packet = response.json()
        if "error" in packet or "result" not in packet:
            raise RuntimeError("asgi_mcp_rpc_rejected")
        return packet["result"]

    async def initialize(self):
        result = await asyncio.to_thread(self._rpc, "initialize", {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "native-v72-validation", "version": "1.0"}})
        await asyncio.to_thread(self._rpc, "notifications/initialized", {}, True)
        return result

    async def call_tool(self, name: str, arguments: dict):
        return await asyncio.to_thread(self._rpc, "tools/call", {"name": name, "arguments": arguments})


@asynccontextmanager
async def public_session(mode: str, run: Path):
    if mode == "production":
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        token = (ROOT / "08_web_data/web_agent.token").read_text(encoding="utf-8").strip()
        async with streamablehttp_client("http://127.0.0.1:48271/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    # Set every writable location before importing browser_bridge or Settings.
    # Keep WEB_AGENT_HOME at the real project so its desktop policy is loaded.
    data, logs = run / "isolated-data", run / "isolated-logs"
    data.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    os.environ.update({"WEB_AGENT_HOME": str(ROOT), "WEB_AGENT_DATA": str(data),
        "WEB_AGENT_LOGS": str(logs), "WEB_AGENT_WORKSPACE": str(run),
        "WEB_AGENT_WORKSPACES": str(run), "WEB_AGENT_BROWSER_TOKEN_FILE": str(data / "browser_bridge.token"),
        "WEB_AGENT_BROWSER_LOG": str(logs / "browser_bridge.log"), "WEB_AGENT_EMBEDDED_WORKER": "false",
        "WEB_AGENT_TOKEN": "isolated-native-validation-" + uuid.uuid4().hex,
        "WEB_AGENT_NATIVE_BROWSER_IPC_ENABLED": "true"})
    for package in ("02_web_mcp", "03_web_worker", "04_web_canvas", "05_web_memory", "06_web_tools", "07_web_config", "09_web_logs"):
        sys.path.insert(0, str(ROOT / package))
    import httpx
    from fastapi.testclient import TestClient
    from web_config.settings import Settings
    from web_mcp.server import create_app
    from web_tools import browser_bridge
    from web_tools.browser_client import BrowserBridgeClient, BrowserBridgeError
    from web_tools.native_browser_ipc import NativeBrowserClient
    from web_tools.unified_browser import UnifiedBrowserService
    from web_tools.xiaoruo_browser_adapter import NativeXiaoruoBrowserAdapter

    settings = replace(Settings.load(), orchestration_mode="chatgpt_direct", oauth_enabled=False,
                       embedded_worker=False)
    if settings.data_root.resolve() != data.resolve() or browser_bridge.DATA_ROOT.resolve() != data.resolve():
        raise RuntimeError("isolated_data_root_mismatch")
    class ObservedNativeClient(NativeBrowserClient):
        """Record bounded native task diagnostics while executing the real client."""
        async def execute(self, action, arguments=None, timeout=30):
            value = await super().execute(action, arguments, timeout)
            task = dict(value.get("native_task") or {})
            result = task.get("result")
            if isinstance(result, str):
                try:
                    task["result"] = json.loads(result)
                except ValueError:
                    pass
            record = safe({"action": action, "task_id": value.get("task_id"), "status": value.get("status"),
                "ok": value.get("ok"), "error": value.get("error"), "native_task": task,
                "image_count": len(value.get("images", []))})
            with (run / "native-tasks.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            return value

    browser_bridge.bridge._native_browser_adapter = NativeXiaoruoBrowserAdapter(data_root=data, client_factory=ObservedNativeClient)
    browser_bridge.unified = UnifiedBrowserService(data / "unified_browser_receipts.sqlite3",
        browser_bridge.bridge.browser_instances, browser_bridge.bridge.browser_dispatch,
        browser_bridge.bridge.upload_desktop_preflight)
    original_call = BrowserBridgeClient.call

    async def authenticated_asgi_call(self, endpoint, payload=None, timeout=30.0):
        # The real bridge routes and token dependency still execute. The only
        # substitution is HTTP transport, so this creates no listening socket.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser_bridge.app),
            base_url="http://127.0.0.1:48272", headers={"X-Xiaoruo1-Browser-Token": browser_bridge.BRIDGE_TOKEN},
            timeout=httpx.Timeout(timeout), trust_env=False) as client:
            response = await client.post(endpoint, json=payload or {})
        if response.status_code >= 400:
            raise BrowserBridgeError("Authenticated isolated bridge request was rejected")
        return response.json()

    BrowserBridgeClient.call = authenticated_asgi_call
    try:
        with TestClient(create_app(settings)) as client:
            # Verify both authentication boundaries without printing either key.
            if client.post("/mcp", json={}).status_code != 401:
                raise RuntimeError("mcp_auth_boundary_failed")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser_bridge.app),
                    base_url="http://127.0.0.1:48272") as unauthenticated:
                if (await unauthenticated.post("/v1/browsers", json={})).status_code != 401:
                    raise RuntimeError("bridge_auth_boundary_failed")
            session = ASGISession(client, settings.token)
            await session.initialize()
            yield session
    finally:
        BrowserBridgeClient.call = original_call


class Blocked(RuntimeError):
    pass


class Harness:
    def __init__(self, session, run, mode, browser_id=None, fixture=FIXTURE):
        self.session, self.run, self.mode = session, run, mode
        self.browser_id, self.fixture = browser_id, fixture
        self.trace, self.checks, self.artifacts = [], [], []
        self.owned, self.originals, self.original_active = {}, {}, None
        self.uncertain, self.blocked, self.finished = False, False, False
        self.engine_status = {"cef": {"passed": False}, "lightpanda": {"passed": False}}
        self.compatibility_limitations = []
        self.requests = {}
        self.run.mkdir(parents=True, exist_ok=True)
        self.save()

    def save(self):
        passed = self.finished and all(value["passed"] for value in self.engine_status.values()) and all(
            check["passed"] for check in self.checks if check.get("required", True)) and not self.uncertain and not self.blocked
        value = {"passed": passed, "validation_mode": self.mode, "engines": self.engine_status,
            "browser_id": self.browser_id, "fixture_url": self.fixture, "original_tabs": self.originals,
            "original_active_tab": self.original_active, "owned_tabs": self.owned,
            "uncertain": self.uncertain, "blocked": self.blocked, "finished": self.finished,
            "checks": self.checks, "calls": self.trace, "artifacts": self.artifacts,
            "compatibility_limitations": self.compatibility_limitations}
        target = self.run / "evidence.json"
        temporary = self.run / "evidence.pending.json"
        temporary.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def check(self, name, passed, detail=None, *, engine=None, required=True, kind="positive"):
        self.checks.append({"name": name, "passed": bool(passed), "engine": engine,
            "required": required, "kind": kind, "detail": safe(detail)})
        self.save()

    async def call(self, label, tool, arguments):
        # Persist intent before dispatch. An interrupted script therefore does
        # not make a possibly submitted write disappear from its evidence.
        entry = {"sequence": len(self.trace) + 1, "label": label, "tool": tool,
            "utc": datetime.now(timezone.utc).isoformat(), "arguments": safe(arguments), "phase": "dispatching"}
        self.trace.append(entry)
        self.save()
        try:
            reply = await self.session.call_tool(tool, arguments)
            value = structured(reply)
            entry.update(phase="returned", reply=safe(value))
            self.save()
            return unwrap(value)
        except BaseException as error:
            entry.update(phase="interrupted_or_error", error_type=type(error).__name__)
            if tool == "browser_control" and arguments.get("action") in WRITES:
                self.uncertain = True
            self.save()
            raise

    async def discover(self, label="instances"):
        return await self.call(label, "browser_instances", {"timeout": 10000})

    async def control(self, label, action, tab=None, args=None, *, restoring=False):
        arguments = dict(args or {})
        request = {"browser_id": self.browser_id, "action": action, "arguments": arguments, "timeout": 30000}
        if tab is not None:
            request["tab_id"] = str(tab)
        if action in WRITES:
            if self.uncertain or self.blocked:
                raise Blocked("write_stopped_after_unknown_or_busy_desktop")
            if action != "open_tab" and str(tab) not in set(self.owned.values()) and not (
                    restoring and action == "activate_tab" and str(tab) == self.original_active):
                raise RuntimeError("write_target_is_not_an_owned_tab")
            inventory = await self.discover(label + "_generation")
            candidates = [item for item in inventory.get("browsers", []) if item.get("browser_id") == self.browser_id and item.get("connected")]
            if len(candidates) != 1:
                raise Blocked("selected_native_instance_unavailable")
            arguments["expected_generation"] = candidates[0]["connection_generation"]
            request.update(operation_id="native-validation-" + uuid.uuid4().hex, confirm=True)
        self.requests[label] = request
        result = await self.call(label, "browser_control", request)
        wait_deadline = time.monotonic() + 45
        # Retry only an explicit pre-dispatch rejection after the real desktop
        # says it is idle. This never resets the takeover controller or calls an
        # input tool. A submitted/unknown operation is never replayed.
        while action in WRITES and result.get("evidence", {}).get("executed") is False and not result.get("ok"):
            code = (result.get("error") or {}).get("code", "")
            if not code.startswith(("windows_", "desktop_", "upload_desktop_", "browser_transport_unavailable")):
                break
            readiness = await self.call(label + "_desktop_control_status", "windows_control_status", {})
            state = find(readiness, "state")
            if state in {"ready", "ai"}:
                result = await self.call(label + "_same_key_after_idle", "browser_control", request)
                break
            if time.monotonic() >= wait_deadline or state not in {"user", "busy", "waiting"}:
                self.blocked = True
                self.save()
                raise Blocked("desktop_not_idle_within_45_seconds")
            await asyncio.sleep(min(5, max(.1, wait_deadline - time.monotonic())))
        if action in WRITES and (result.get("status") in UNKNOWN or result.get("evidence", {}).get("executed") is None):
            self.uncertain = True
            self.save()
        return result

    async def tabs(self, label):
        result = await self.control(label, "list_tabs")
        if not result.get("ok"):
            raise RuntimeError("native_tabs_unavailable")
        return (result.get("result") or {}).get("tabs", [])

    @staticmethod
    def tab_id(tab):
        return str(tab.get("tab_id", tab.get("id", "")))

    @classmethod
    def snapshot(cls, tabs):
        return {cls.tab_id(tab): digest(str(tab.get("url") or tab.get("pending_url") or tab.get("pendingUrl") or "")) for tab in tabs}

    async def task_status(self, label, response, engine):
        task_id = find(response, "native_task_id")
        if not task_id:
            self.check(label, False, "No native task ID was exposed", engine=engine)
            return
        status = await self.control(label, "task_status", args={"task_id": task_id})
        self.check(label, status.get("ok") is True and find(status, "native_terminal") is True
                   and find(status, "native_state") == "succeeded", {"native_task_id": task_id, "reply": status}, engine=engine)

    def screenshot(self, label, response, engine, required=True):
        path = find(response, "path")
        valid = False
        metadata = {"reply_ok": response.get("ok"), "native_task_id": find(response, "native_task_id"),
            "reply": response}
        diagnostics = self.run / "native-tasks.jsonl"
        if diagnostics.is_file() and metadata["native_task_id"]:
            for line in reversed(diagnostics.read_text(encoding="utf-8").splitlines()):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("task_id") == metadata["native_task_id"]:
                    metadata["native_diagnostic"] = record
                    break
        if response.get("ok") and isinstance(path, str):
            source = Path(path).resolve()
            allowed = self.run / "isolated-data/browser_screenshots" if self.mode == "isolated" else ROOT / "08_web_data/browser_screenshots"
            try:
                source.relative_to(allowed.resolve())
                body = source.read_bytes()
                if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) <= 16 * 1024 * 1024 and len(body) >= 24:
                    width, height = struct.unpack(">II", body[16:24])
                    destination = self.run / "screenshots" / (label + ".png")
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copyfile(source, destination)
                    metadata.update(path=str(destination), sha256=hashlib.sha256(body).hexdigest(),
                        size_bytes=len(body), width=width, height=height)
                    self.artifacts.append(metadata)
                    valid = width > 0 and height > 0
            except (OSError, ValueError, struct.error):
                pass
        self.check(label, valid, metadata, engine=engine, required=required)

    async def select_owned_for_input(self, name, tab, phase):
        selected = await self.control(name + "_select_owned_before_" + phase, "activate_tab", tab)
        tabs = await self.tabs(name + "_active_tab_before_" + phase)
        target = next((item for item in tabs if self.tab_id(item) == tab), {})
        valid = selected.get("ok") is True and target.get("active") is True
        self.check(name + "_owned_tab_active_before_" + phase, valid,
            {"tab_id": tab, "active": target.get("active"), "selection_reply": selected}, engine=name)
        return valid

    async def engine(self, name, wire_engine):
        created = await self.control(name + "_create_owned", "open_tab", args={"url": self.fixture, "engine": wire_engine})
        tab = str((created.get("result") or {}).get("tab_id", ""))
        good = created.get("ok") is True and bool(tab) and tab not in self.originals and tab not in self.owned.values()
        self.check(name + "_create_exact_id", good, created, engine=name)
        if not good:
            return
        self.owned[name] = tab
        self.save()
        await self.task_status(name + "_completed_task_status", created, name)
        request = self.requests[name + "_create_owned"]
        duplicate = await self.call(name + "_same_operation_receipt", "browser_control", request)
        self.check(name + "_same_operation_does_not_redispatch", duplicate.get("duplicate_call") is True
            and duplicate.get("redispatched") is False and duplicate.get("ok") is True, duplicate, engine=name)
        navigated = await self.control(name + "_navigate_owned", "navigate", tab,
            {"url": self.fixture + "?native_verify=" + self.run.name + "-" + name})
        self.check(name + "_navigation_terminal_ready", navigated.get("ok") is True
            and find(navigated, "native_state") == "succeeded", navigated, engine=name)
        if not navigated.get("ok") or self.uncertain:
            return
        before = await self.control(name + "_read_fixture", "read", tab, {"max_chars": 20000})
        self.check(name + "_read_fixture", before.get("ok") is True
            and "Unified browser interaction fixture" in str((before.get("result") or {}).get("text", "")), before, engine=name)
        marker = "native" + name + uuid.uuid4().hex[:10]
        if not await self.select_owned_for_input(name, tab, "fill"):
            return
        typed = await self.control(name + "_fill_owned", "type", tab,
            {"by": "css", "selector": "#dynamic-input", "text": marker, "clear": True})
        self.check(name + "_fill_terminal", typed.get("ok") is True and find(typed, "native_state") == "succeeded", typed, engine=name)
        if not typed.get("ok") or self.uncertain:
            return
        if not await self.select_owned_for_input(name, tab, "click"):
            return
        clicked = await self.control(name + "_click_owned", "click", tab, {"by": "css", "selector": "#dynamic-button"})
        self.check(name + "_click_terminal", clicked.get("ok") is True and find(clicked, "native_state") == "succeeded", clicked, engine=name)
        if not clicked.get("ok") or self.uncertain:
            return
        changed = {}
        for attempt in range(8):
            changed = await self.control(name + "_marker_read_" + str(attempt), "read", tab, {"selector": "#dynamic-state", "max_chars": 20000})
            if changed.get("ok") and marker in str((changed.get("result") or {}).get("text", "")):
                break
            await asyncio.sleep(.25)
        self.check(name + "_marker_observed_after_click", changed.get("ok") is True
            and marker in str((changed.get("result") or {}).get("text", "")), {"expected_marker": marker, "reply": changed}, engine=name)
        scrolled = await self.control(name + "_scroll_owned", "scroll", tab, {"x": 0, "y": 700})
        observed = scroll_position(scrolled)
        self.check(name + "_scroll_observed_position", scrolled.get("ok") is True
            and find(scrolled, "native_state") == "succeeded" and observed is not None
            and abs(observed["x"]) <= 1 and abs(observed["y"] - 700) <= 1,
            {"requested_position": {"x": 0, "y": 700}, "observed_position": observed, "reply": scrolled}, engine=name)
        if self.uncertain:
            return
        if name == "cef":
            page = await self.control("cef_page_pixels", "screenshot", tab, {"mode": "page"})
            self.screenshot("cef_page_pixels", page, name)
        else:
            negative = await self.control("lightpanda_page_pixels_rejected", "screenshot", tab, {"mode": "page"})
            self.check("lightpanda_page_pixels_explicitly_unsupported", negative.get("ok") is False
                and (negative.get("error") or {}).get("code") == "unsupported"
                and negative.get("evidence", {}).get("executed") is False and not find(negative, "native_task_id"),
                {"reply": negative, "pixel_capture_fulfilled": False}, engine=name, kind="negative")
            selected = await self.control("lightpanda_select_owned_for_reader", "activate_tab", tab)
            self.check("lightpanda_select_owned", selected.get("ok") is True, selected, engine=name)
            if selected.get("ok") and not self.uncertain:
                window = await self.control("lightpanda_native_reader_window", "screenshot", tab, {"mode": "window"})
                self.screenshot("lightpanda_native_reader_window", window, name)

    async def execute(self):
        inventory = await self.discover()
        candidates = [item for item in inventory.get("browsers", []) if item.get("adapter") == "native_ipc" and item.get("connected")]
        if self.browser_id:
            candidates = [item for item in candidates if item.get("browser_id") == self.browser_id]
        if len(candidates) != 1:
            raise Blocked("select_exactly_one_existing_native_browser")
        self.browser_id = candidates[0]["browser_id"]
        self.check("selected_existing_native_instance", True, candidates[0])
        tabs = await self.tabs("original_tabs")
        self.originals = self.snapshot(tabs)
        active = [self.tab_id(tab) for tab in tabs if tab.get("active") is True]
        self.original_active = active[0] if len(active) == 1 else None
        self.save()
        for name, engine in (("cef", "chromium"), ("lightpanda", "lightpanda")):
            if self.uncertain or self.blocked:
                break
            await self.engine(name, engine)
        if len(self.owned) == 2 and not self.uncertain:
            # A normal CEF background target under another CEF tab is a
            # separate scenario from a hidden CEF renderer under Lightpanda.
            # The first run's cross-engine timeout remains retained evidence.
            auxiliary = await self.control("cef_background_auxiliary_create", "open_tab", args={"url": self.fixture, "engine": "chromium"})
            auxiliary_id = str((auxiliary.get("result") or {}).get("tab_id", ""))
            valid = auxiliary.get("ok") is True and auxiliary_id and auxiliary_id not in self.originals and auxiliary_id not in self.owned.values()
            self.check("cef_background_auxiliary_exact_id", valid, auxiliary, engine="cef")
            if not valid:
                return
            self.owned["cef_auxiliary"] = auxiliary_id
            self.save()
            auxiliary_selected = await self.control("cef_background_auxiliary_select", "activate_tab", auxiliary_id)
            self.check("cef_background_auxiliary_active", auxiliary_selected.get("ok") is True, auxiliary_selected, engine="cef")
            tabs = await self.tabs("background_capture_tab_state")
            cef = next((tab for tab in tabs if self.tab_id(tab) == self.owned["cef"]), {})
            self.check("cef_background_tab_is_not_active", cef.get("active") is False, cef, engine="cef")
            if cef.get("active") is False:
                background = await self.control("cef_background_pixels", "screenshot", self.owned["cef"], {"mode": "page"})
                # Background capture is a separately disclosed native-runtime
                # capability diagnostic. Core acceptance requires actual CEF
                # pixels, LP reader pixels, truthful timeout and lock recovery.
                self.screenshot("cef_background_pixels", background, "cef", required=False)
                if background.get("ok") is not True:
                    code = (background.get("error") or {}).get("code")
                    self.compatibility_limitations.append({"capability":"cef_inactive_tab_pixels", "status":"not_verified", "native_error":code,
                        "detail":"Native v72 reports a screenshot timeout for a hidden CEF tab; no implicit activation or alternate pixels were returned"})
                    self.check("cef_background_timeout_reported_truthfully", code == "timeout" and find(background, "native_state") == "timed_out", background, engine="cef", kind="negative")
                    selected = await self.control("cef_select_after_capture_timeout", "activate_tab", self.owned["cef"])
                    self.check("cef_task_lock_released_after_capture_timeout", selected.get("ok") is True, selected, engine="cef")
                    recovered = await self.control("cef_page_capture_after_timeout", "screenshot", self.owned["cef"], {"mode":"page"})
                    self.screenshot("cef_page_capture_after_timeout", recovered, "cef")

    async def cleanup(self):
        if not self.browser_id or not self.originals:
            return
        try:
            current = self.snapshot(await self.tabs("original_tabs_before_cleanup"))
            self.check("original_tab_ids_and_url_hashes_preserved_before_cleanup",
                all(current.get(tab) == url_hash for tab, url_hash in self.originals.items()))
            for name, tab in list(self.owned.items())[::-1]:
                if self.uncertain or self.blocked:
                    self.check(name + "_cleanup_not_fulfilled", False, {"tab_id": tab, "reason": "unknown_write_or_desktop_blocked"})
                    continue
                closed = await self.control(name + "_close_owned", "close_tab", tab)
                self.check(name + "_owned_tab_closed", closed.get("ok") is True, closed)
            if self.original_active and not self.uncertain and not self.blocked:
                tabs = await self.tabs("selection_before_restore")
                original = next((tab for tab in tabs if self.tab_id(tab) == self.original_active), None)
                if original and original.get("active") is not True:
                    restored = await self.control("restore_original_active_tab", "activate_tab", self.original_active, restoring=True)
                    self.check("original_active_tab_restored", restored.get("ok") is True, restored)
            final_tabs = await self.tabs("final_tabs")
            final = self.snapshot(final_tabs)
            self.check("original_tab_ids_and_url_hashes_preserved",
                all(final.get(tab) == url_hash for tab, url_hash in self.originals.items()))
            self.check("all_owned_tabs_closed", not set(self.owned.values()).intersection(final))
        except BaseException as error:
            self.check("cleanup_completed", False, {"error_type": type(error).__name__})

    def finish(self):
        for name in self.engine_status:
            checks = [check for check in self.checks if check.get("engine") == name and check.get("required", True)]
            self.engine_status[name] = {"passed": bool(checks) and all(check["passed"] for check in checks),
                "required_checks": len(checks), "passed_checks": sum(check["passed"] for check in checks),
                "page_pixels_supported": name == "cef"}
        self.finished = True
        self.save()


async def main(args):
    run = args.output.resolve() if args.output else BASE / (args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, exist_ok=True)
    harness = None
    try:
        async with public_session(args.mode, run) as session:
            harness = Harness(session, run, args.mode, args.browser_id, args.fixture)
            try:
                await harness.execute()
            except Blocked as error:
                harness.blocked = True
                harness.check("run_not_blocked", False, {"reason": str(error)})
            except BaseException as error:
                harness.check("scenario_completed", False, {"error_type": type(error).__name__})
            finally:
                await harness.cleanup()
                harness.finish()
    except BaseException as error:
        if harness is None:
            (run / "evidence.json").write_text(json.dumps({"passed": False, "validation_mode": args.mode,
                "engines": {"cef": {"passed": False}, "lightpanda": {"passed": False}},
                "error_type": type(error).__name__, "phase": "initialization"}, indent=2), encoding="utf-8")
        else:
            harness.check("session_closed_cleanly", False, {"error_type": type(error).__name__})
            harness.finish()
    evidence = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
    print(json.dumps({"passed": evidence.get("passed", False), "validation_mode": args.mode,
        "engines": evidence.get("engines"), "blocked": evidence.get("blocked", False),
        "uncertain": evidence.get("uncertain", False), "evidence": str(run / "evidence.json"),
        "failed_checks": [{"name": check.get("name"), "error_code": find(check.get("detail"), "code"),
            "native_state": find(check.get("detail"), "native_state"), "task_id": find(check.get("detail"), "native_task_id")}
            for check in evidence.get("checks", []) if not check.get("passed")][:12]}, ensure_ascii=False))
    return 0 if evidence.get("passed") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("isolated", "production"), default="isolated")
    parser.add_argument("--browser-id", help="Exact native browser ID from public discovery")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", choices=(FIXTURE, "https://mcp.example.com/upload-test/fixture.html"), default=FIXTURE)
    raise SystemExit(asyncio.run(main(parser.parse_args())))