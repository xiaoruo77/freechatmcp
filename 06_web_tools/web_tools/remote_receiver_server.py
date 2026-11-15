"""Durable, outbound-only remote desktop receiver queue.

This module never starts a process, opens a desktop, or performs local input.
Only the enrolled receiver can consume its fixed-action commands. Credentials
are hashed at rest; capture upload credentials exist only in process memory.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .uu_remote_keyboard import RemoteKeyboardError, RemoteKeyboardSession


MAX_BODY_BYTES = 128 * 1024
MAX_DEVICES = 32
MAX_SESSIONS = 64
SESSION_IDLE_SECONDS = 180
COMMAND_RECEIPT_SECONDS = 90
POLL_SECONDS = 10
DEVICE_POLL_INTERVAL_SECONDS = 60
DEVICE_RECENTLY_SEEN_SECONDS = 120
_PATHS = frozenset({"/remote-receiver/enroll", "/remote-receiver/poll", "/remote-receiver/reply"})
_ACTIONS = frozenset({"status", "type", "key", "click", "scroll", "drag", "capture"})
_INPUT_ACTIONS = frozenset({"type", "key", "click", "scroll", "drag"})
_ID = re.compile(r"[a-f0-9]{32}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_COMPUTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
_RESULT_KEYS = frozenset({
    "runtime", "remote_runtime", "foreground", "remote_foreground", "computer",
    "session_id", "active_session_id", "window_station", "thread_desktop", "input_desktop",
    "helper_pid", "helper_creation_time_utc_ticks", "capture_id", "sha256", "width", "height",
    "bounds", "remote_bounds", "helper_stopped", "receiver_exiting", "stopping", "fixture_closed",
    "reason", "events", "event_count", "characters", "code_units", "remote_effect_verified",
    "delivery", "sent", "count", "keys_sent", "activated", "input_count",
})
_NESTED_KEYS = frozenset({
    "computer", "session_id", "active_session_id", "window_station", "thread_desktop", "input_desktop",
    "helper_pid", "helper_creation_time_utc_ticks", "hwnd", "pid", "creation_time_utc_ticks",
    "title", "class_name", "process_name", "left", "top", "width", "height", "available", "target_changed",
})
# Fixed diagnostics emitted by Receiver.cs or the bundled input helper. Never
# accept an arbitrary machine-looking identifier: it can itself contain a token.
_REMOTE_ERROR_CODES = frozenset({
    "receiver_failed", "network_unavailable", "server_rejected", "payload_too_large",
    "invalid_arguments", "invalid_identifier", "invalid_session", "invalid_credential",
    "invalid_json", "invalid_server_response", "invalid_endpoint_frame", "endpoint_checksum_mismatch",
    "session_idle_timeout", "session_command_limit", "session_already_started", "session_mismatch",
    "untrusted_origin", "package_integrity_failed", "helper_start_failed", "helper_unavailable",
    "helper_timeout", "helper_delivery_unknown", "helper_stop_unconfirmed", "helper_job_failed",
    "desktop_unavailable", "expected_computer_required", "remote_computer_mismatch",
    "interactive_session_required", "interactive_desktop_required", "target_window_stale",
    "activation_foreground_unverified", "target_identity_mismatch", "target_not_foreground",
    "input_queue_thread_unavailable", "input_queue_attach_failed", "input_queue_detach_failed",
    "transport_window_protected", "invalid_text", "invalid_keys", "secure_attention_unsupported",
    "remote_key_already_down", "input_delivery_incomplete", "invalid_bounds", "invalid_coordinates",
    "invalid_button", "invalid_click_count", "invalid_scroll", "invalid_drag_duration",
    "physical_pixels_unavailable", "dpi_context_restore_failed", "screen_bounds_changed",
    "remote_mouse_already_down", "mouse_target_session_mismatch", "invalid_capture_origin",
    "invalid_capture_ticket", "capture_origin_already_configured", "capture_origin_required",
    "capture_origin_mismatch", "invalid_capture_png", "capture_upload_failed",
    "capture_upload_http_401", "capture_upload_http_403", "capture_upload_http_404",
    "capture_upload_http_409", "capture_upload_http_413", "capture_upload_http_415",
    "capture_upload_http_429", "capture_upload_http_500", "capture_upload_http_502",
    "capture_upload_http_503", "capture_upload_http_504", "capture_upload_timeout",
    "capture_upload_tls_failed", "capture_upload_connection_failed", "capture_upload_dns_failed",
    "capture_upload_timeout_open", "capture_upload_timeout_write", "capture_upload_timeout_response",
    "capture_encoding_failed",
    "invalid_request",
    "invalid_request_frame", "request_checksum_mismatch", "nonce_mismatch", "unsupported_action",
    "request_id_conflict", "request_inflight", "receipt_cache_busy", "receipt_state_invalid",
    "nonce_required", "runtime_proof_missing", "remote_initialization_failed", "remote_input_failed",
})


class RemoteReceiverError(RuntimeError):
    """Bounded public diagnostic, with no request data or remote exception text."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _error(code: str, message: str, status: int = 400):
    raise RemoteReceiverError(code, message, status)


def _json(value: Any) -> str:
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded = result.encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _error("receiver_schema_invalid", "The receiver request has an invalid schema")
    if len(encoded) > MAX_BODY_BYTES:
        _error("receiver_body_too_large", "The receiver request exceeds its size limit", 413)
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        _error("receiver_authentication_required", "Receiver authentication is required", 401)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _error("receiver_identity_invalid", "A valid receiver identity is required")
    return value


def _computer(value: str) -> str:
    if not isinstance(value, str) or not _COMPUTER.fullmatch(value):
        _error("receiver_computer_invalid", "An exact receiver computer name is required")
    if value.casefold() == "xiaoruo":
        _error("receiver_controller_forbidden", "The controller computer cannot be a receiver", 403)
    return value.upper()


def _name(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or value != value.strip() or any(ord(char) < 32 for char in value)):
        _error("receiver_device_name_invalid", "A bounded device name is required")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _error("receiver_device_name_invalid", "A bounded device name is required")
    return value


def _operation(value: str) -> str:
    if not isinstance(value, str) or not _OPERATION.fullmatch(value):
        _error("receiver_operation_id_invalid", "A stable operation ID is required")
    return value


def _safe_result(value: dict) -> dict:
    """Keep receipt metadata, never echoed input, URLs, credentials, or output."""
    result = {}
    for key, item in value.items():
        if key not in _RESULT_KEYS:
            continue
        if isinstance(item, dict):
            result[key] = {k: v for k, v in item.items() if k in _NESTED_KEYS
                           and (v is None or type(v) in {str, int, bool, float})}
        elif item is None or type(item) in {str, int, bool, float}:
            result[key] = item
    return result


def receiver_route_uses_own_auth(scope: dict) -> bool:
    return (scope.get("type") == "http" and scope.get("method") == "POST"
            and scope.get("path") in _PATHS)


class RemoteReceiverManager:
    def __init__(self, data_root: Path | str, public_origin: str, capture_manager=None,
                 *, recover_on_startup: bool = True):
        parsed = urlsplit(public_origin) if isinstance(public_origin, str) else None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or len(public_origin) > 512):
            raise ValueError("a fixed HTTPS public origin is required")
        self.public_origin = public_origin.rstrip("/")
        self.capture_manager = capture_manager
        self.root = Path(data_root) / "remote_receiver"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.sqlite3"
        self._lock = threading.RLock()
        self._capture_payloads: dict[str, dict] = {}
        self._polling: set[str] = set()
        self._closed = False
        self._runtime_initialized = False
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS enrollments (
              enrollment_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
              device_name TEXT NOT NULL, expected_computer TEXT NOT NULL,
              expires_at REAL NOT NULL, consumed_at REAL);
            CREATE TABLE IF NOT EXISTS devices (
              device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
              device_name TEXT NOT NULL UNIQUE COLLATE NOCASE, expected_computer TEXT NOT NULL,
              created_at REAL NOT NULL, last_seen REAL, instance_id TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
              session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
              state TEXT NOT NULL, created_at REAL NOT NULL, activity_at REAL NOT NULL,
              runtime_json TEXT, foreground_json TEXT, error_code TEXT, closed_at REAL);
            CREATE TABLE IF NOT EXISTS commands (
              command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
              action TEXT NOT NULL, arguments_json TEXT NOT NULL, state TEXT NOT NULL,
              created_at REAL NOT NULL, dispatched_at REAL, finished_at REAL, instance_id TEXT,
              receipt_json TEXT, receipt_digest TEXT, error_code TEXT);
            CREATE INDEX IF NOT EXISTS commands_session ON commands(session_id,created_at);
            CREATE TABLE IF NOT EXISTS operations (
              scope TEXT NOT NULL, operation_id TEXT NOT NULL, digest TEXT NOT NULL,
              session_id TEXT NOT NULL, command_id TEXT NOT NULL,
              PRIMARY KEY(scope,operation_id));
        """)
        if recover_on_startup:
            self.initialize_runtime()

    def initialize_runtime(self):
        """Recover delivery state once, at actual service startup, not import."""
        with self._lock:
            if self._runtime_initialized:
                return
            # A process restart can never replay an already dispatched command.
            with self._transaction():
                for row in self._db.execute("SELECT * FROM commands WHERE state='dispatched'").fetchall():
                    self._unknown_locked(row, "receiver_outcome_unknown")
                for row in self._db.execute("SELECT * FROM commands WHERE state='queued' AND action='capture'").fetchall():
                    self._db.execute("UPDATE commands SET state='failed',error_code=?,finished_at=? WHERE command_id=?",
                                     ("receiver_capture_ticket_lost", time.time(), row["command_id"]))
            self._runtime_initialized = True

    def _transaction(self):
        manager = self
        class Transaction:
            def __enter__(self):
                manager._db.execute("BEGIN IMMEDIATE")
            def __exit__(self, kind, value, traceback):
                manager._db.execute("ROLLBACK" if kind else "COMMIT")
        return Transaction()

    def close(self):
        with self._lock:
            if not self._closed:
                self._capture_payloads.clear()
                self._db.close()
                self._closed = True

    def _session_locked(self, session_id: str):
        row = self._db.execute("""SELECT s.*,d.device_name,d.expected_computer,d.instance_id AS device_instance
                                 FROM sessions s JOIN devices d USING(device_id) WHERE session_id=?""",
                               (_identity(session_id),)).fetchone()
        if row is None:
            _error("receiver_session_not_found", "The receiver session was not found", 404)
        return row

    def _command_locked(self, session_id: str, command_id: str):
        row = self._db.execute("SELECT * FROM commands WHERE session_id=? AND command_id=?",
                               (_identity(session_id), _identity(command_id))).fetchone()
        if row is None:
            _error("receiver_command_not_found", "The receiver command was not found", 404)
        return row

    def _device_auth_locked(self, device_id: str, token: str):
        digest = _hash_token(token)
        # Lookup and bad-secret responses intentionally share one diagnostic.
        row = self._db.execute("SELECT * FROM devices WHERE device_id=?", (_identity(device_id),)).fetchone()
        if row is None or not secrets.compare_digest(digest, row["token_hash"]):
            _error("receiver_authentication_required", "Receiver authentication is required", 401)
        return row

    def issue_enrollment(self, device_name: str, expected_computer: str, ttl_seconds: int = 3600) -> dict:
        device_name, expected_computer = _name(device_name), _computer(expected_computer)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            _error("receiver_enrollment_ttl_invalid", "Enrollment lifetime must be 1-86400 seconds")
        enrollment_id, token = uuid.uuid4().hex, secrets.token_urlsafe(48)
        now = time.time()
        with self._lock, self._transaction():
            self._db.execute("DELETE FROM enrollments WHERE consumed_at IS NOT NULL OR expires_at<=?", (now,))
            if self._db.execute("SELECT 1 FROM devices WHERE device_name=? COLLATE NOCASE", (device_name,)).fetchone():
                _error("receiver_device_exists", "This receiver device name is already enrolled", 409)
            if self._db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= MAX_DEVICES:
                _error("receiver_device_limit", "The receiver device limit has been reached", 409)
            # Reissuing replaces an unconsumed ticket for this exact device name.
            self._db.execute("DELETE FROM enrollments WHERE device_name=? COLLATE NOCASE", (device_name,))
            if self._db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] >= MAX_DEVICES:
                _error("receiver_enrollment_limit", "The pending enrollment limit has been reached", 409)
            self._db.execute("INSERT INTO enrollments VALUES(?,?,?,?,?,NULL)",
                             (enrollment_id, _hash_token(token), device_name, expected_computer, now + ttl_seconds))
        return {"enrollment_id": enrollment_id, "enrollment_token": token, "expected_computer": expected_computer,
                "device_name": device_name, "origin": self.public_origin, "expires_at": now + ttl_seconds}

    def enroll(self, enrollment_id: str, token: str, computer_name: str) -> dict:
        enrollment_id, digest, computer_name = _identity(enrollment_id), _hash_token(token), _computer(computer_name)
        device_id, device_token, now = uuid.uuid4().hex, secrets.token_urlsafe(48), time.time()
        with self._lock, self._transaction():
            row = self._db.execute("SELECT * FROM enrollments WHERE enrollment_id=?", (enrollment_id,)).fetchone()
            if (row is None or row["consumed_at"] is not None or row["expires_at"] <= now
                    or not secrets.compare_digest(digest, row["token_hash"])):
                _error("receiver_authentication_required", "Receiver authentication is required", 401)
            if computer_name != row["expected_computer"]:
                _error("receiver_computer_mismatch", "The receiver computer does not match enrollment", 403)
            if self._db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= MAX_DEVICES:
                _error("receiver_device_limit", "The receiver device limit has been reached", 409)
            if self._db.execute("SELECT 1 FROM devices WHERE device_name=? COLLATE NOCASE", (row["device_name"],)).fetchone():
                _error("receiver_device_exists", "This receiver device name is already enrolled", 409)
            self._db.execute("INSERT INTO devices VALUES(?,?,?,?,?,NULL,NULL)",
                             (device_id, _hash_token(device_token), row["device_name"], computer_name, now))
            self._db.execute("UPDATE enrollments SET consumed_at=? WHERE enrollment_id=?", (now, enrollment_id))
        return {"device_id": device_id, "device_token": device_token, "expected_computer": computer_name,
                "device_name": row["device_name"], "origin": self.public_origin}

    def devices(self) -> list[dict]:
        with self._lock, self._transaction():
            self._maintain_locked()
            result = []
            now = time.time()
            for row in self._db.execute("SELECT * FROM devices ORDER BY device_name").fetchall():
                session = self._db.execute("SELECT session_id,state FROM sessions WHERE device_id=? AND state!='closed' ORDER BY created_at DESC LIMIT 1", (row["device_id"],)).fetchone()
                age = max(0.0, now - row["last_seen"]) if row["last_seen"] is not None else None
                # The idle checker exits between scheduled polls. A recent
                # heartbeat is evidence of a visit, not a persistent connection.
                availability = ("never_seen" if age is None else "recently_seen"
                                if age <= DEVICE_RECENTLY_SEEN_SECONDS else "not_recently_seen")
                result.append({"device_id": row["device_id"], "device_name": row["device_name"],
                               "expected_computer": row["expected_computer"], "last_seen": row["last_seen"],
                               "availability": availability, "last_seen_age_seconds": age,
                               "poll_interval_seconds": DEVICE_POLL_INTERVAL_SECONDS,
                               "connection_mode": "periodic_poll",
                               "session_id": session["session_id"] if session else None,
                               "state": session["state"] if session else "enrolled"})
            return result

    def _dedupe_locked(self, scope: str, operation_id: str, digest: str):
        row = self._db.execute("SELECT * FROM operations WHERE scope=? AND operation_id=?", (scope, operation_id)).fetchone()
        if row is not None:
            if not secrets.compare_digest(row["digest"], digest):
                _error("receiver_operation_conflict", "This operation ID was used for different arguments", 409)
            return self._command_view_locked(self._command_locked(row["session_id"], row["command_id"]))
        return None

    def _enqueue_locked(self, session_id: str, action: str, arguments: dict, scope: str, operation_id: str, digest: str):
        if self._db.execute("SELECT 1 FROM commands WHERE session_id=? AND state IN ('queued','dispatched')", (session_id,)).fetchone():
            _error("receiver_command_busy", "This receiver session has a command awaiting its receipt", 409)
        if action != "stop" and self._db.execute("SELECT COUNT(*) FROM commands WHERE session_id=?", (session_id,)).fetchone()[0] >= 256:
            _error("receiver_command_limit", "Close this receiver session before issuing more commands", 409)
        command_id = uuid.uuid4().hex
        stored = dict(arguments)
        if action == "capture":
            self._capture_payloads[command_id] = dict(arguments)
            stored = {"capture_id": arguments["capture_id"]}
        self._db.execute("INSERT INTO commands(command_id,session_id,action,arguments_json,state,created_at) VALUES(?,?,?,?,?,?)",
                         (command_id, session_id, action, _json(stored), "queued", time.time()))
        self._db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (scope, operation_id, digest, session_id, command_id))
        return self._command_view_locked(self._command_locked(session_id, command_id))

    def start_session(self, device_name: str, operation_id: str) -> dict:
        device_name, operation_id = _name(device_name), _operation(operation_id)
        with self._lock, self._transaction():
            self._maintain_locked()
            device = self._db.execute("SELECT * FROM devices WHERE device_name=? COLLATE NOCASE", (device_name,)).fetchone()
            if device is None:
                _error("receiver_device_not_found", "The receiver device was not found", 404)
            scope, digest = "device:" + device["device_id"], _digest({"action": "start", "device_name": device["device_name"]})
            duplicate = self._dedupe_locked(scope, operation_id, digest)
            if duplicate is not None:
                return duplicate
            if self._db.execute("SELECT 1 FROM sessions WHERE device_id=? AND state!='closed'", (device["device_id"],)).fetchone():
                _error("receiver_session_busy", "Close the existing receiver session before starting another", 409)
            if self._db.execute("SELECT COUNT(*) FROM sessions WHERE state!='closed'").fetchone()[0] >= MAX_SESSIONS:
                _error("receiver_session_limit", "The active receiver session limit has been reached", 409)
            session_id, now = uuid.uuid4().hex, time.time()
            self._db.execute("INSERT INTO sessions(session_id,device_id,state,created_at,activity_at) VALUES(?,?,?,?,?)",
                             (session_id, device["device_id"], "starting", now, now))
            return self._enqueue_locked(session_id, "start", {"origin": self.public_origin}, scope, operation_id, digest)

    def submit(self, session_id: str, action: str, args: dict, operation_id: str) -> dict:
        session_id, operation_id = _identity(session_id), _operation(operation_id)
        if not isinstance(action, str) or action not in _ACTIONS:
            _error("receiver_action_invalid", "Choose one fixed receiver action")
        try:
            RemoteKeyboardSession._validate(action, args)
        except RemoteKeyboardError:
            _error("receiver_arguments_invalid", "The receiver arguments did not match the fixed action schema")
        canonical = _json({"action": action, "arguments": args})
        if action == "capture" and urlsplit(args["upload_url"]).netloc != urlsplit(self.public_origin).netloc:
            _error("receiver_capture_origin_invalid", "The capture upload must use the configured receiver origin")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._lock, self._transaction():
            self._maintain_locked()
            session = self._session_locked(session_id)
            scope = "session:" + session_id
            duplicate = self._dedupe_locked(scope, operation_id, digest)
            if duplicate is not None:
                return duplicate
            if session["state"] != "ready":
                _error("receiver_session_not_ready", "This receiver session is not ready for commands", 409)
            self._db.execute("UPDATE sessions SET activity_at=? WHERE session_id=?", (time.time(), session_id))
            return self._enqueue_locked(session_id, action, args, scope, operation_id, digest)

    def _command_view_locked(self, command) -> dict:
        session = self._session_locked(command["session_id"])
        result = {"session_id": command["session_id"], "command_id": command["command_id"],
                  "action": command["action"], "state": command["state"], "status": command["state"],
                  "session_state": session["state"], "created_at": command["created_at"]}
        if command["receipt_json"]:
            receipt = json.loads(command["receipt_json"])
            result.update(receipt)
        if command["error_code"]:
            result["error_code"] = command["error_code"]
        if command["state"] == "outcome_unknown":
            result.update({"outcome_unknown": True, "action_may_have_run": True})
        return result

    def command_status(self, session_id: str, command_id: str) -> dict:
        with self._lock, self._transaction():
            self._maintain_locked()
            command = self._command_locked(session_id, command_id)
            self._db.execute("UPDATE sessions SET activity_at=? WHERE session_id=?", (time.time(), session_id))
            return self._command_view_locked(command)

    def session_status(self, session_id: str) -> dict:
        with self._lock, self._transaction():
            self._maintain_locked()
            row = self._session_locked(session_id)
            self._db.execute("UPDATE sessions SET activity_at=? WHERE session_id=?", (time.time(), session_id))
            result = {"session_id": row["session_id"], "device_id": row["device_id"],
                      "device_name": row["device_name"], "expected_computer": row["expected_computer"],
                      "state": row["state"], "status": row["state"], "created_at": row["created_at"]}
            for field in ("runtime", "foreground"):
                if row[field + "_json"]:
                    result[field] = json.loads(row[field + "_json"])
            if row["error_code"]:
                result["error_code"] = row["error_code"]
            latest = self._db.execute("SELECT * FROM commands WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
            if latest:
                result["last_command"] = self._command_view_locked(latest)
            capture = self._db.execute("SELECT receipt_json FROM commands WHERE session_id=? AND action='capture' AND state='completed' ORDER BY finished_at DESC LIMIT 1", (session_id,)).fetchone()
            if capture:
                result["last_capture"] = json.loads(capture[0])["result"]
            return result

    def _stop_locked(self, session_id: str, operation_id: str):
        scope, digest = "session:" + session_id, _digest({"action": "stop", "arguments": {}})
        duplicate = self._dedupe_locked(scope, operation_id, digest)
        if duplicate is not None:
            return duplicate
        session = self._session_locked(session_id)
        if session["state"] == "closed":
            return {"session_id": session_id, "state": "closed", "status": "closed", "session_state": "closed"}
        pending = self._db.execute("SELECT * FROM commands WHERE session_id=? AND state IN ('queued','dispatched')", (session_id,)).fetchall()
        for command in pending:
            if command["action"] == "stop":
                # A second close operation observes the existing exact stop.
                self._db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (scope, operation_id, digest, session_id, command["command_id"]))
                return self._command_view_locked(command)
            if command["state"] == "dispatched":
                self._unknown_locked(command, "receiver_outcome_unknown")
            else:
                self._db.execute("UPDATE commands SET state='failed',error_code='receiver_command_cancelled',finished_at=? WHERE command_id=?", (time.time(), command["command_id"]))
                self._capture_payloads.pop(command["command_id"], None)
        self._db.execute("UPDATE sessions SET state='closing',activity_at=? WHERE session_id=?", (time.time(), session_id))
        return self._enqueue_locked(session_id, "stop", {}, scope, operation_id, digest)

    def close_session(self, session_id: str, operation_id: str) -> dict:
        session_id, operation_id = _identity(session_id), _operation(operation_id)
        with self._lock, self._transaction():
            self._maintain_locked()
            return self._stop_locked(session_id, operation_id)

    def _unknown_locked(self, command, code: str):
        self._db.execute("UPDATE commands SET state='outcome_unknown',error_code=?,finished_at=? WHERE command_id=? AND state='dispatched'", (code, time.time(), command["command_id"]))
        self._db.execute("UPDATE sessions SET state=CASE WHEN state IN ('closed','closing') THEN state ELSE 'fenced' END,error_code=? WHERE session_id=?", (code, command["session_id"]))
        self._capture_payloads.pop(command["command_id"], None)

    def _maintain_locked(self):
        now = time.time()
        for command in self._db.execute("SELECT * FROM commands WHERE state='dispatched' AND dispatched_at<=?", (now - COMMAND_RECEIPT_SECONDS,)).fetchall():
            self._unknown_locked(command, "receiver_outcome_unknown")
        for row in self._db.execute("SELECT session_id FROM sessions WHERE state NOT IN ('closed','closing') AND activity_at<=?", (now - SESSION_IDLE_SECONDS,)).fetchall():
            self._stop_locked(row["session_id"], "internal-idle-stop")
        self._db.execute("DELETE FROM enrollments WHERE expires_at<=?", (now,))

    def maintain(self):
        with self._lock, self._transaction():
            self._maintain_locked()

    async def poll(self, device_id: str, token: str, computer_name: str, instance_id: str,
                   session_id: str | None = None, *, timeout_seconds: float = POLL_SECONDS) -> dict:
        computer_name, instance_id = _computer(computer_name), _identity(instance_id)
        if session_id is not None:
            _identity(session_id)
        if (type(timeout_seconds) not in {float, int} or not math.isfinite(timeout_seconds)
                or not 0 <= timeout_seconds <= POLL_SECONDS):
            _error("receiver_poll_timeout_invalid", "The receiver poll timeout is invalid")
        with self._lock, self._transaction():
            device = self._device_auth_locked(device_id, token)
            if device["expected_computer"] != computer_name:
                _error("receiver_computer_mismatch", "The receiver computer does not match enrollment", 403)
            if device_id in self._polling:
                _error("receiver_poll_busy", "This receiver already has an active poll", 409)
            if session_id is not None and self._session_locked(session_id)["device_id"] != device_id:
                _error("receiver_session_mismatch", "The receiver session does not belong to this device", 403)
            # A new poll without a receipt means the previous delivery may have
            # run. Neither reconnects nor a replacement receiver replay it.
            code = "receiver_instance_changed" if device["instance_id"] and device["instance_id"] != instance_id else "receiver_outcome_unknown"
            for command in self._db.execute("SELECT c.* FROM commands c JOIN sessions s USING(session_id) WHERE s.device_id=? AND c.state='dispatched'", (device_id,)).fetchall():
                self._unknown_locked(command, code)
            if code == "receiver_instance_changed":
                self._db.execute("UPDATE sessions SET state='fenced',error_code=? WHERE device_id=? AND state NOT IN ('closed','closing') AND EXISTS (SELECT 1 FROM commands c WHERE c.session_id=sessions.session_id AND c.action='start' AND c.state!='queued')", (code, device_id))
                # Queued input was validated against a child in the old process.
                for queued in self._db.execute("SELECT c.command_id FROM commands c JOIN sessions s USING(session_id) WHERE s.device_id=? AND c.state='queued' AND c.action NOT IN ('start','stop')", (device_id,)).fetchall():
                    self._capture_payloads.pop(queued["command_id"], None)
                self._db.execute("UPDATE commands SET state='failed',error_code=?,finished_at=? WHERE state='queued' AND action NOT IN ('start','stop') AND session_id IN (SELECT session_id FROM sessions WHERE device_id=?)", (code, time.time(), device_id))
            self._db.execute("UPDATE devices SET instance_id=?,last_seen=? WHERE device_id=?", (instance_id, time.time(), device_id))
            if code == "receiver_instance_changed":
                # A replacement instance can clean up an already-closing
                # session. Issue a new, distinct stop only; keep every earlier
                # unknown receipt intact and require a real shutdown receipt.
                closing = self._db.execute("""SELECT s.session_id FROM sessions s
                    WHERE s.device_id=? AND s.state='closing'
                    AND EXISTS (SELECT 1 FROM commands c WHERE c.session_id=s.session_id
                                AND c.action='stop' AND c.state='outcome_unknown'
                                AND c.dispatched_at IS NOT NULL)
                    AND NOT EXISTS (SELECT 1 FROM commands c WHERE c.session_id=s.session_id
                                    AND c.state IN ('queued','dispatched'))""", (device_id,)).fetchall()
                for session in closing:
                    self._stop_locked(session['session_id'], 'internal-instance-stop:' + instance_id)
            self._polling.add(device_id)
        try:
            deadline = time.monotonic() + timeout_seconds
            while True:
                with self._lock, self._transaction():
                    self._maintain_locked()
                    command = self._db.execute("SELECT c.*,s.expected_computer FROM commands c JOIN (SELECT s.*,d.expected_computer FROM sessions s JOIN devices d USING(device_id)) s USING(session_id) WHERE s.device_id=? AND c.state='queued' ORDER BY c.created_at LIMIT 1", (device_id,)).fetchone()
                    if command is not None:
                        arguments = json.loads(command["arguments_json"])
                        if command["action"] == "capture":
                            arguments = self._capture_payloads.pop(command["command_id"], None)
                            if arguments is None:
                                self._db.execute("UPDATE commands SET state='failed',error_code='receiver_capture_ticket_lost',finished_at=? WHERE command_id=?", (time.time(), command["command_id"]))
                                continue
                        self._db.execute("UPDATE commands SET state='dispatched',dispatched_at=?,instance_id=? WHERE command_id=?", (time.time(), instance_id, command["command_id"]))
                        return {"kind": "command", "command": {"command_id": command["command_id"], "session_id": command["session_id"], "action": command["action"], "arguments": arguments, "expected_computer": command["expected_computer"]}}
                if time.monotonic() >= deadline:
                    return {"kind": "idle"}
                await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        finally:
            with self._lock:
                self._polling.discard(device_id)

    def mark_delivery_unknown(self, session_id: str, command_id: str):
        """Fence a dispatched response when its HTTP delivery is interrupted."""
        with self._lock, self._transaction():
            command = self._command_locked(session_id, command_id)
            if command["state"] == "dispatched":
                self._unknown_locked(command, "receiver_outcome_unknown")

    def reply(self, device_id: str, token: str, command_id: str, session_id: str,
              ok: bool, result: dict | None = None, error: dict | None = None) -> dict:
        if type(ok) is not bool or (ok and not isinstance(result, dict)) or (not ok and not isinstance(error, dict)):
            _error("receiver_reply_invalid", "The receiver reply has an invalid schema")
        raw_receipt = {"ok": ok, "result": result} if ok else {"ok": ok, "error": error}
        digest = _digest(raw_receipt)
        code = error.get("code") if not ok else None
        safe_code = code if isinstance(code, str) and code in _REMOTE_ERROR_CODES else "receiver_command_failed"
        safe = {"ok": ok, "result": _safe_result(result)} if ok else {"ok": False, "error": {"code": safe_code, "message": "The receiver helper rejected this operation"}}
        with self._lock, self._transaction():
            self._device_auth_locked(device_id, token)
            session = self._session_locked(session_id)
            if session["device_id"] != device_id:
                _error("receiver_session_mismatch", "The receiver session does not belong to this device", 403)
            command = self._command_locked(session_id, command_id)
            if command["receipt_digest"] is not None:
                if not secrets.compare_digest(command["receipt_digest"], digest):
                    _error("receiver_reply_conflict", "This command already has a different receiver receipt", 409)
                return {"acknowledged": True, "command_id": command_id, "session_id": session_id}
            if command["state"] not in {"dispatched", "outcome_unknown"}:
                _error("receiver_reply_not_dispatched", "This command was not dispatched to the receiver", 409)
            if ok and command["action"] == "start":
                runtime = result.get("runtime", result.get("remote_runtime"))
                if not isinstance(runtime, dict) or str(runtime.get("computer", "")).upper() != session["expected_computer"]:
                    _error("receiver_runtime_invalid", "The receiver runtime did not prove the expected computer", 409)
            if ok and command["action"] == "stop" and result.get("helper_stopped") is not True:
                _error("receiver_stop_unconfirmed", "The receiver did not confirm helper shutdown", 409)
            self._db.execute("UPDATE commands SET state=?,receipt_json=?,receipt_digest=?,finished_at=?,error_code=? WHERE command_id=?", ("completed" if ok else "failed", _json(safe), digest, time.time(), None if ok else safe_code, command_id))
            if ok:
                metadata = safe["result"]
                runtime = metadata.get("runtime", metadata.get("remote_runtime"))
                if command["action"] == "status" and runtime is None:
                    runtime = {key: value for key, value in metadata.items() if key in _NESTED_KEYS and key not in {"foreground", "remote_foreground"}}
                foreground = metadata.get("foreground", metadata.get("remote_foreground"))
                if isinstance(runtime, dict):
                    self._db.execute("UPDATE sessions SET runtime_json=? WHERE session_id=?", (_json(runtime), session_id))
                if isinstance(foreground, dict):
                    self._db.execute("UPDATE sessions SET foreground_json=? WHERE session_id=?", (_json(foreground), session_id))
                if command["action"] == "stop":
                    self._db.execute("UPDATE sessions SET state='closed',closed_at=?,error_code=NULL WHERE session_id=?", (time.time(), session_id))
                elif (session["state"] not in {"closed", "closing"} and command["instance_id"] == session["device_instance"]
                      and session["error_code"] != "receiver_instance_changed"):
                    self._db.execute("UPDATE sessions SET state='ready',error_code=NULL WHERE session_id=?", (session_id,))
            elif command["action"] in _INPUT_ACTIONS | {"start", "stop"}:
                self._db.execute("UPDATE sessions SET state=CASE WHEN state='closed' THEN state ELSE 'fenced' END,error_code=? WHERE session_id=?", (safe_code, session_id))
            return {"acknowledged": True, "command_id": command_id, "session_id": session_id}


async def _body(request: Request, required: set[str], optional: set[str] = frozenset()) -> dict:
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        _error("receiver_encoding_unsupported", "Receiver content encoding is not supported", 415)
    size = request.headers.get("content-length")
    if size is not None:
        try:
            declared = int(size)
        except ValueError:
            _error("receiver_body_invalid", "The receiver request body is invalid")
        if declared < 0:
            _error("receiver_body_invalid", "The receiver request body is invalid")
        if declared > MAX_BODY_BYTES:
            _error("receiver_body_too_large", "The receiver request exceeds its size limit", 413)
    payload = bytearray()
    try:
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_BODY_BYTES:
                _error("receiver_body_too_large", "The receiver request exceeds its size limit", 413)
            payload.extend(chunk)
        value = json.loads(payload)
    except (ValueError, UnicodeError, ClientDisconnect, RecursionError):
        _error("receiver_body_invalid", "The receiver request body is invalid")
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        _error("receiver_schema_invalid", "The receiver request has an invalid schema")
    return value


def _bearer(request: Request) -> str:
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not separator:
        _error("receiver_authentication_required", "Receiver authentication is required", 401)
    _hash_token(token)
    return token


def create_receiver_router(manager: RemoteReceiverManager) -> APIRouter:
    class DeliveryResponse(JSONResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            except (OSError, asyncio.CancelledError):
                command = self.receiver_command
                manager.mark_delivery_unknown(command["session_id"], command["command_id"])
                raise

    @asynccontextmanager
    async def lifespan(_app):
        async def sweep():
            while True:
                await asyncio.sleep(1)
                manager.maintain()
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            manager.close()

    router = APIRouter(lifespan=lifespan)
    router.receiver_manager = manager

    async def run(request: Request, action: str):
        try:
            token = _bearer(request)
            if action == "enroll":
                body = await _body(request, {"enrollment_id", "computer_name"})
                result = manager.enroll(body["enrollment_id"], token, body["computer_name"])
            elif action == "poll":
                body = await _body(request, {"device_id", "computer_name", "instance_id"}, {"session_id"})
                result = await manager.poll(body["device_id"], token, body["computer_name"], body["instance_id"], body.get("session_id"))
            else:
                body = await _body(request, {"device_id", "command_id", "session_id", "ok"}, {"result", "error"})
                if (body["ok"] is True and ("result" not in body or "error" in body)
                        or body["ok"] is False and ("error" not in body or "result" in body)):
                    _error("receiver_reply_invalid", "The receiver reply has an invalid schema")
                result = manager.reply(body["device_id"], token, body["command_id"], body["session_id"], body["ok"], body.get("result"), body.get("error"))
            if action == "poll" and result.get("kind") == "command":
                command = result["command"]
                if await request.is_disconnected():
                    manager.mark_delivery_unknown(command["session_id"], command["command_id"])
                response = DeliveryResponse(result, headers={"Cache-Control": "no-store"})
                response.receiver_command = command
                return response
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except RemoteReceiverError as error:
            return JSONResponse({"ok": False, "error": {"code": error.code, "message": error.message}}, status_code=error.status_code, headers={"Cache-Control": "no-store"})

    @router.post("/remote-receiver/enroll")
    async def enroll(request: Request):
        return await run(request, "enroll")

    @router.post("/remote-receiver/poll")
    async def poll(request: Request):
        return await run(request, "poll")

    @router.post("/remote-receiver/reply")
    async def reply(request: Request):
        return await run(request, "reply")

    return router


create_remote_receiver_router = create_receiver_router
