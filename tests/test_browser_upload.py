from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from web_tools.browser_upload import UploadError, UploadService, allowed_url, read_allowed_media

SELECTOR = 'input[type="file"][data-xiaoruo1-upload-id="test_observed_uuid"]'
URL = "https://member.bilibili.com/platform/upload/video/frame"


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "videos"
    folder = root / "2026_10" / "27"
    folder.mkdir(parents=True)
    file = folder / "测试成片.mp4"
    file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"test-video" * 10)
    return root, file


def test_real_bytes_and_media_metadata(media):
    root, file = media
    result = read_allowed_media(str(file), root)
    assert result["name"] == file.name
    assert result["size"] == file.stat().st_size
    assert base64.b64decode(result["data_base64"]) == file.read_bytes()
    assert result["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()


@pytest.mark.parametrize("relative", ["secret.token", "credentials.json", "env.txt", "script.py", "fake.mp4.exe", "cover.svg"])
def test_nonmedia_extensions_are_denied(media, relative):
    root, file = media
    denied = file.with_name(relative)
    denied.write_bytes(file.read_bytes())
    with pytest.raises(UploadError, match="Only MP4"):
        read_allowed_media(str(denied), root)


def test_wrong_content_and_empty_and_large(media):
    root, file = media
    file.write_bytes(b"not a video")
    with pytest.raises(UploadError) as error:
        read_allowed_media(str(file), root)
    assert error.value.code == "upload_content_mismatch"
    file.write_bytes(b"")
    with pytest.raises(UploadError) as error:
        read_allowed_media(str(file), root)
    assert error.value.code == "upload_size_denied"
    file.write_bytes(b"\0\0\0\x18ftyp" + b"x" * 20)
    with pytest.raises(UploadError) as error:
        read_allowed_media(str(file), root, max_bytes=10)
    assert error.value.code == "upload_size_denied"


def test_traversal_relative_ads_and_outside(media, tmp_path):
    root, file = media
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(file.read_bytes())
    for denied in ["relative.mp4", str(file.parent / ".." / "27" / file.name), str(file) + ":secret", str(outside)]:
        with pytest.raises(UploadError):
            read_allowed_media(denied, root)


def test_invalid_date_and_undated_directory(media):
    root, file = media
    for folder in [root / "2026_02" / "30", root / "undated"]:
        folder.mkdir(parents=True)
        bad = folder / "video.mp4"
        bad.write_bytes(file.read_bytes())
        with pytest.raises(UploadError) as error:
            read_allowed_media(str(bad), root)
        assert error.value.code == "upload_path_denied"


def test_missing_file_and_directory(media):
    root, file = media
    with pytest.raises(UploadError) as error:
        read_allowed_media(str(file.with_name("missing.mp4")), root)
    assert error.value.code == "upload_file_missing"
    with pytest.raises(UploadError):
        read_allowed_media(str(file.parent), root)


@pytest.mark.parametrize("url", ["https://member.bilibili.com/platform/upload/video/frame?foo=bar",
    "http://127.0.0.1:48272/upload-test/fixture.html"])
def test_exact_upload_destinations(url):
    assert allowed_url(url)


@pytest.mark.parametrize("url", ["https://evil.test/platform/upload/video/frame", "http://member.bilibili.com/platform/upload/video/frame",
    "https://member.bilibili.com.evil.test/platform/upload/video/frame", "https://member.bilibili.com/platform/home",
    "https://user:pass@localhost/platform/upload/video/frame", "http://127.0.0.1:48273/upload-test/fixture.html",
    "http://127.0.0.1:48272/private/token", "http://localhost:48272/upload-test/fixture.html",
    "http://127.0.0.1:48272/upload-test/fixture.html?secret=1"])
def test_other_upload_destinations_denied(url):
    assert not allowed_url(url)


@pytest.fixture
def service(media, tmp_path):
    root, file = media
    return UploadService(tmp_path / "data" / "receipts.sqlite3", root)


async def tabs():
    return {"tabs": [{"tab_id": "42", "url": URL, "browser_family": "edge"}]}


def kwargs(media, **changes):
    result = dict(tab_id="42", selector=SELECTOR, file_path=str(media[1]), confirm=True,
                  timeout=1000, idempotency_key="test-upload-0001", list_tabs=tabs)
    result.update(changes)
    return result


async def success_dispatch(method, payload, timeout):
    assert method == "upload_file"
    assert payload["confirm"] is True
    assert payload["expected_url"] == URL
    return {"file_selected": True, "selected_file": {"name": payload["name"], "size": payload["size"], "sha256": payload["sha256"], "type": payload["mime_type"]}}


@pytest.mark.asyncio
async def test_selection_is_not_platform_completion_and_duplicate_not_redispatched(service, media):
    called = []
    async def dispatch(*args, **kw):
        called.append(True)
        return await success_dispatch(*args, **kw)
    first = await service.upload(**kwargs(media), dispatch=dispatch)
    assert first["ok"] is True
    assert first["stages"] == {"file_selected": True, "upload_started": None, "server_received": None, "platform_processed": None}
    second = await service.upload(**kwargs(media), dispatch=dispatch)
    assert second["duplicate_call"] is True and second["redispatched"] is False
    assert len(called) == 1
    assert "data_base64" not in json.dumps(service.status("test-upload-0001"))


@pytest.mark.asyncio
async def test_duplicate_receipt_does_not_require_desktop_ownership_again(service, media):
    await service.upload(**kwargs(media), dispatch=success_dispatch)
    async def user_control():
        raise AssertionError("A read-only duplicate must not check new input ownership")
    result = await service.upload(**kwargs(media), dispatch=success_dispatch, desktop_check=user_control)
    assert result["duplicate_call"] is True and result["status"] == "selected"


@pytest.mark.asyncio
async def test_preflight_timeout_does_not_dispatch_or_reserve_an_uncertain_operation(service, media):
    async def slow_tabs():
        await asyncio.sleep(2)
    async def forbidden(*args, **kw):
        raise AssertionError("Preflight timeout must not dispatch")
    result = await service.upload(**kwargs(media, list_tabs=slow_tabs), dispatch=forbidden)
    assert result["error"]["code"] == "upload_timeout_before_dispatch"
    assert result["stages"]["file_selected"] is False
    assert service.status("test-upload-0001")["status"] == "not_found"


@pytest.mark.asyncio
async def test_external_database_lock_is_bounded_and_other_async_work_remains_responsive(service, media):
    holder = sqlite3.connect(str(service.database), timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    ticks = []
    stop = asyncio.Event()
    async def health_work():
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(.005)
    async def forbidden(*args, **kw):
        raise AssertionError("Busy database must prevent dispatch")
    ticker = asyncio.create_task(health_work())
    began = time.monotonic()
    try:
        result = await service.upload(**kwargs(media), dispatch=forbidden)
        elapsed = time.monotonic() - began
        await asyncio.sleep(.01)
        assert result["error"]["code"] == "upload_receipt_database_busy"
        assert result["retryable"] is True
        assert result["redispatched"] is False
        assert result["stages"]["file_selected"] is False
        assert elapsed < 1
        assert len(ticks) >= 2
        # Windows CI can incur scheduler delays while other regressions run;
        # this still rules out the previous multi-second event-loop stall.
        assert max(b-a for a,b in zip(ticks,ticks[1:])) < .5
        status = service.status("test-upload-0001")
        assert status["status"] == "receipt_unavailable" and status["retryable"] is True
    finally:
        holder.rollback()
        holder.close()
        stop.set()
        await ticker
    assert service.status("test-upload-0001")["status"] == "not_found"


@pytest.mark.asyncio
async def test_database_lock_after_dispatch_preserves_unknown_and_target_fence(service, media):
    holder = sqlite3.connect(str(service.database), timeout=0)
    async def selected_then_locked(*args, **kw):
        selected = await success_dispatch(*args, **kw)
        holder.execute("BEGIN EXCLUSIVE")
        return selected
    try:
        result = await service.upload(**kwargs(media), dispatch=selected_then_locked)
        assert result["status"] == "outcome_unknown"
        assert result["error"]["code"] == "upload_receipt_database_busy"
        assert result["stages"]["file_selected"] is None
    finally:
        holder.rollback()
        holder.close()
    assert service.status("test-upload-0001")["status"] == "outcome_unknown"
    again = await service.upload(**kwargs(media), dispatch=success_dispatch)
    assert again["duplicate_call"] is True and again["redispatched"] is False


@pytest.mark.asyncio
async def test_parallel_same_key_is_only_dispatched_once(service, media):
    calls = []
    async def dispatch(*args, **kw):
        calls.append(True)
        await asyncio.sleep(.02)
        return await success_dispatch(*args, **kw)
    one, two = await asyncio.gather(*(service.upload(**kwargs(media), dispatch=dispatch) for _ in range(2)))
    assert len(calls) == 1
    assert one["status"] == "selected" or two["status"] == "selected"


@pytest.mark.asyncio
async def test_duplicate_with_new_key_and_changed_key_content_are_guarded(service, media):
    await service.upload(**kwargs(media), dispatch=success_dispatch)
    duplicate = await service.upload(**kwargs(media, idempotency_key="test-upload-0002"), dispatch=success_dispatch)
    assert duplicate["error"]["code"] == "upload_duplicate_prevented"
    changed = await service.upload(**kwargs(media, selector=SELECTOR.replace("test_observed_uuid", "other_observed_uuid")), dispatch=success_dispatch)
    assert changed["error"]["code"] == "upload_idempotency_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,code", [({"confirm": False}, "upload_confirmation_required"),
    ({"confirm": 1}, "upload_confirmation_required"), ({"selector": 'input[name="buploader"]'}, "upload_selector_unobserved"),
    ({"selector": 'input[type="file"]'}, "upload_selector_unobserved"), ({"timeout": 0}, "upload_timeout_invalid"),
    ({"timeout": True}, "upload_timeout_invalid"), ({"idempotency_key": "a"}, "upload_key_invalid")])
async def test_rejected_before_any_dispatch(service, media, changes, code):
    async def forbidden(*args, **kw):
        raise AssertionError("Must not dispatch")
    result = await service.upload(**kwargs(media, **changes), dispatch=forbidden)
    assert result["error"]["code"] == code
    assert result["redispatched"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["chromium-other", "unknown", None])
async def test_edge_identity_required(service, media, family):
    async def wrong():
        return {"tabs": [{"tab_id": "42", "url": URL, "browser_family": family}]}
    result = await service.upload(**kwargs(media, list_tabs=wrong), dispatch=success_dispatch)
    assert result["error"]["code"] == "upload_requires_edge"


@pytest.mark.asyncio
async def test_origin_and_missing_or_ambiguous_tab(service, media):
    async def evil():
        return {"tabs": [{"tab_id": "42", "url": "https://evil.test", "browser_family": "edge"}]}
    assert (await service.upload(**kwargs(media, list_tabs=evil), dispatch=success_dispatch))["error"]["code"] == "upload_origin_denied"
    async def ambiguous():
        return {"tabs": [(await tabs())["tabs"][0]] * 2}
    assert (await service.upload(**kwargs(media, list_tabs=ambiguous), dispatch=success_dispatch))["error"]["code"] == "upload_tab_ambiguous"


@pytest.mark.asyncio
async def test_user_takeover_is_predispatch_and_safe_to_wait(service, media):
    async def desktop():
        raise UploadError("upload_desktop_control_unavailable", "User takeover is active")
    result = await service.upload(**kwargs(media), dispatch=success_dispatch, desktop_check=desktop)
    assert result["error"]["code"] == "upload_desktop_control_unavailable"
    assert result["stages"]["file_selected"] is False and result["retryable"] is True
    assert service.status("test-upload-0001")["status"] == "not_found"


@pytest.mark.asyncio
async def test_timeout_receipt_survives_restart_and_never_replays(service, media):
    calls = []
    async def timeout(*args, **kw):
        calls.append(True)
        raise TimeoutError()
    first = await service.upload(**kwargs(media), dispatch=timeout)
    assert first["status"] == "outcome_unknown"
    assert first["stages"]["file_selected"] is None
    restarted = UploadService(service.database, service.root)
    again = await restarted.upload(**kwargs(media), dispatch=timeout)
    assert again["duplicate_call"] is True and len(calls) == 1


@pytest.mark.asyncio
async def test_uncertain_target_cannot_be_overwritten_with_another_file_after_restart(service, media):
    async def timeout(*args, **kw):
        raise TimeoutError()
    await service.upload(**kwargs(media), dispatch=timeout)
    other = media[1].with_name("another.mp4")
    other.write_bytes(media[1].read_bytes() + b"different")
    restarted = UploadService(service.database, service.root)
    result = await restarted.upload(**kwargs(media, file_path=str(other), idempotency_key="other-file-new-key"), dispatch=success_dispatch)
    assert result["error"]["code"] == "upload_duplicate_prevented"
    assert result["redispatched"] is False


@pytest.mark.asyncio
async def test_cancelled_call_does_not_reupload(service, media):
    started = asyncio.Event()
    async def delayed(*args, **kw):
        started.set()
        await asyncio.sleep(60)
    task = asyncio.create_task(service.upload(**kwargs(media), dispatch=delayed))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.status("test-upload-0001")["error"]["code"] == "upload_cancelled_after_dispatch"
    assert (await service.upload(**kwargs(media), dispatch=success_dispatch))["duplicate_call"] is True


@pytest.mark.asyncio
async def test_mismatched_selection_is_unknown_not_success(service, media):
    async def wrong(*args, **kw):
        return {"file_selected": True, "selected_file": {"name": "other.mp4", "size": 1, "sha256": "bad"}}
    result = await service.upload(**kwargs(media), dispatch=wrong)
    assert result["status"] == "outcome_unknown"
    assert result["error"]["code"] == "upload_selection_mismatch"


@pytest.mark.asyncio
async def test_extension_known_refusal_can_be_diagnosed(service, media):
    async def refused(*args, **kw):
        return {"file_selected": False, "action_may_have_run": False, "error": {"code": "upload_target_disabled"}}
    result = await service.upload(**kwargs(media), dispatch=refused)
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "upload_target_disabled"


def test_upload_routes_require_auth_and_strict_confirmation():
    from fastapi.testclient import TestClient
    from web_tools import browser_bridge
    with TestClient(browser_bridge.app) as client:
        assert client.post("/v1/tabs/upload_status", json={"idempotency_key": "test-key-0001"}).status_code == 401
        response = client.post("/v1/tabs/upload_file", headers={"X-Xiaoruo1-Browser-Token": browser_bridge.BRIDGE_TOKEN},
            json={"tab_id": "1", "selector": SELECTOR, "file_path": "example.mp4", "confirm": "true", "idempotency_key": "test-key-0001"})
        assert response.status_code == 422


def test_fixture_does_not_mount_arbitrary_local_paths():
    from fastapi.testclient import TestClient
    from web_tools import browser_bridge
    with TestClient(browser_bridge.app) as client:
        assert client.get("/upload-test/config.js").status_code == 404
        assert client.get("/upload-test/browser_bridge.token").status_code == 404


def test_reparse_is_rejected_even_when_target_would_be_allowed(media, monkeypatch):
    import web_tools.browser_upload as module
    root, file = media
    monkeypatch.setattr(module, "_is_reparse", lambda path: path == file)
    with pytest.raises(UploadError) as error:
        read_allowed_media(str(file), root)
    assert error.value.code == "upload_path_denied"


def test_multiple_extension_sockets_route_explicit_edge_tab_without_evicting_it(monkeypatch):
    from fastapi.testclient import TestClient
    from web_tools import browser_bridge
    isolated = browser_bridge.EdgeExtensionBridge()
    monkeypatch.setattr(browser_bridge, "bridge", isolated)
    token = browser_bridge.BRIDGE_TOKEN
    headers = {"origin": "chrome-extension://test", "X-Xiaoruo1-Browser-Token": token}
    with TestClient(browser_bridge.app) as client:
        with client.websocket_connect(f"/ws?token={token}", headers=headers) as edge:
            assert edge.receive_json()["type"] == "hello"
            edge.send_json({"type": "hello_ack", "browser_family": "edge"})
            with client.websocket_connect(f"/ws?token={token}", headers=headers) as chrome:
                assert chrome.receive_json()["type"] == "hello"
                chrome.send_json({"type": "hello_ack", "browser_family": "chromium-other"})
                with ThreadPoolExecutor(max_workers=1) as executor:
                    request = executor.submit(lambda: client.post("/v1/tabs", headers=headers))
                    command = edge.receive_json()
                    edge.send_json({"id": command["id"], "ok": True, "result": {"browser_family": "edge", "tabs": [{"tab_id": "42", "url": URL}]}})
                    command = chrome.receive_json()
                    chrome.send_json({"id": command["id"], "ok": True, "result": {"browser_family": "chromium-other", "tabs": [{"tab_id": "99", "url": "https://example.test"}]}})
                    response = request.result(timeout=5)
                    assert {t["browser_family"] for t in response.json()["tabs"]} == {"edge", "chromium-other"}
                    request = executor.submit(lambda: client.post("/v1/tabs/read", headers=headers, json={"tab_id": "42", "settle_ms": 0}))
                    command = edge.receive_json()
                    assert command["method"] == "read_page" and command["params"]["tab_id"] == "42"
                    edge.send_json({"id": command["id"], "ok": True, "result": {"tab_id": "42", "url": URL, "text": "Edge page observed"}})
                    assert request.result(timeout=5).json()["text"] == "Edge page observed"
            # Detaching Chrome leaves the Edge socket as the live default.
            with ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(lambda: client.post("/v1/tabs", headers=headers))
                command = edge.receive_json()
                edge.send_json({"id": command["id"], "ok": True, "result": {"browser_family": "edge", "tabs": []}})
                assert request.result(timeout=5).status_code == 200


@pytest.mark.asyncio
async def test_duplicate_tab_ids_are_not_routed_to_an_arbitrary_browser():
    from web_tools import browser_bridge
    bridge = browser_bridge.EdgeExtensionBridge()
    bridge._websocket = object()
    bridge._tab_connections["42"] = None
    with pytest.raises(browser_bridge.BrowserBridgeError, match="ambiguous"):
        await bridge.call("upload_file", {"tab_id": "42"})