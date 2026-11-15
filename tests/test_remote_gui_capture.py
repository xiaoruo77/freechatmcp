from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from web_tools import remote_gui_capture as captures


BRIDGE_TOKEN = "test-background-bridge-secret"
MCP_TOKEN = "test-main-mcp-secret"
ORIGIN = "https://capture.example"


def png_bytes(width=3, height=2):
    output = BytesIO()
    Image.new("RGB", (width, height), "blue").save(output, "PNG")
    return output.getvalue()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def endpoint(tmp_path):
    router = captures.create_capture_router(tmp_path, BRIDGE_TOKEN, ORIGIN)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        yield local, app, router.capture_manager, tmp_path


def issue(client):
    response = client.post("/internal/remote-gui/captures", headers=bearer(BRIDGE_TOKEN))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def upload_path(ticket):
    return "/remote-gui/captures/" + ticket["capture_id"]


def status_path(ticket):
    return "/internal/remote-gui/captures/" + ticket["capture_id"]


def test_public_upload_uses_only_ticket_and_fixed_artifact_path(endpoint, caplog):
    local, app, manager, root = endpoint
    ticket = issue(local)
    assert len(base64.urlsafe_b64decode(ticket["token"] + "=")) == 32
    assert ticket["upload_url"] == ORIGIN + upload_path(ticket)
    assert ticket["expires_in"] == 90
    assert BRIDGE_TOKEN not in json.dumps(ticket)
    before = local.get(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).json()
    assert before == {"capture_id": ticket["capture_id"], "status": "pending"}
    remote = TestClient(app, client=("203.0.113.5", 51000))
    payload = png_bytes()
    response = remote.post(upload_path(ticket), headers=bearer(ticket["token"]), content=payload)
    assert response.status_code == 200
    assert response.json() == {"capture_id": ticket["capture_id"],
                               "sha256": hashlib.sha256(payload).hexdigest()}
    metadata = local.get(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).json()
    assert metadata["status"] == "completed"
    capture = metadata["capture"]
    expected = root / "background_desktop" / "captures" / f"gui-{ticket['capture_id']}.png"
    assert Path(capture["path"]) == expected
    assert expected.read_bytes() == payload
    assert capture == {"path": str(expected), "width": 3, "height": 2,
                       "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                       "mime_type": "image/png"}
    assert ticket["token"] not in json.dumps(metadata)
    assert ticket["token"] not in repr(manager._tickets)
    assert ticket["token"] not in caplog.text
    assert BRIDGE_TOKEN not in caplog.text


@pytest.mark.parametrize("method,path", [
    ("POST", "/internal/remote-gui/captures"),
    ("GET", "/internal/remote-gui/captures/unknown"),
    ("DELETE", "/internal/remote-gui/captures/unknown"),
])
def test_all_internal_routes_require_actual_loopback_even_with_forwarded_headers(endpoint, method, path):
    _, app, _, _ = endpoint
    remote = TestClient(app, client=("198.51.100.42", 51000))
    headers = {**bearer(BRIDGE_TOKEN), "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    assert remote.request(method, path, headers=headers).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("POST", "/internal/remote-gui/captures"),
    ("GET", "/internal/remote-gui/captures/unknown"),
    ("DELETE", "/internal/remote-gui/captures/unknown"),
])
@pytest.mark.parametrize("token", [None, "wrong", MCP_TOKEN])
def test_all_internal_routes_require_bridge_credential(endpoint, method, path, token):
    local, _, _, _ = endpoint
    headers = {} if token is None else bearer(token)
    assert local.request(method, path, headers=headers).status_code == 401


@pytest.mark.parametrize("token", [None, BRIDGE_TOKEN, MCP_TOKEN, "wrong"])
def test_public_upload_rejects_other_credentials_without_consuming_ticket(endpoint, token):
    local, _, _, _ = endpoint
    ticket = issue(local)
    headers = {} if token is None else bearer(token)
    assert local.post(upload_path(ticket), headers=headers, content=png_bytes()).status_code == 401
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 200


def test_ticket_cannot_upload_another_capture_or_replay_success(endpoint):
    local, _, _, root = endpoint
    first, second = issue(local), issue(local)
    assert local.post(upload_path(second), headers=bearer(first["token"]), content=png_bytes()).status_code == 401
    assert local.post(upload_path(first), headers=bearer(first["token"]), content=png_bytes()).status_code == 200
    assert local.post(upload_path(first), headers=bearer(first["token"]), content=png_bytes()).status_code == 401
    assert len(list(root.rglob("*.png"))) == 1


def test_atomic_claim_allows_only_one_concurrent_request(endpoint):
    local, _, manager, _ = endpoint
    ticket = issue(local)

    def claim(_):
        try:
            manager.claim(ticket["capture_id"], ticket["token"])
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(20)))
    assert results.count(200) == 1
    assert results.count(401) == 19
    assert manager.inspect(ticket["capture_id"])["status"] == "uploading"


@pytest.mark.parametrize("payload,code", [(b"not PNG", 415), (b"\x89PNG\r\n\x1a\ninvalid", 422),
                                         (png_bytes()[:-10], 422)])
def test_invalid_png_is_rejected_and_consumes_authenticated_attempt(endpoint, payload, code):
    local, _, manager, root = endpoint
    ticket = issue(local)
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=payload).status_code == code
    assert manager.inspect(ticket["capture_id"])["status"] == "failed"
    assert not list(root.rglob("*.png"))
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 401


@pytest.mark.parametrize("dimensions", [(8193, 1), (1, 8193), (8192, 4096), (100000, 100000)])
def test_png_dimensions_and_pixel_budget_are_checked_before_decode(endpoint, dimensions):
    local, _, _, root = endpoint
    ticket = issue(local)
    payload = bytearray(png_bytes())
    payload[16:24] = struct.pack(">II", *dimensions)
    payload[29:33] = struct.pack(">I", zlib.crc32(payload[12:29]) & 0xffffffff)
    response = local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=bytes(payload))
    assert response.status_code == 413
    assert not list(root.rglob("*.png"))


def test_streaming_limit_cannot_be_bypassed_by_underreported_content_length(endpoint, monkeypatch):
    local, _, _, root = endpoint
    monkeypatch.setattr(captures, "MAX_UPLOAD_BYTES", 64)
    ticket = issue(local)
    response = local.post(upload_path(ticket), headers={**bearer(ticket["token"]), "Content-Length": "1"},
                          content=png_bytes())
    assert response.status_code == 413
    assert response.json()["detail"] == "capture_upload_too_large"
    assert not list(root.rglob("*.png"))


@pytest.mark.parametrize("content_length", ["-1", "nonnumeric", str(16 * 1024 * 1024 + 1)])
def test_invalid_or_oversized_declared_body_is_rejected(endpoint, content_length):
    local, _, _, _ = endpoint
    ticket = issue(local)
    response = local.post(upload_path(ticket), content=b"", headers={**bearer(ticket["token"]),
                                                                    "Content-Length": content_length})
    assert response.status_code == (413 if content_length.isdecimal() else 400)


def test_compressed_request_is_rejected(endpoint):
    local, _, _, _ = endpoint
    ticket = issue(local)
    response = local.post(upload_path(ticket), content=png_bytes(),
                          headers={**bearer(ticket["token"]), "Content-Encoding": "gzip"})
    assert response.status_code == 415


def test_expiry_removes_tokens_and_metadata(endpoint):
    local, _, manager, root = endpoint
    ticket = issue(local)
    now = manager._clock()
    manager._clock = lambda: now + 121
    assert local.get(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).status_code == 404
    assert manager._tickets == {}
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 404
    assert not list(root.rglob("*.png"))


def test_router_sweeper_removes_expired_ticket_without_further_request(endpoint):
    local, _, manager, _ = endpoint
    ticket = issue(local)
    now = manager._clock()
    manager._clock = lambda: now + 121
    deadline = time.monotonic() + 3
    while manager._tickets and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ticket["capture_id"] not in manager._tickets


def test_upload_must_finish_before_ticket_expiry(endpoint, monkeypatch):
    local, _, manager, root = endpoint
    ticket = issue(local)
    now = manager._clock()
    original = captures._validate_png

    def expire_during_validation(payload):
        dimensions = original(payload)
        manager._clock = lambda: now + 121
        return dimensions

    monkeypatch.setattr(captures, "_validate_png", expire_during_validation)
    response = local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes())
    assert response.status_code == 404
    assert not list(root.rglob("*.png"))


def test_delete_revokes_ticket_but_does_not_delete_saved_artifact(endpoint):
    local, _, _, root = endpoint
    ticket = issue(local)
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 200
    assert local.delete(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).status_code == 204
    assert local.get(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).status_code == 404
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 404
    assert len(list(root.rglob("*.png"))) == 1


def test_no_client_file_path_can_be_selected(endpoint):
    local, _, _, root = endpoint
    ticket = issue(local)
    response = local.post("/remote-gui/captures/not-a-capture.png", headers=bearer(ticket["token"]),
                          content=png_bytes())
    assert response.status_code == 404
    assert not list(root.rglob("*.png"))
    assert local.get(upload_path(ticket), headers=bearer(ticket["token"])).status_code == 405


def test_artifact_storage_error_is_safe_and_ticket_remains_consumed(endpoint):
    local, _, manager, root = endpoint
    ticket = issue(local)
    root.joinpath("background_desktop").write_text("blocks directory")
    response = local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes())
    assert response.status_code == 500
    assert response.json() == {"detail": "capture_upload_failed"}
    assert manager.inspect(ticket["capture_id"])["error_code"] == "capture_upload_failed"
    assert local.post(upload_path(ticket), headers=bearer(ticket["token"]), content=png_bytes()).status_code == 401


def test_shutdown_discards_credentials(tmp_path):
    router = captures.create_capture_router(tmp_path, BRIDGE_TOKEN, ORIGIN)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, client=("::1", 50000)) as client:
        issue(client)
        assert router.capture_manager._tickets
    assert router.capture_manager._tickets == {}


@pytest.mark.parametrize("origin", ["http://capture.example", "https://capture.example/path",
                                   "https://user:secret@capture.example", "https://capture.example?token=x",
                                   "https://capture.example/#x"])
def test_factory_rejects_non_origin_upload_destinations(tmp_path, origin):
    with pytest.raises(ValueError):
        captures.create_capture_router(tmp_path, BRIDGE_TOKEN, origin)


def test_required_credential_and_fixed_limits(tmp_path):
    with pytest.raises(ValueError):
        captures.create_capture_router(tmp_path, "", ORIGIN)
    with pytest.raises(ValueError):
        captures.CaptureTicketManager(tmp_path, ORIGIN, ttl_seconds=3600)
    assert captures.MAX_UPLOAD_BYTES == 16 * 1024 * 1024
    assert captures.MAX_IMAGE_PIXELS == 32_000_000
    assert captures.MAX_IMAGE_DIMENSION == 8192


def jpeg_bytes(width=3, height=2, *, orientation=None):
    output = BytesIO()
    options = {"quality": 92}
    if orientation is not None:
        metadata = Image.Exif()
        metadata[0x0112] = orientation
        options["exif"] = metadata
    Image.new("RGB", (width, height), "blue").save(output, "JPEG", **options)
    return output.getvalue()


def jpeg_headers(ticket, mime_type="image/jpeg"):
    return {**bearer(ticket["token"]), "Content-Type": mime_type}


def test_jpeg_upload_preserves_exact_bytes_dimensions_mime_and_fixed_jpg_path(endpoint, caplog):
    local, app, manager, root = endpoint
    ticket = issue(local)
    data = jpeg_bytes(13, 7)
    remote = TestClient(app, client=("203.0.113.5", 51000))
    response = remote.post(upload_path(ticket), headers=jpeg_headers(ticket), content=data)
    assert response.status_code == 200
    assert response.json() == {"capture_id": ticket["capture_id"], "sha256": hashlib.sha256(data).hexdigest(), "mime_type": "image/jpeg"}
    assert response.headers["cache-control"] == "no-store"
    metadata = manager.inspect(ticket["capture_id"])
    path = root / "background_desktop" / "captures" / f"gui-{ticket['capture_id']}.jpg"
    assert metadata["status"] == "completed"
    assert metadata["capture"] == {"path": str(path), "width": 13, "height": 7,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime_type": "image/jpeg"}
    assert path.read_bytes() == data and not path.with_suffix(".png").exists()
    assert ticket["token"] not in json.dumps(metadata) and ticket["token"] not in caplog.text
    assert remote.post(upload_path(ticket), headers=jpeg_headers(ticket), content=data).status_code == 401


@pytest.mark.parametrize("content_type", [None, "image/png", "image/jpg", "image/gif", "application/octet-stream"])
def test_jpeg_requires_explicit_supported_http_mime_type(endpoint, content_type):
    local, _, manager, root = endpoint
    ticket = issue(local)
    headers = bearer(ticket["token"]) if content_type is None else jpeg_headers(ticket, content_type)
    response = local.post(upload_path(ticket), headers=headers, content=jpeg_bytes())
    assert response.status_code == 415 and response.json()["detail"] == "capture_png_required"
    assert manager.inspect(ticket["capture_id"])["status"] == "failed"
    assert not list(root.rglob("*.jpg"))


@pytest.mark.parametrize("payload,status", [(png_bytes(), 415), (b"GIF89a" + b"x" * 20, 415),
    (b"not an image", 415), (b"\xff\xd8\xffinvalid\xff\xd9", 422),
    (jpeg_bytes()[:-20], 422), (jpeg_bytes() + b"extra", 422)])
def test_jpeg_mime_rejects_wrong_formats_corruption_and_truncation(endpoint, payload, status):
    local, _, manager, root = endpoint
    ticket = issue(local)
    response = local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=payload)
    assert response.status_code == status
    assert manager.inspect(ticket["capture_id"])["status"] == "failed"
    assert not list(root.rglob("*.jpg"))
    assert local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes()).status_code == 401


@pytest.mark.parametrize("dimensions", [(8193, 1), (1, 8193), (8192, 4096), (65535, 65535)])
def test_jpeg_dimensions_and_pixel_budget_are_checked_before_load(endpoint, dimensions):
    local, _, _, root = endpoint
    ticket = issue(local)
    data = bytearray(jpeg_bytes())
    sof = data.index(b"\xff\xc0")
    data[sof + 5:sof + 9] = struct.pack(">HH", dimensions[1], dimensions[0])
    response = local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=bytes(data))
    assert response.status_code == 413 and response.json()["detail"] == "capture_dimensions_exceeded"
    assert not list(root.rglob("*.jpg"))


@pytest.mark.parametrize("orientation", [2, 3, 6, 8])
def test_jpeg_exif_must_not_rotate_or_reinterpret_physical_coordinates(endpoint, orientation):
    local, _, _, root = endpoint
    ticket = issue(local)
    response = local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes(13, 7, orientation=orientation))
    assert response.status_code == 415 and response.json()["detail"] == "capture_jpeg_orientation_unsupported"
    assert not list(root.rglob("*.jpg"))


def test_jpeg_identity_exif_orientation_is_preserved_without_transform(endpoint):
    local, _, manager, _ = endpoint
    ticket = issue(local)
    data = jpeg_bytes(13, 7, orientation=1)
    assert local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=data).status_code == 200
    metadata = manager.inspect(ticket["capture_id"])["capture"]
    assert (metadata["width"], metadata["height"]) == (13, 7)
    assert Path(metadata["path"]).read_bytes() == data


def test_jpeg_twelve_mib_limit_and_streaming_limit_leave_png_quota_unchanged(endpoint, monkeypatch):
    local, _, _, root = endpoint
    assert captures.MAX_JPEG_UPLOAD_BYTES == 12 * 1024 * 1024
    assert captures.MAX_UPLOAD_BYTES == 16 * 1024 * 1024
    ticket = issue(local)
    response = local.post(upload_path(ticket), headers={**jpeg_headers(ticket),
        "Content-Length": str(captures.MAX_JPEG_UPLOAD_BYTES + 1)}, content=b"")
    assert response.status_code == 413
    monkeypatch.setattr(captures, "MAX_JPEG_UPLOAD_BYTES", 32)
    ticket = issue(local)
    response = local.post(upload_path(ticket), headers={**jpeg_headers(ticket), "Content-Length": "1"}, content=jpeg_bytes())
    assert response.status_code == 413 and not list(root.rglob("*.jpg"))


@pytest.mark.parametrize("token", [None, BRIDGE_TOKEN, MCP_TOKEN, "wrong"])
def test_jpeg_upload_requires_its_own_single_use_ticket(endpoint, token):
    local, _, manager, _ = endpoint
    ticket = issue(local)
    headers = {"Content-Type": "image/jpeg"}
    if token is not None:
        headers.update(bearer(token))
    assert local.post(upload_path(ticket), headers=headers, content=jpeg_bytes()).status_code == 401
    assert manager.inspect(ticket["capture_id"])["status"] == "pending"
    assert local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes()).status_code == 200


def test_jpeg_validation_loads_pixel_scan_in_addition_to_verify(endpoint, monkeypatch):
    local, _, _, root = endpoint
    ticket = issue(local)
    from PIL import JpegImagePlugin
    def corrupt_pixels(self, *args, **kwargs):
        raise OSError("PRIVATE_DECODER_DETAILS")
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", corrupt_pixels)
    response = local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes())
    assert response.status_code == 422 and response.json() == {"detail": "capture_jpeg_invalid"}
    assert "PRIVATE_DECODER_DETAILS" not in response.text and not list(root.rglob("*.jpg"))


def test_manager_only_allows_fixed_png_or_jpeg_extensions(tmp_path):
    manager = captures.CaptureTicketManager(tmp_path, ORIGIN)
    for mime_type in ("image/gif", "image/webp", "../../outside.jpg", None):
        ticket = manager.issue()
        manager.claim(ticket["capture_id"], ticket["token"])
        with pytest.raises(HTTPException) as caught:
            manager.store(ticket["capture_id"], jpeg_bytes(), (3, 2), mime_type)
        assert caught.value.status_code == 415
    assert not manager.capture_root.exists()


def test_jpeg_delete_revokes_ticket_but_preserves_artifact(endpoint):
    local, _, manager, root = endpoint
    ticket = issue(local)
    assert local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes()).status_code == 200
    path = Path(manager.inspect(ticket["capture_id"])["capture"]["path"])
    assert local.delete(status_path(ticket), headers=bearer(BRIDGE_TOKEN)).status_code == 204
    assert local.post(upload_path(ticket), headers=jpeg_headers(ticket), content=jpeg_bytes()).status_code == 404
    assert path.is_file() and path.parent == root / "background_desktop" / "captures"
