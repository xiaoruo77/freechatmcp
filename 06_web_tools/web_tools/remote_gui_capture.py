"""Short-lived, single-use PNG/JPEG uploads for temporary remote GUI agents.

Internal routes use the local background bridge credential.  A remote agent
receives only its capture ticket, never that credential or the main MCP token.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import secrets
import threading
import time
import warnings
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect


MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_JPEG_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_DIMENSION = 8192
TICKET_TTL_SECONDS = 90


@dataclass
class _CaptureTicket:
    capture_id: str
    token: str | None = field(repr=False)
    expires_at: float
    status: str = "pending"
    capture: dict[str, Any] | None = None
    error_code: str | None = None


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
        raise HTTPException(401, "capture_authentication_required")
    return token


def _matches(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _require_local_bridge(request: Request, bridge_token: str) -> None:
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(403, "capture_loopback_required") from None
    mapped = getattr(address, "ipv4_mapped", None)
    if not address.is_loopback and not (mapped and mapped.is_loopback):
        raise HTTPException(403, "capture_loopback_required")
    if not _matches(_bearer(request), bridge_token):
        raise HTTPException(401, "capture_authentication_required")


def _validate_png(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(415, "capture_png_required")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as picture:
                if picture.format != "PNG":
                    raise HTTPException(415, "capture_png_required")
                width, height = picture.size
                if (not 0 < width <= MAX_IMAGE_DIMENSION
                        or not 0 < height <= MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS):
                    raise HTTPException(413, "capture_dimensions_exceeded")
                picture.verify()
                return width, height
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(413, "capture_dimensions_exceeded") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise HTTPException(422, "capture_png_invalid") from None


def _validate_jpeg(payload: bytes) -> tuple[int, int]:
    if len(payload) > min(MAX_UPLOAD_BYTES, MAX_JPEG_UPLOAD_BYTES):
        raise HTTPException(413, "capture_upload_too_large")
    if not payload.startswith(b"\xff\xd8\xff"):
        raise HTTPException(415, "capture_jpeg_required")
    if not payload.endswith(b"\xff\xd9"):
        raise HTTPException(422, "capture_jpeg_invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as picture:
                if picture.format != "JPEG":
                    raise HTTPException(415, "capture_jpeg_required")
                width, height = picture.size
                if (not 0 < width <= MAX_IMAGE_DIMENSION
                        or not 0 < height <= MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS):
                    raise HTTPException(413, "capture_dimensions_exceeded")
                # A screenshot's pixel coordinates must match its displayed
                # dimensions. Never transpose/rotate from EXIF metadata.
                if picture.getexif().get(0x0112, 1) != 1:
                    raise HTTPException(415, "capture_jpeg_orientation_unsupported")
                picture.verify()
            # JPEG verify alone does not decode every scan. Load separately to
            # reject incomplete/corrupt pixel data before persisting any bytes.
            with Image.open(BytesIO(payload)) as picture:
                if picture.format != "JPEG" or picture.size != (width, height):
                    raise HTTPException(422, "capture_jpeg_invalid")
                picture.load()
            return width, height
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(413, "capture_dimensions_exceeded") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise HTTPException(422, "capture_jpeg_invalid") from None


class CaptureTicketManager:
    """In-memory credentials with fixed artifact paths and atomic claiming."""

    def __init__(self, data_root: Path | str, public_origin: str, *,
                 ttl_seconds: int = TICKET_TTL_SECONDS,
                 clock: Callable[[], float] | None = None):
        origin = urlsplit(public_origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in ("", "/") or origin.query or origin.fragment):
            raise ValueError("public_origin must be an HTTPS origin")
        if not 60 <= ttl_seconds <= 120:
            raise ValueError("capture ticket TTL must be between 60 and 120 seconds")
        self.capture_root = Path(data_root).resolve() / "background_desktop" / "captures"
        self.public_origin = public_origin.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._tickets: dict[str, _CaptureTicket] = {}

    def _purge_locked(self) -> None:
        now = self._clock()
        expired = [key for key, ticket in self._tickets.items() if ticket.expires_at <= now]
        for key in expired:
            ticket = self._tickets.pop(key)
            ticket.token = None
            ticket.capture = None

    def purge_expired(self) -> None:
        with self._lock:
            self._purge_locked()

    def clear(self) -> None:
        with self._lock:
            for ticket in self._tickets.values():
                ticket.token = None
                ticket.capture = None
            self._tickets.clear()

    def close(self) -> None:
        """Idempotently revoke remaining tickets without deleting artifacts."""
        self.clear()

    def issue(self) -> dict[str, Any]:
        with self._lock:
            self._purge_locked()
            capture_id = secrets.token_hex(16)
            while capture_id in self._tickets:
                capture_id = secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            self._tickets[capture_id] = _CaptureTicket(
                capture_id, token, self._clock() + self.ttl_seconds)
            return {"capture_id": capture_id,
                    "upload_url": f"{self.public_origin}/remote-gui/captures/{capture_id}",
                    "token": token, "expires_in": self.ttl_seconds}

    def _ticket_locked(self, capture_id: str) -> _CaptureTicket:
        self._purge_locked()
        ticket = self._tickets.get(capture_id)
        if ticket is None:
            raise HTTPException(404, "capture_ticket_not_found")
        return ticket

    def inspect(self, capture_id: str) -> dict[str, Any]:
        with self._lock:
            ticket = self._ticket_locked(capture_id)
            result: dict[str, Any] = {"capture_id": capture_id, "status": ticket.status}
            if ticket.capture is not None:
                result["capture"] = dict(ticket.capture)
            if ticket.error_code is not None:
                result["error_code"] = ticket.error_code
            return result

    def delete(self, capture_id: str) -> None:
        with self._lock:
            ticket = self._ticket_locked(capture_id)
            ticket.token = None
            ticket.capture = None
            del self._tickets[capture_id]

    def claim(self, capture_id: str, token: str) -> None:
        with self._lock:
            ticket = self._ticket_locked(capture_id)
            if ticket.status != "pending" or ticket.token is None or not _matches(token, ticket.token):
                raise HTTPException(401, "capture_authentication_required")
            # An authenticated attempt consumes the credential before reading.
            ticket.token = None
            ticket.status = "uploading"

    def fail(self, capture_id: str, error_code: str) -> None:
        with self._lock:
            self._purge_locked()
            ticket = self._tickets.get(capture_id)
            if ticket is not None and ticket.status == "uploading":
                ticket.status = "failed"
                ticket.error_code = error_code

    def store(self, capture_id: str, payload: bytes, dimensions: tuple[int, int],
              mime_type: str = "image/png") -> dict[str, Any]:
        with self._lock:
            ticket = self._ticket_locked(capture_id)
            if ticket.status != "uploading":
                raise HTTPException(409, "capture_upload_not_active")
            if mime_type not in ("image/png", "image/jpeg"):
                raise HTTPException(415, "capture_image_type_unsupported")
            if mime_type == "image/jpeg" and len(payload) > min(MAX_UPLOAD_BYTES, MAX_JPEG_UPLOAD_BYTES):
                raise HTTPException(413, "capture_upload_too_large")
            self.capture_root.mkdir(parents=True, exist_ok=True)
            extension = "jpg" if mime_type == "image/jpeg" else "png"
            path = self.capture_root / f"gui-{capture_id}.{extension}"
            created = False
            try:
                with path.open("xb") as output:
                    created = True
                    output.write(payload)
            except BaseException:
                if created:
                    with suppress(OSError):
                        path.unlink()
                raise
            capture = {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(),
                       "size": len(payload), "width": dimensions[0], "height": dimensions[1],
                       "mime_type": mime_type}
            ticket.status = "completed"
            ticket.capture = capture
            return dict(capture)


def create_capture_router(data_root: Path | str, bridge_token: str, public_origin: str) -> APIRouter:
    """Create self-authenticating routes; the outer MCP auth must exempt these paths."""
    if not isinstance(bridge_token, str) or not bridge_token:
        raise ValueError("a background bridge token is required")
    manager = CaptureTicketManager(data_root, public_origin)

    @asynccontextmanager
    async def lifespan(_app):
        async def expire_tickets():
            while True:
                await asyncio.sleep(1)
                manager.purge_expired()

        sweeper = asyncio.create_task(expire_tickets())
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            manager.close()

    router = APIRouter(lifespan=lifespan)
    # The server may inspect/clear the manager without handling remote tokens.
    router.capture_manager = manager

    @router.post("/internal/remote-gui/captures")
    async def create_ticket(request: Request):
        _require_local_bridge(request, bridge_token)
        return JSONResponse(manager.issue(), headers={"Cache-Control": "no-store"})

    @router.get("/internal/remote-gui/captures/{capture_id}")
    async def inspect_ticket(capture_id: str, request: Request):
        _require_local_bridge(request, bridge_token)
        return JSONResponse(manager.inspect(capture_id), headers={"Cache-Control": "no-store"})

    @router.delete("/internal/remote-gui/captures/{capture_id}")
    async def delete_ticket(capture_id: str, request: Request):
        _require_local_bridge(request, bridge_token)
        manager.delete(capture_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.post("/remote-gui/captures/{capture_id}")
    async def upload_capture(capture_id: str, request: Request):
        manager.claim(capture_id, _bearer(request))
        try:
            mime_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            jpeg = mime_type == "image/jpeg"
            upload_limit = min(MAX_UPLOAD_BYTES, MAX_JPEG_UPLOAD_BYTES) if jpeg else MAX_UPLOAD_BYTES
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    raise HTTPException(400, "capture_content_length_invalid") from None
                if declared_size < 0:
                    raise HTTPException(400, "capture_content_length_invalid")
                if declared_size > upload_limit:
                    raise HTTPException(413, "capture_upload_too_large")
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                raise HTTPException(415, "capture_encoding_unsupported")
            payload = bytearray()
            async for chunk in request.stream():
                if len(payload) + len(chunk) > upload_limit:
                    raise HTTPException(413, "capture_upload_too_large")
                payload.extend(chunk)
            data = bytes(payload)
            dimensions = await run_in_threadpool(_validate_jpeg if jpeg else _validate_png, data)
            if jpeg:
                capture = await run_in_threadpool(manager.store, capture_id, data, dimensions, "image/jpeg")
            else:
                capture = await run_in_threadpool(manager.store, capture_id, data, dimensions)
            acknowledgement = {"capture_id": capture_id, "sha256": capture["sha256"]}
            if jpeg:
                acknowledgement["mime_type"] = "image/jpeg"
            return JSONResponse(acknowledgement,
                                headers={"Cache-Control": "no-store"})
        except HTTPException as error:
            manager.fail(capture_id, str(error.detail))
            raise
        except ClientDisconnect:
            manager.fail(capture_id, "capture_upload_disconnected")
            raise HTTPException(400, "capture_upload_disconnected") from None
        except asyncio.CancelledError:
            manager.fail(capture_id, "capture_upload_cancelled")
            raise
        except Exception:
            manager.fail(capture_id, "capture_upload_failed")
            raise HTTPException(500, "capture_upload_failed") from None

    return router
