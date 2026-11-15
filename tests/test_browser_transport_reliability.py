"""Transport deadlines and socket isolation must not replay browser commands."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from test_edge_background_routes import current_bridge


class Socket:
    client = None

    def __init__(self, bridge, connection, *, stall=False, fail=False):
        self.bridge, self.connection = bridge, connection
        self.stall, self.fail = stall, fail
        self.started = asyncio.Event()
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, message):
        self.sent.append(message)
        self.started.set()
        if self.fail:
            raise ConnectionError("Test socket send failed")
        if self.stall:
            await asyncio.Event().wait()
        if message.get("id"):
            await self.bridge._receive({"id": message["id"], "ok": True,
                "result": {"verified": True, "socket": self.connection}}, self.connection)


def register(bridge, key, **options):
    socket = Socket(bridge, key, **options)
    bridge._connections[key] = {"websocket": socket}
    bridge._websocket, bridge._connection_id = socket, key
    return socket


async def test_timeout_includes_a_blocked_socket_send_and_clears_pending(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    socket = register(bridge, "first", stall=True)
    with pytest.raises(current_bridge.BrowserBridgeError, match="执行超时"):
        await asyncio.wait_for(bridge.call("click", timeout=.03, _connection_id="first"), .3)
    assert len(socket.sent) == 1
    assert not bridge._pending and not bridge._pending_connections
    socket.stall = False
    assert (await bridge.call("read_page", timeout=.2, _connection_id="first"))["verified"]
    assert [message["method"] for message in socket.sent] == ["click", "read_page"]


async def test_same_socket_send_queue_uses_the_callers_deadline(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    socket = register(bridge, "first", stall=True)
    original = asyncio.create_task(bridge.call("click", timeout=10, _connection_id="first"))
    await socket.started.wait()
    try:
        with pytest.raises(current_bridge.BrowserBridgeError, match="执行超时"):
            await asyncio.wait_for(bridge.call("type_text", timeout=.03, _connection_id="first"), .3)
        assert [message["method"] for message in socket.sent] == ["click"]
    finally:
        original.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original
    assert not bridge._pending and not bridge._pending_connections


async def test_one_stalled_browser_never_holds_another_browsers_send_queue(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    first = register(bridge, "first", stall=True)
    second = register(bridge, "second")
    original = asyncio.create_task(bridge.call("click", timeout=10, _connection_id="first"))
    await first.started.wait()
    try:
        response = await asyncio.wait_for(bridge.call("read_page", timeout=.2, _connection_id="second"), .3)
        assert response["socket"] == "second"
        assert [message["method"] for message in second.sent] == ["read_page"]
        assert len(first.sent) == 1
    finally:
        original.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original
    assert not bridge._pending


async def test_routing_discovery_is_in_the_original_timeout_budget(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    socket = register(bridge, "first", stall=True)
    with pytest.raises(current_bridge.BrowserBridgeError, match="执行超时"):
        await asyncio.wait_for(bridge.call("click", {"tab_id": "unobserved"}, timeout=.03), .3)
    assert [message["method"] for message in socket.sent] == ["list_tabs"]
    assert not bridge._pending and not bridge._pending_connections


async def test_failed_initial_hello_cannot_leave_a_phantom_connected_browser(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    socket = Socket(bridge, "unused", fail=True)
    await bridge.attach(socket)
    assert not bridge.connected
    assert not bridge._connections and not bridge._send_locks


async def test_cancelling_during_hello_removes_the_connection(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    socket = Socket(bridge, "unused", stall=True)
    attached = asyncio.create_task(bridge.attach(socket))
    await socket.started.wait()
    attached.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attached
    assert not bridge.connected
    assert not bridge._connections and not bridge._send_locks


async def test_disconnect_fails_only_its_pending_request_without_fallback(current_bridge):
    bridge = current_bridge.EdgeExtensionBridge()
    first = register(bridge, "first", stall=True)
    second = register(bridge, "second")
    original = asyncio.create_task(bridge.call("click", timeout=1, _connection_id="first"))
    await first.started.wait()
    # The send can have entered the socket already. Reconnection must not move it.
    await bridge.detach(first, "first")
    original.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original
    assert bridge._websocket is second and bridge._connection_id == "second"
    assert not second.sent
    assert not bridge._pending and not bridge._pending_connections


def client_for(tmp_path, monkeypatch, response):
    from web_tools import browser_client
    token = tmp_path / "test-token"
    token.write_text("test-only-browser-bridge-token", encoding="utf-8")
    original = httpx.AsyncClient
    monkeypatch.setattr(browser_client.httpx, "AsyncClient", lambda *a, **kw:
        original(*a, **kw, transport=httpx.MockTransport(lambda _: response)))
    client = browser_client.BrowserBridgeClient(SimpleNamespace(
        browser_bridge_url="http://test.invalid", browser_bridge_token_file=token))
    return browser_client, client


@pytest.mark.parametrize("status,code,retryable", [
    (401, "browser_bridge_authentication_failed", False),
    (403, "browser_bridge_authentication_failed", False),
    (404, "browser_bridge_request_rejected", False),
    (422, "browser_bridge_request_rejected", False),
    (429, "browser_bridge_http_error", True),
    (503, "browser_bridge_http_error", True),
])
async def test_http_statuses_have_precise_classification_without_reflecting_private_bodies(tmp_path, monkeypatch, status, code, retryable):
    module, client = client_for(tmp_path, monkeypatch,
        httpx.Response(status, json={"detail": "PRIVATE_FORM_TEXT token=PRIVATE_TOKEN"}))
    with pytest.raises(module.BrowserBridgeError) as caught:
        await client.list_tabs()
    assert caught.value.code == code and caught.value.retryable is retryable
    assert "PRIVATE_FORM_TEXT" not in str(caught.value) and "PRIVATE_TOKEN" not in str(caught.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>PRIVATE_PROXY_PAGE</html>"),
    httpx.Response(200, json=["PRIVATE_UNEXPECTED_RESULT"]),
    httpx.Response(302, headers={"location": "http://other.invalid/"}, json={"detail": "redirect"}),
])
async def test_non_json_or_wrong_shape_response_cannot_be_claimed_as_success(tmp_path, monkeypatch, response):
    module, client = client_for(tmp_path, monkeypatch, response)
    with pytest.raises(module.BrowserBridgeError) as caught:
        await client.list_tabs()
    assert caught.value.code == "browser_bridge_invalid_response"
    assert "PRIVATE" not in str(caught.value)


async def test_missing_bridge_credentials_is_not_a_retryable_network_failure(tmp_path):
    from web_tools.browser_client import BrowserBridgeClient, BrowserBridgeError
    client = BrowserBridgeClient(SimpleNamespace(browser_bridge_url="http://test.invalid",
        browser_bridge_token_file=tmp_path / "not-created"))
    with pytest.raises(BrowserBridgeError) as caught:
        await client.list_tabs()
    assert caught.value.code == "browser_bridge_credentials_unavailable" and caught.value.retryable is False
