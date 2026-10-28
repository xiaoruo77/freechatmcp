"""Bounded, consented media delivery to an observed input in an existing Edge tab.

The durable receipt is deliberately a selection receipt. It does not claim that
the website received, processed, saved or published the media.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

MEDIA_ROOT = Path(os.getenv("WEB_AGENT_MEDIA_ROOT", str(Path.home() / "Videos" / "FreeChatMCP")))
MAX_BYTES = 32 * 1024 * 1024
# Synchronous SQLite calls are deliberately short: a second dialog/process
# holding a transaction must not stall the bridge event loop for seconds.
DATABASE_BUSY_SECONDS = 0.05
MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SELECTOR = re.compile(r'^input\[type="file"\]\[data-xiaoruo1-upload-id="[A-Za-z0-9_-]{8,100}"\]$')


class UploadError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _database_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", 0)
    return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(error).lower()


def allowed_url(value: str) -> bool:
    try:
        u = urlsplit(value)
        if u.username or u.password:
            return False
        if u.scheme == "https" and u.hostname == "mcp.example.com" and u.port in (None,443):
            return u.path == "/upload-test/fixture.html" and not u.query and not u.fragment
        if u.scheme == "https" and u.hostname == "member.bilibili.com" and u.port in (None, 443):
            return u.path.startswith("/platform/upload/")
        return (u.scheme == "http" and u.hostname == "127.0.0.1" and u.port == 48272
                and u.path == "/upload-test/fixture.html" and not u.query and not u.fragment)
    except (ValueError, TypeError):
        return False


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def read_allowed_media(value: str, root: Path = MEDIA_ROOT, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UploadError("upload_path_invalid", "An absolute local media file path is required")
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts or ":" in value[len(p.drive):]:
        raise UploadError("upload_path_invalid", "Relative paths, traversal and alternate data streams are forbidden")
    try:
        allowed = root.resolve(strict=True)
        lexical = p.relative_to(root)
        if len(lexical.parts) < 3 or not re.fullmatch(r"\d{4}_\d{2}", lexical.parts[0]) or not re.fullmatch(r"\d{2}", lexical.parts[1]):
            raise UploadError("upload_path_denied", "Media must be inside the dated video production directory")
        year, month = map(int, lexical.parts[0].split("_"))
        date(year, month, int(lexical.parts[1]))
        resolved = p.resolve(strict=True)
        resolved.relative_to(allowed)
        # Refuse links even when their current target is inside the allowlist.
        for entry in [p, *p.parents]:
            if entry == root.parent:
                break
            if _is_reparse(entry):
                raise UploadError("upload_path_denied", "Reparse points and symbolic links are forbidden")
        if not resolved.is_file():
            raise UploadError("upload_path_invalid", "Upload target must be a regular file")
        suffix = resolved.suffix.lower()
        if suffix not in MIME:
            raise UploadError("upload_extension_denied", "Only MP4, WebM, JPEG and PNG media are allowed")
        if any(ord(c) < 32 for c in resolved.name):
            raise UploadError("upload_path_invalid", "Control characters in file names are forbidden")
        before = resolved.stat()
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise UploadError("upload_size_denied", f"Media must contain 1 through {max_bytes} bytes")
        with resolved.open("rb") as source:
            opened = os.fstat(source.fileno())
            body = source.read(max_bytes + 1)
            after = os.fstat(source.fileno())
        final = resolved.stat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if signature(before) != signature(opened) or signature(opened) != signature(after) or signature(after) != signature(final) or len(body) != before.st_size:
            raise UploadError("upload_file_changed", "File changed during validation; observe it again before retrying")
        if _is_reparse(p) or p.resolve(strict=True) != resolved:
            raise UploadError("upload_file_changed", "File target changed during validation")
        valid_header = ((suffix == ".mp4" and body[4:8] == b"ftyp") or
                        (suffix == ".webm" and body.startswith(b"\x1aE\xdf\xa3")) or
                        (suffix in (".jpg", ".jpeg") and body.startswith(b"\xff\xd8\xff")) or
                        (suffix == ".png" and body.startswith(b"\x89PNG\r\n\x1a\n")))
        if not valid_header:
            raise UploadError("upload_content_mismatch", "Media header does not match its allowed extension")
        return {"path": str(resolved), "name": resolved.name, "size": len(body), "mime_type": MIME[suffix],
                "sha256": hashlib.sha256(body).hexdigest(), "data_base64": base64.b64encode(body).decode("ascii")}
    except UploadError:
        raise
    except FileNotFoundError as exc:
        raise UploadError("upload_file_missing", "Requested media file does not exist") from exc
    except (ValueError, OSError) as exc:
        raise UploadError("upload_path_denied", "Requested file is outside the allowed dated media directory or inaccessible") from exc


def receipt(key: str, state: str, *, code: str | None = None, message: str | None = None,
            selected: bool | None = False, **details: Any) -> dict[str, Any]:
    return {"ok": state == "selected", "status": state, "upload_id": key,
            "idempotency_key": key, "stages": {"file_selected": selected, "upload_started": None,
            "server_received": None, "platform_processed": None},
            "error": {"code": code, "message": message} if code else None,
            "retryable": False, "follow_up_required": True,
            "next_action": "Read the same webpage and inspect upload, server receipt and processing separately; never blindly re-upload.", **details}


class UploadService:
    def __init__(self, database: Path, root: Path = MEDIA_ROOT):
        self.database = database
        self.root = root
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS browser_upload_receipts (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def _connect(self):
        db = sqlite3.connect(str(self.database), timeout=DATABASE_BUSY_SECONDS)
        db.row_factory = sqlite3.Row
        return db

    def status(self, key: str) -> dict[str, Any]:
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return receipt(str(key)[:128], "rejected", code="upload_key_invalid", message="Use a stable 8-128 character idempotency key")
        try:
            with self._connect() as db:
                row = db.execute("SELECT result,state FROM browser_upload_receipts WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if _database_busy(exc):
                return receipt(key, "receipt_unavailable", selected=None, code="upload_receipt_database_busy", message="Receipt database is temporarily busy; retry this status read without selecting another file", retryable=True)
            raise
        if row is None:
            return receipt(key, "not_found", code="upload_not_found", message="No upload invocation was recorded")
        result = json.loads(row["result"])
        if row["state"] == "dispatching":
            result = receipt(key, "outcome_unknown", selected=None, code="upload_outcome_unknown", message="Invocation may still be running or was interrupted; inspect the original page before any new upload")
        return result

    def _save(self, key: str, result: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("UPDATE browser_upload_receipts SET state=?,result=?,updated_at=? WHERE key=?",
                       (result["status"], json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key))
        return result

    async def upload(self, *, tab_id: str, selector: str, file_path: str, confirm: bool,
                     timeout: int, idempotency_key: str, list_tabs: Callable[[], Awaitable[dict[str, Any]]],
                     dispatch: Callable[..., Awaitable[Any]], desktop_check: Callable[[], Awaitable[Any]] | None = None) -> dict[str, Any]:
        key = idempotency_key
        dispatched = False
        try:
            if confirm is not True:
                raise UploadError("upload_confirmation_required", "Explicit confirm=true is required for this local media upload")
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise UploadError("upload_key_invalid", "Use a stable 8-128 character idempotency key")
            if not isinstance(selector, str) or not SELECTOR.fullmatch(selector):
                raise UploadError("upload_selector_unobserved", "Use the precise observed file_inputs selector from browser_read_page")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1000 <= timeout <= 120000:
                raise UploadError("upload_timeout_invalid", "Timeout must be an integer from 1000 through 120000 milliseconds")
            deadline = asyncio.get_running_loop().time() + timeout / 1000
            def remaining():
                value = deadline - asyncio.get_running_loop().time()
                if value <= .05:
                    raise UploadError("upload_timeout_before_dispatch", "Preflight exhausted the upload timeout; no file was sent")
                return value
            try:
                media = await asyncio.wait_for(asyncio.to_thread(read_allowed_media, file_path, self.root), remaining())
                listed = await asyncio.wait_for(list_tabs(), remaining())
            except TimeoutError as exc:
                raise UploadError("upload_timeout_before_dispatch", "Media or browser preflight timed out before any file was sent") from exc
            candidates = [t for t in listed.get("tabs", []) if str(t.get("tab_id", t.get("id", ""))) == str(tab_id)]
            if len(candidates) != 1:
                raise UploadError("upload_tab_ambiguous", "Expected exactly one currently existing browser tab")
            tab = candidates[0]
            if tab.get("browser_family") != "edge":
                raise UploadError("upload_requires_edge", "The selected extension must identify Microsoft Edge; update or select the Edge bridge")
            expected_url = str(tab.get("url", ""))
            if not allowed_url(expected_url):
                raise UploadError("upload_origin_denied", "Upload is restricted to the Bilibili web creator upload page or the exact local test fixture")
            identity = {"tab_id": str(tab_id), "selector": selector, "url": expected_url,
                        "path": media["path"], "sha256": media["sha256"], "name": media["name"]}
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            # Fence the observed target, not just a particular file. A second
            # filename/key must not overwrite an uncertain first selection,
            # including after the extension process has restarted.
            resource = hashlib.sha256(json.dumps({k: identity[k] for k in ("tab_id", "selector", "url")}, sort_keys=True).encode()).hexdigest()
            now = datetime.now(timezone.utc).isoformat()
            # A duplicate is a read of its receipt, even while the user is
            # currently controlling the desktop. It must not require new input
            # ownership or reach a second dispatch.
            with self._connect() as db:
                old = db.execute("SELECT fingerprint FROM browser_upload_receipts WHERE key=?", (key,)).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise UploadError("upload_idempotency_conflict", "This key already belongs to different media, input or webpage")
                return {**self.status(key), "duplicate_call": True, "redispatched": False}
            if desktop_check:
                try:
                    await asyncio.wait_for(desktop_check(), remaining())
                except TimeoutError as exc:
                    raise UploadError("upload_timeout_before_dispatch", "Desktop preflight timed out before any file was sent") from exc
            # The isolated extension enforces the same deadline; leave enough
            # time for it to reject before assignment instead of extending it.
            if remaining() < .5:
                raise UploadError("upload_timeout_before_dispatch", "No selection time remains after preflight; no file was sent")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT fingerprint,result,state FROM browser_upload_receipts WHERE key=?", (key,)).fetchone()
                if old is not None:
                    if old["fingerprint"] != fingerprint:
                        raise UploadError("upload_idempotency_conflict", "This key already belongs to different media, input or webpage")
                    return {**self.status(key), "duplicate_call": True, "redispatched": False}
                prior = db.execute("SELECT key FROM browser_upload_receipts WHERE resource=? AND state IN ('dispatching','outcome_unknown','selected') LIMIT 1", (resource,)).fetchone()
                if prior is not None:
                    return receipt(key, "rejected", code="upload_duplicate_prevented", message="The same file/input/page already has an invocation; inspect its receipt and webpage", existing_upload_id=prior["key"], redispatched=False)
                pending = receipt(key, "dispatching", selected=None, code="upload_outcome_unknown", message="Invocation reserved; use its receipt rather than repeating it")
                db.execute("INSERT INTO browser_upload_receipts VALUES(?,?,?,?,?,?)", (key, fingerprint, resource, "dispatching", json.dumps(pending), now))
            payload = {k: media[k] for k in ("name", "size", "mime_type", "data_base64", "sha256")}
            payload.update(tab_id=str(tab_id), selector=selector, expected_url=expected_url,
                           timeout=max(1, int(remaining() * 1000)), idempotency_key=key, confirm=True)
            try:
                dispatched = True
                result = await dispatch("upload_file", payload, timeout=remaining())
            except asyncio.CancelledError:
                self._save(key, receipt(key, "outcome_unknown", selected=None, code="upload_cancelled_after_dispatch", message="Caller cancelled after dispatch; inspect the webpage, do not re-upload"))
                raise
            except Exception:
                return self._save(key, receipt(key, "outcome_unknown", selected=None, code="upload_transport_uncertain", message="Upload selection did not return a conclusive response; inspect the same page and receipt before any new action"))
            if not isinstance(result, dict):
                return self._save(key, receipt(key, "outcome_unknown", selected=None, code="upload_response_invalid", message="Extension did not return structured selection evidence"))
            selected = result.get("selected_file") or {}
            if not isinstance(selected, dict):
                return self._save(key, receipt(key, "outcome_unknown", selected=None, code="upload_response_invalid", message="Extension selection evidence was malformed"))
            if result.get("file_selected") is not True and result.get("stages", {}).get("file_selected") is not True:
                error = result.get("error") or {}
                code = error.get("code", "upload_selection_failed") if isinstance(error, dict) else "upload_selection_failed"
                state = "outcome_unknown" if result.get("action_may_have_run") is not False else "rejected"
                return self._save(key, receipt(key, state, selected=None if state == "outcome_unknown" else False, code=code, message="Extension did not confirm file selection; inspect the current input and webpage"))
            if (selected.get("name") != media["name"] or selected.get("size") != media["size"] or selected.get("sha256") != media["sha256"]):
                return self._save(key, receipt(key, "outcome_unknown", selected=None, code="upload_selection_mismatch", message="Selected file evidence did not match the validated local media"))
            selected = {k: selected[k] for k in ("name", "size", "sha256", "type") if k in selected}
            return self._save(key, receipt(key, "selected", selected=True, selected_file=selected,
                                           tab_id=str(tab_id), expected_url=expected_url, verification="file_name_size_and_sha256_matched", redispatched=False))
        except UploadError as exc:
            safe_wait = exc.code.startswith("upload_desktop") or exc.code in {"desktop_locked_or_secure", "desktop_session_inactive", "desktop_session_unavailable"}
            return receipt(str(key)[:128], "rejected", code=exc.code, message=str(exc), redispatched=False, retryable=safe_wait)
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError as exc:
            if _database_busy(exc):
                if dispatched:
                    return receipt(str(key)[:128], "outcome_unknown", selected=None, code="upload_receipt_database_busy", message="File selection may have run while its receipt database became busy; inspect the original page and retry status only", redispatched=False)
                return receipt(str(key)[:128], "rejected", code="upload_receipt_database_busy", message="Receipt database is temporarily busy; no file was dispatched", retryable=True, redispatched=False)
            if dispatched:
                return receipt(str(key)[:128], "outcome_unknown", selected=None, code="upload_receipt_persistence_failed", message="File selection may have run but receipt persistence failed; inspect the same webpage before any new action", redispatched=False)
            return receipt(str(key)[:128], "rejected", code="upload_preflight_failed", message="Local upload receipt preflight failed before file selection", redispatched=False)
        except Exception:
            if dispatched:
                return receipt(str(key)[:128], "outcome_unknown", selected=None, code="upload_receipt_persistence_failed", message="File selection may have run but receipt persistence failed; inspect the same webpage before any new action", redispatched=False)
            return receipt(str(key)[:128], "rejected", code="upload_preflight_failed", message="Browser or local media preflight failed before file selection", redispatched=False)