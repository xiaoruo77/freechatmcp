from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from web_tools.unified_browser import UnifiedBrowserService, WIRE, WRITE_ACTIONS

BROWSER = "browser_12345678-1234-1234-1234-123456789abc"
GEN = "connection-generation-1"


class Runtime:
    def __init__(self):
        self.generation = GEN
        self.connected = True
        self.calls = []
        self.tabs = [{"tab_id": "17", "url": "https://example.com/", "active": True},
                     {"tab_id": "18", "url": "https://example.com/other", "active": False}]
        self.handler = None
        self.discoveries = 0

    async def discover(self):
        self.discoveries += 1
        return {"ok": True, "browsers": [{"browser_id": BROWSER,
            "browser_kind": "edge", "connection_generation": self.generation,
            "connected": self.connected, "supported_actions": list(WIRE)}]}

    async def dispatch(self, browser_id, generation, method, payload, timeout):
        assert browser_id == BROWSER and generation == self.generation
        assert timeout > 0
        self.calls.append((method, dict(payload)))
        if method == "list_tabs":
            return {"tabs": [dict(t) for t in self.tabs]}
        if self.handler:
            return await self.handler(method, payload)
        if method == "read_page":
            return {"text": "actual page", "url": "https://example.com/"}
        return {"verified": True, "tab_id": payload.get("tab_id")}


@pytest.fixture
def setup(tmp_path):
    runtime = Runtime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover, runtime.dispatch)
    return runtime, service


def args(**extra):
    return {"expected_generation": GEN, **extra}


async def write(service, action="click", op_id="operation-0001", tab="17", **arguments):
    return await service.control(BROWSER, action, tab, args(**arguments), op_id, True, 2000)


@pytest.mark.parametrize("action", list(WIRE))
async def test_discovery_advertises_each_supported_action(setup, action):
    _, service = setup
    discovered = await service.instances()
    assert action in discovered["browsers"][0]["supported_actions"]


@pytest.mark.parametrize("browser,tab,action,arguments,key,confirm,code", [
    ("", "17", "read", {}, None, False, "browser_id_required"),
    (BROWSER, None, "read", {}, None, False, "browser_tab_required"),
    (BROWSER, "unowned", "read", {}, None, False, "browser_tab_not_owned"),
    (BROWSER, "17", "evaluate", {"script": "steal()"}, None, False, "browser_action_unsupported"),
    (BROWSER, "17", "read", {"script": "anything"}, None, False, "browser_arguments_invalid"),
    (BROWSER, "17", "click", args(), "operation-0001", False, "browser_confirmation_required"),
    (BROWSER, "17", "click", args(), None, True, "browser_operation_id_required"),
    (BROWSER, "17", "click", {}, "operation-0001", True, "browser_generation_required"),
    (BROWSER, "17", "click", {"expected_generation": "stale"}, "operation-0001", True, "browser_generation_changed"),
    (BROWSER, "17", "navigate", args(url="file:///private"), "operation-0001", True, "browser_url_denied"),
    (BROWSER, "17", "navigate", args(url="https://user:pass@example.com"), "operation-0001", True, "browser_url_denied"),
    (BROWSER, "17", "read", {"frame_id": True}, None, False, "browser_arguments_invalid"),
    (BROWSER, "17", "type", args(text="x", clear="true"), "operation-0001", True, "browser_arguments_invalid"),
])
async def test_denies_unauthorized_or_ambiguous_calls(setup, browser, tab, action, arguments, key, confirm, code):
    runtime, service = setup
    response = await service.control(browser, action, tab, arguments, key, confirm)
    assert response["error"]["code"] == code
    assert response["evidence"]["executed"] is False
    assert all(method == "list_tabs" for method, _ in runtime.calls)


async def test_real_response_read_observation_and_no_hidden_launch(setup):
    runtime, service = setup
    response = await service.control(BROWSER, "read", "17", {"include_frames": True})
    assert response["ok"] and response["result"]["text"] == "actual page"
    assert response["observation_id"].startswith("observation_")
    assert [m for m, _ in runtime.calls] == ["list_tabs", "read_page"]


async def test_duplicate_is_receipt_read_and_conflicting_key_rejected(setup):
    runtime, service = setup
    first = await write(service)
    second = await write(service)
    assert first["ok"] and second["duplicate_call"] and not second["redispatched"]
    assert sum(m == "click" for m, _ in runtime.calls) == 1
    conflict = await write(service, action="type", text="different")
    assert conflict["error"]["code"] == "browser_operation_conflict"


async def test_unverified_write_fences_other_write_but_read_can_reconcile(setup):
    runtime, service = setup
    first = await write(service, action="press", op_id="operation-press", key="Enter")
    assert first["status"] == "dispatched_unverified" and not first["ok"]
    fenced = await write(service, op_id="operation-next")
    assert fenced["error"]["code"] == "browser_target_fenced"
    missing = await service.control(BROWSER, "reconcile", "17", {
        "operation_id": "operation-press", "decision": "verified", "evidence": "Observed page transition after Enter"}, confirm=True)
    assert missing["error"]["code"] == "browser_reconciliation_observation_required"
    observed = await service.control(BROWSER, "read", "17")
    reconciled = await service.control(BROWSER, "reconcile", "17", {
        "operation_id": "operation-press", "observation_id": observed["observation_id"],
        "decision": "verified", "evidence": "Observed page transition after Enter"}, confirm=True)
    assert reconciled["ok"] and reconciled["status"] == "reconciled_verified"
    assert (await write(service, op_id="operation-next"))["ok"]


async def test_timeout_after_dispatch_is_durable_and_never_replayed(setup, tmp_path):
    runtime, service = setup
    async def slow(method, payload):
        await asyncio.sleep(2)
        return {"verified": True}
    runtime.handler = slow
    started = time.monotonic()
    response = await service.control(BROWSER, "click", "17", args(), "operation-timeout", True, 500)
    assert time.monotonic() - started < 1.2
    assert response["status"] == "outcome_unknown" and not response["retryable"]
    restarted = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover, runtime.dispatch)
    duplicate = await restarted.control(BROWSER, "click", "17", args(), "operation-timeout", True, 500)
    assert duplicate["duplicate_call"] and not duplicate["redispatched"]
    assert sum(m == "click" for m, _ in runtime.calls) == 1
    runtime.connected = False
    offline = await restarted.control(BROWSER, "status", arguments={"operation_id": "operation-timeout"})
    assert offline["status"] == "outcome_unknown"


async def test_cancel_after_dispatch_keeps_target_fence(setup):
    runtime, service = setup
    started = asyncio.Event()
    async def slow(method, payload):
        started.set()
        await asyncio.sleep(10)
    runtime.handler = slow
    pending = asyncio.create_task(write(service, op_id="operation-cancel"))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    stored = await service.control(BROWSER, "status", operation_id="operation-cancel")
    assert stored["status"] == "outcome_unknown"
    fenced = await write(service, op_id="operation-after-cancel")
    assert fenced["error"]["code"] == "browser_target_fenced"


async def test_same_tab_writes_serialize_and_different_tabs_remain_independent(setup):
    runtime, service = setup
    first_started, release_first, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []
    async def handler(method, payload):
        events.append(("start", payload["tab_id"]))
        if payload["tab_id"] == "17" and not first_started.is_set():
            first_started.set()
            await release_first.wait()
        if payload["tab_id"] == "18":
            other_done.set()
        events.append(("end", payload["tab_id"]))
        return {"verified": True}
    runtime.handler = handler
    first = asyncio.create_task(write(service, op_id="operation-concurrent-1"))
    await first_started.wait()
    second = asyncio.create_task(write(service, op_id="operation-concurrent-2"))
    other = asyncio.create_task(write(service, tab="18", op_id="operation-other-tab"))
    await asyncio.wait_for(other_done.wait(), 1)
    assert events == [("start", "17"), ("start", "18"), ("end", "18")]
    release_first.set()
    assert all(r["ok"] for r in await asyncio.gather(first, second, other))
    assert events[-3:] == [("end", "17"), ("start", "17"), ("end", "17")]


async def test_generation_revalidated_after_lock_wait(setup):
    runtime, service = setup
    lock = service.target_lock(BROWSER, "17")
    await lock.acquire()
    pending = asyncio.create_task(write(service))
    while runtime.discoveries < 1:
        await asyncio.sleep(0)
    runtime.generation = "connection-generation-2"
    lock.release()
    response = await pending
    assert response["error"]["code"] == "browser_generation_changed"
    assert not runtime.calls


async def test_real_adapter_failure_never_becomes_verified_read(setup):
    runtime, service = setup
    async def fail(method, payload):
        return {"ok": False, "error": {"code": "real_failure"}}
    runtime.handler = fail
    response = await service.control(BROWSER, "read", "17")
    assert not response["ok"] and not response["evidence"]["verified"]


async def test_preflight_rejects_before_any_write_and_does_not_reserve_target(setup):
    runtime, service = setup
    async def denied():
        raise RuntimeError("Desktop locked or user takeover")
    service.desktop_check = denied
    response = await write(service)
    assert response["evidence"]["executed"] is False
    assert not service.receipts.get("operation-0001")
    assert all(m == "list_tabs" for m, _ in runtime.calls)


async def test_database_lock_is_bounded_without_any_browser_dispatch(setup):
    runtime, service = setup
    with sqlite3.connect(service.receipts.path) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        response = await write(service)
        assert time.monotonic() - started < 1
        assert response["error"]["code"] == "browser_receipt_database_busy"
        assert response["retryable"] and not runtime.calls


async def test_receipts_do_not_persist_page_text_or_inline_images(setup):
    runtime, service = setup
    async def handler(method, payload):
        return {"verified": True, "text": "PRIVATE-PAGE", "data_url": "data:image/png;base64,SECRET", "token": "SECRET-TOKEN"}
    runtime.handler = handler
    response = await write(service)
    assert response["ok"]
    row = service.receipts.get("operation-0001")
    assert all(marker not in row["result"] for marker in ["PRIVATE-PAGE", "SECRET-TOKEN", "base64"])


@pytest.fixture
def staged_bridge(tmp_path, monkeypatch):
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[1]
    path = root / "10_web_deploy/staging/browser-unification-20261027/06_web_tools/web_tools/browser_bridge.py"
    if not path.is_file():
        pytest.skip("Historical deployment staging snapshot is not included in public source")
    monkeypatch.setenv("WEB_AGENT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_AGENT_BROWSER_TOKEN_FILE", str(tmp_path / "browser.token"))
    spec = importlib.util.spec_from_file_location("web_tools._staged_unified_browser_bridge", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_staged_unified_routes_are_authenticated_and_strict(staged_bridge):
    from fastapi.testclient import TestClient
    with TestClient(staged_bridge.app) as client:
        for route in ["/v1/browsers", "/v1/browser/dispatch"]:
            assert client.post(route, json={}).status_code == 401
        headers = {"X-Xiaoruo1-Browser-Token": staged_bridge.BRIDGE_TOKEN}
        discovered = client.post("/v1/browsers", headers=headers, json={})
        assert discovered.status_code == 200 and discovered.json()["browsers"] == []
        assert client.post("/v1/browsers", headers=headers, json={"timeout": True}).status_code == 422
        assert client.post("/v1/browser/dispatch", headers=headers, json={"browser_id": BROWSER,
            "action": "click", "tab_id": "17", "confirm": "true"}).status_code == 422


async def test_staged_identity_discovery_and_explicit_connection_dispatch(staged_bridge, monkeypatch):
    bridge = staged_bridge.EdgeExtensionBridge()
    edge_socket, other_socket = object(), object()
    bridge._connections = {"edge-connection": {"websocket": edge_socket}, "other-connection": {"websocket": other_socket}}
    await bridge._receive({"type": "hello_ack", "browser_family": "edge", "browser_kind": "edge",
        "instance_id": BROWSER.removeprefix("browser_"), "version": "1.9.0",
        "supported_actions": ["read", "click", "not_a_command"]}, "edge-connection")
    browser = (await bridge.browser_instances())["browsers"][0]
    assert browser["browser_id"] == BROWSER and browser["stable_identity"]
    assert browser["supported_actions"] == ["read", "click"]
    legacy = (await bridge.browser_instances())["browsers"][1]
    assert legacy["stable_identity"] is False and "click" not in legacy["supported_actions"]
    calls = []
    async def dispatch(method, payload, timeout, _connection_id=None):
        calls.append(_connection_id)
        return {"text": "real selected adapter response"}
    monkeypatch.setattr(bridge, "call", dispatch)
    response = await bridge.browser_dispatch(BROWSER, "edge-connection", "read_page", {"tab_id": "17"}, 1)
    assert response["text"] == "real selected adapter response" and calls == ["edge-connection"]
    with pytest.raises(staged_bridge.BrowserBridgeError):
        await bridge.browser_dispatch(BROWSER, "other-connection", "click", {"tab_id": "17"}, 1)
    assert calls == ["edge-connection"]


async def test_staged_screenshot_is_private_artifact_not_inline(staged_bridge, monkeypatch, tmp_path):
    import base64
    bridge = staged_bridge.EdgeExtensionBridge()
    bridge._connections[GEN] = {"websocket": object(), "instance_id": BROWSER.removeprefix("browser_")}
    body = b"\x89PNG\r\n\x1a\n" + b"test-image-bytes"
    async def dispatch(*args, **kwargs):
        return {"data_url": "data:image/png;base64," + base64.b64encode(body).decode(), "url": "https://example.com/"}
    monkeypatch.setattr(bridge, "call", dispatch)
    monkeypatch.setattr(staged_bridge, "SCREENSHOT_ROOT", tmp_path)
    response = await bridge.browser_dispatch(BROWSER, GEN, "screenshot", {"tab_id": "17"}, 1)
    assert "data_url" not in response and Path(response["path"]).read_bytes() == body
    assert Path(response["path"]).parent == tmp_path


async def test_staged_legacy_cef_uses_only_verified_peer_identity(staged_bridge):
    bridge = staged_bridge.EdgeExtensionBridge()
    identity={"instance_id": BROWSER.removeprefix("browser_"), "browser_kind":"xiaoruo_browser",
        "identity_source":"verified_loopback_process","browser_family":"chromium-other",
        "manual_instance":True,"engine":"cef","supported_actions":["read","click"]}
    bridge._connections[GEN]={"websocket":object(),"process_identity":identity}
    await bridge._receive({"type":"hello_ack","version":"1.8.4"},GEN)
    current=(await bridge.browser_instances())["browsers"][0]
    assert current["browser_id"]==BROWSER and current["manual_instance"] is True
    assert current["identity_source"]=="verified_loopback_process" and current["browser_kind"]=="xiaoruo_browser"
    assert current["supported_actions"]==["read","click"]