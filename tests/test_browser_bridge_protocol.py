from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient


def test_edge_extension_websocket_round_trip():
    from web_tools import browser_bridge

    headers = {
        "origin": "chrome-extension://xiaoruo1-test",
        "X-Xiaoruo1-Browser-Token": browser_bridge.BRIDGE_TOKEN,
    }
    with TestClient(browser_bridge.app) as client:
        with client.websocket_connect(
            f"/ws?token={browser_bridge.BRIDGE_TOKEN}",
            headers=headers,
        ) as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "hello"
            websocket.send_json({"type": "hello_ack", "version": "test"})

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: client.post(
                        "/v1/tabs",
                        headers={"X-Xiaoruo1-Browser-Token": browser_bridge.BRIDGE_TOKEN},
                    )
                )
                command = websocket.receive_json()
                assert command["method"] == "list_tabs"
                websocket.send_json({"id": command["id"], "ok": True, "result": {"tabs": []}})
                response = future.result(timeout=5)

            assert response.status_code == 200
            assert response.json() == {"browser": "edge-extension", "tabs": []}


def test_local_download_fallback_uses_edge_configured_directory(tmp_path, monkeypatch):
    from web_tools import browser_bridge

    download = tmp_path / "glycam-structure.zip"
    download.write_bytes(b"zip-content")
    monkeypatch.setattr(browser_bridge, "_edge_download_directories", lambda: [tmp_path])

    records = browser_bridge._local_downloads(10)

    assert records
    assert records[0]["state"] == "complete"
    assert records[0]["file_path"] == str(download.resolve())


def test_duplicate_tab_script_shell_is_not_accepted_as_page_text():
    from web_tools.browser_bridge import _is_placeholder_duplicate_read

    assert _is_placeholder_duplicate_read(
        {
            "recovery": "duplicate_tab",
            "text": "(function(e){document.currentScript;__oai_logHTML();requestAnimationFrame(e)})()",
        }
    ) is True
    assert _is_placeholder_duplicate_read(
        {"recovery": "duplicate_tab", "text": "GLYCAM build completed with a structure"}
    ) is False


def test_click_returns_filesystem_download_when_extension_is_stale(tmp_path, monkeypatch):
    from web_tools import browser_bridge

    monkeypatch.setattr(browser_bridge, "_edge_download_directories", lambda: [tmp_path])

    async def fake_call(method, params=None, timeout=30.0):
        assert method == "click"
        (tmp_path / "glycam-structure.zip").write_bytes(b"zip-content")
        return {"verification": "target_state_changed"}

    monkeypatch.setattr(browser_bridge.bridge, "call", fake_call)
    request = browser_bridge.LocatorRequest(tab_id="1", by="css", selector="#bulk-download")
    result = asyncio.run(browser_bridge.bridge.click(request))

    assert result["download"]["state"] == "complete"
    assert result["download"]["file_path"].endswith("glycam-structure.zip")
    assert result["downloads"][0]["source"] == "edge_download_directory"


def test_click_recovers_download_after_extension_timeout(tmp_path, monkeypatch):
    from web_tools import browser_bridge

    monkeypatch.setattr(browser_bridge, "_edge_download_directories", lambda: [tmp_path])

    async def fake_call(method, params=None, timeout=30.0):
        assert method == "click"
        (tmp_path / "glycam-timeout-recovered.zip").write_bytes(b"zip-content")
        raise browser_bridge.BrowserBridgeError("Edge 页面脚本执行超时")

    monkeypatch.setattr(browser_bridge.bridge, "call", fake_call)
    request = browser_bridge.LocatorRequest(tab_id="1", by="css", selector="#bulk-download")
    result = asyncio.run(browser_bridge.bridge.click(request))

    assert result["download"]["file_path"].endswith("glycam-timeout-recovered.zip")
    assert result["verification"] == "download_observed_after_extension_error"


def test_click_timeout_is_structured_when_action_was_dispatched(monkeypatch):
    from web_tools import browser_bridge

    async def fake_call(method, params=None, timeout=30.0):
        if method == "click":
            raise browser_bridge.BrowserBridgeError("点击已发送，但 3000ms 内未观察到页面状态变化")
        if method == "read_page":
            return {"tab_id": "1", "url": "https://example.test", "text": "after click"}
        raise AssertionError(method)

    monkeypatch.setattr(browser_bridge.bridge, "call", fake_call)
    request = browser_bridge.LocatorRequest(tab_id="1", by="css", selector="#slow-spa-action")
    result = asyncio.run(browser_bridge.bridge.click(request))

    assert result["state_change_observed"] is False
    assert result["follow_up_required"] is True
    assert result["after_click"]["text"] == "after click"


def test_click_locator_errors_are_not_downgraded_to_retry_checkpoint(monkeypatch):
    from web_tools import browser_bridge

    async def fake_call(method, params=None, timeout=30.0):
        assert method == "click"
        raise browser_bridge.BrowserBridgeError(
            "点击已发送，但 3000ms 内未观察到页面状态变化；执行层错误：找不到可点击目标"
        )

    monkeypatch.setattr(browser_bridge.bridge, "call", fake_call)
    request = browser_bridge.LocatorRequest(tab_id="1", by="css", selector="#missing")

    try:
        asyncio.run(browser_bridge.bridge.click(request))
    except browser_bridge.BrowserBridgeError as exc:
        assert "执行层错误" in str(exc)
    else:
        raise AssertionError("locator error was incorrectly converted to a retry checkpoint")


def test_read_retries_transient_startup_page_type_error(monkeypatch):
    from web_tools import browser_bridge

    calls = {"read_page": 0}

    async def fake_call(method, params=None, timeout=30.0):
        if method == "read_page":
            calls["read_page"] += 1
            if calls["read_page"] == 1:
                raise browser_bridge.BrowserBridgeError(
                    "Edge 安全边界禁止读取此页面类型；请切换到普通 HTTP、HTTPS 或已允许文件访问的 file 页面"
                )
            return {"tab_id": "42", "url": "https://glycam.org/txt/", "text": "GLYCAM"}
        if method == "list_tabs":
            return {"tabs": [{"tab_id": "42", "url": "https://glycam.org/txt/"}]}
        raise AssertionError(method)

    monkeypatch.setattr(browser_bridge.bridge, "call", fake_call)
    result = asyncio.run(
        browser_bridge.bridge.read(
            browser_bridge.ReadRequest(tab_id="42", max_chars=1000, settle_ms=0, mode="fast")
        )
    )

    assert result["url"] == "https://glycam.org/txt/"
    assert calls["read_page"] == 2