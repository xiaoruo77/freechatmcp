"""MCP-facing control of an independently enrolled remote receiver.

This adapter never imports or invokes a local input backend. A command being
accepted or delivered is not evidence that the intended GUI effect occurred.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .uu_remote_keyboard import RemoteKeyboardError, RemoteKeyboardSession


class RemoteControlResult(TypedDict):
    ok: bool
    status: str
    terminal: bool
    action: str
    background_only: bool
    host_input_used: bool
    foreground_input_used: bool
    remote_effect_verified: bool
    session_id: NotRequired[str]
    command_id: NotRequired[str]
    error_code: NotRequired[str]
    capture: NotRequired[dict[str, Any]]


class _ControlError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Operation:
    digest: str
    action: str
    session_id: str
    command_id: str
    submitted_at: float
    generation: int
    capture_id: str | None = None
    capture_result: dict[str, Any] | None = None
    capture_failed: bool = False
    ticket_deleted: bool = False


@dataclass
class _Snapshot:
    captured_at: float
    bounds: dict[str, int]
    foreground: dict[str, Any]


_ACTIONS = {"devices", "status", "enroll", "connect", "session_status", "screenshot",
            "click", "type", "key", "scroll", "drag", "command_status", "close"}
_INPUT = {"click", "type", "key", "scroll", "drag"}
_WRITE = _INPUT | {"enroll", "connect", "close"}
_TERMINAL = {"completed", "failed", "outcome_unknown", "closed"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_DEVICE_FIELDS = {"device_name", "expected_computer", "computer", "state", "status",
                  "online", "last_seen", "last_seen_at", "enrolled_at", "device_id",
                  "availability", "last_seen_age_seconds", "poll_interval_seconds", "connection_mode",
                  "active_session_id", "session_id", "created_at", "updated_at"}
_SESSION_FIELDS = {"session_id", "device_name", "expected_computer", "state", "status",
                   "error_code", "created_at", "updated_at", "session_state"}


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _ControlError(f"invalid_{field}")
    return value


def _name(value: Any, field: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(ord(c) < 32 for c in value)):
        raise _ControlError(f"invalid_{field}")
    return value


def _project(value: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: item for key, item in value.items()
            if key in fields and isinstance(item, (str, int, float, bool, type(None)))}


class RemoteReceiverController:
    """Serialize adaptation and retain evidence without retaining upload secrets."""

    def __init__(self, manager: Any, capture_manager: Any, data_root: str | Path):
        self.manager = manager
        self.capture_manager = capture_manager
        self.data_root = Path(data_root).resolve()
        self.capture_root = self.data_root / "background_desktop" / "captures"
        self._lock = asyncio.Lock()
        self._operations: dict[tuple[str, str], _Operation] = {}
        self._commands: dict[tuple[str, str], _Operation] = {}
        self._snapshots: dict[str, _Snapshot] = {}
        self._generations: dict[str, int] = {}
        self._clock = time.monotonic

    @staticmethod
    def _base(action: str, status: str, **extra: Any) -> RemoteControlResult:
        return {"ok": status not in {"failed", "outcome_unknown", "fenced", "error"},
                "status": status, "terminal": status in _TERMINAL or status == "error",
                "action": action, "background_only": True, "host_input_used": False,
                "foreground_input_used": False, "remote_effect_verified": False, **extra}

    async def perform(self, action: str, args: dict[str, Any] | None,
                      confirm: bool = False) -> RemoteControlResult:
        """Accept a fixed action. Poll command_status to observe terminal outcomes.

        The enrollment result contains a one-time enrollment credential intended
        exclusively for the private receiver installer, never for public logging.
        """
        async with self._lock:
            try:
                if not isinstance(action, str) or action not in _ACTIONS:
                    raise _ControlError("unsupported_remote_action")
                if args is None:
                    args = {}
                if not isinstance(args, dict):
                    raise _ControlError("invalid_arguments")
                if action in _WRITE and confirm is not True:
                    raise _ControlError("confirmation_required")
                return self._perform(action, args)
            except (_ControlError, RemoteKeyboardError) as error:
                return self._base(action if isinstance(action, str) else "invalid", "error",
                                  error_code=error.code)
            except Exception as error:
                # Fixed manager error codes are safe; arbitrary exception text
                # could include a capture token, enrollment token, or user text.
                code = getattr(error, "code", None)
                if (type(error).__name__ != "RemoteReceiverError" or not isinstance(code, str)
                        or not re.fullmatch(r"[a-z0-9_]{1,100}", code)):
                    code = "remote_control_failed"
                return self._base(action if isinstance(action, str) else "invalid", "error",
                                  error_code=code)

    @staticmethod
    def _fields(args: dict[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
        if not required <= args.keys() or args.keys() - required - optional:
            raise _ControlError("invalid_arguments")

    def _perform(self, action: str, args: dict[str, Any]) -> RemoteControlResult:
        if action in {"devices", "status"}:
            self._fields(args, set(), {"device_name"} if action == "status" else set())
            devices = self.manager.devices()
            if isinstance(devices, dict):
                devices = devices.get("devices", [])
            if not isinstance(devices, list):
                raise _ControlError("invalid_device_status")
            if "device_name" in args:
                name = _name(args["device_name"], "device_name")
                devices = [item for item in devices if item.get("device_name") == name]
            return self._base(action, "completed", devices=[_project(item, _DEVICE_FIELDS)
                                                            for item in devices if isinstance(item, dict)])
        if action == "enroll":
            self._fields(args, {"device_name", "expected_computer"})
            result = self.manager.issue_enrollment(_name(args["device_name"], "device_name"),
                                                   _name(args["expected_computer"], "expected_computer"))
            return self._base(action, "completed", **_project(result, {
                "enrollment_id", "enrollment_token", "expected_computer", "device_name", "origin", "expires_at"}))
        if action == "connect":
            self._fields(args, {"device_name", "operation_id"})
            result = self.manager.start_session(_name(args["device_name"], "device_name"),
                                                _identifier(args["operation_id"], "operation_id"))
            return self._command_view(action, result)
        if action == "session_status":
            self._fields(args, {"session_id"})
            session_id = _identifier(args["session_id"], "session_id")
            value = self.manager.session_status(session_id)
            state = value.get("state", value.get("status", "unknown"))
            result = self._base(action, state, **_project(value, _SESSION_FIELDS - {"status", "state"}))
            result["session_id"] = session_id
            result["terminal"] = state in {"closed", "failed", "fenced"}
            return result
        if action == "command_status":
            self._fields(args, {"session_id", "command_id"})
            session_id = _identifier(args["session_id"], "session_id")
            command_id = _identifier(args["command_id"], "command_id")
            value = self.manager.command_status(session_id, command_id)
            return self._command_view(action, value, self._commands.get((session_id, command_id)))
        if action == "close":
            self._fields(args, {"session_id", "operation_id"})
            session_id = _identifier(args["session_id"], "session_id")
            operation_id = _identifier(args["operation_id"], "operation_id")
            result = self.manager.close_session(session_id, operation_id)
            self._snapshots.pop(session_id, None)
            self._generations[session_id] = self._generations.get(session_id, 0) + 1
            # close_session terminally cancels queued work and fences work
            # already dispatched. Revoke its outstanding image credentials
            # even when the caller never polls those old commands again.
            for operation in self._commands.values():
                if (operation.session_id == session_id and operation.capture_id is not None
                        and not operation.ticket_deleted):
                    self._delete_ticket(operation.capture_id)
                    operation.ticket_deleted = True
                    if operation.capture_result is None:
                        operation.capture_failed = True
            return self._command_view(action, result)
        return self._submit(action, args)

    def _submit(self, action: str, args: dict[str, Any]) -> RemoteControlResult:
        common = {"session_id", "operation_id"}
        fields = {"screenshot": set(), "click": {"x", "y", "button", "count"},
                  "scroll": {"x", "y", "vertical", "horizontal"},
                  "drag": {"x", "y", "to_x", "to_y", "duration_ms"},
                  "type": {"text"}, "key": {"keys"}}[action]
        # Input properties may be flat or in one explicitly named args object.
        if "args" in args:
            self._fields(args, common | {"args"})
            user = args["args"]
            if not isinstance(user, dict):
                raise _ControlError("invalid_arguments")
        else:
            self._fields(args, common, fields)
            user = {key: value for key, value in args.items() if key not in common}
        defaults = {"click": {"button": "left", "count": 1},
                    "scroll": {"vertical": 0, "horizontal": 0},
                    "drag": {"duration_ms": 500}}.get(action, {})
        user = {**defaults, **user}
        self._fields(user, fields)
        session_id = _identifier(args["session_id"], "session_id")
        operation_id = _identifier(args["operation_id"], "operation_id")
        try:
            digest = hashlib.sha256(json.dumps({"action": action, "arguments": user},
                sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        except (TypeError, ValueError, UnicodeError):
            raise _ControlError("invalid_arguments") from None
        previous = self._operations.get((session_id, operation_id))
        if previous:
            if previous.digest != digest:
                raise _ControlError("operation_id_conflict")
            return self._command_view(action, self.manager.command_status(session_id, previous.command_id), previous)
        capture_id = None
        if action == "screenshot":
            if self.capture_manager is None:
                raise _ControlError("remote_capture_unavailable")
            ticket = self.capture_manager.issue()
            capture_id = ticket["capture_id"]
            payload = {key: ticket[key] for key in ("capture_id", "upload_url", "token")}
            command_action = "capture"
        else:
            snapshot = self._snapshots.get(session_id)
            if snapshot is None or not 0 <= self._clock() - snapshot.captured_at <= 60:
                self._snapshots.pop(session_id, None)
                raise _ControlError("fresh_remote_screenshot_required")
            payload = dict(user)
            command_action = action
            if action in {"type", "key"}:
                fingerprint = snapshot.foreground
                if fingerprint.get("available") is not True:
                    raise _ControlError("remote_foreground_unavailable")
                payload["expect"] = {key: fingerprint.get(key) for key in ("hwnd", "pid", "creation_time_utc_ticks")}
                payload["activate"] = True
            else:
                bounds = snapshot.bounds
                for xkey, ykey in [("x", "y")] + ([("to_x", "to_y")] if action == "drag" else []):
                    x, y = payload.get(xkey), payload.get(ykey)
                    if (type(x) is not int or type(y) is not int or not 0 <= x < bounds["width"]
                            or not 0 <= y < bounds["height"]):
                        raise _ControlError("remote_coordinates_out_of_bounds")
                    payload[xkey], payload[ykey] = x + bounds["left"], y + bounds["top"]
                payload["bounds"] = dict(bounds)
        try:
            RemoteKeyboardSession._validate(command_action, payload)
            value = self.manager.submit(session_id, command_action, payload, operation_id)
            command_id = _identifier(value.get("command_id"), "command_id")
        except Exception:
            if capture_id:
                self._delete_ticket(capture_id)
            raise
        if action in _INPUT:
            self._snapshots.pop(session_id, None)
            self._generations[session_id] = self._generations.get(session_id, 0) + 1
        operation = _Operation(digest, action, session_id, command_id, self._clock(),
                               self._generations.get(session_id, 0), capture_id)
        self._operations[(session_id, operation_id)] = operation
        self._commands[(session_id, command_id)] = operation
        return self._command_view(action, value, operation)

    def _delete_ticket(self, capture_id: str) -> None:
        try:
            self.capture_manager.delete(capture_id)
        except Exception:
            # Expired tickets are already revoked. Never expose their errors.
            pass

    def _command_view(self, action: str, value: dict[str, Any],
                      operation: _Operation | None = None) -> RemoteControlResult:
        state = value.get("state", value.get("status", "unknown"))
        if not isinstance(state, str):
            raise _ControlError("invalid_command_status")
        result = self._base(action, state)
        result.update(_project(value, {"session_id", "command_id", "session_state", "error_code"}))
        if state == "outcome_unknown":
            result.update(outcome_unknown=True, action_may_have_run=True)
        command_action = operation.action if operation else value.get("action")
        if command_action in _INPUT:
            result["verification_required"] = True
            result["delivery_only"] = state == "completed"
        if operation is None and command_action == "capture" and state == "completed":
            # A controller restart does not reconstruct one-use ticket evidence
            # from an untrusted receipt or silently declare an image verified.
            result.update(ok=False, status="failed", terminal=True,
                          capture_verified=False, error_code="remote_capture_evidence_unavailable")
        if operation is None or operation.capture_id is None:
            return result
        if state in _TERMINAL:
            if state == "completed" and operation.capture_result is None and not operation.capture_failed:
                try:
                    capture, snapshot = self._verify_capture(operation, value.get("result"))
                    operation.capture_result = capture
                    if operation.generation == self._generations.get(operation.session_id, 0):
                        self._snapshots[operation.session_id] = snapshot
                except Exception:
                    operation.capture_failed = True
            if not operation.ticket_deleted:
                self._delete_ticket(operation.capture_id)
                operation.ticket_deleted = True
        if operation.capture_failed:
            result.update(ok=False, status="failed", terminal=True, error_code="remote_capture_verification_failed")
        elif operation.capture_result is not None:
            result.update(capture=dict(operation.capture_result), capture_verified=True)
        return result

    def _verify_capture(self, operation: _Operation, receipt: Any) -> tuple[dict[str, Any], _Snapshot]:
        if not isinstance(receipt, dict) or receipt.get("capture_id") != operation.capture_id:
            raise _ControlError("remote_capture_identity_mismatch")
        digest = receipt.get("sha256")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise _ControlError("remote_capture_digest_invalid")
        bounds = receipt.get("remote_bounds")
        if (not isinstance(bounds, dict) or set(bounds) != {"left", "top", "width", "height"}
                or any(type(v) is not int for v in bounds.values())):
            raise _ControlError("remote_capture_bounds_invalid")
        # Reuse the helper's physical desktop constraints without sending input.
        RemoteKeyboardSession._validate("click", {"x": bounds["left"], "y": bounds["top"],
            "bounds": bounds, "button": "left", "count": 1})
        uploaded = self.capture_manager.inspect(operation.capture_id)
        metadata = uploaded.get("capture") if isinstance(uploaded, dict) else None
        if uploaded.get("status") != "completed" or not isinstance(metadata, dict):
            raise _ControlError("remote_capture_upload_missing")
        mime_type = metadata.get("mime_type")
        if mime_type not in {"image/png", "image/jpeg"}:
            raise _ControlError("remote_capture_metadata_mismatch")
        suffix = ".png" if mime_type == "image/png" else ".jpg"
        path = Path(metadata.get("path", "")).resolve(strict=True)
        expected = (self.capture_root / f"gui-{operation.capture_id}{suffix}").resolve()
        root = self.capture_root.resolve()
        if (not root.is_relative_to(self.data_root) or path != expected
                or not path.is_relative_to(root) or not path.is_file()):
            raise _ControlError("remote_capture_path_invalid")
        from .remote_gui_capture import MAX_UPLOAD_BYTES, _validate_png
        maximum, validator = MAX_UPLOAD_BYTES, _validate_png
        if mime_type == "image/jpeg":
            from .remote_gui_capture import MAX_JPEG_UPLOAD_BYTES, _validate_jpeg
            maximum, validator = MAX_JPEG_UPLOAD_BYTES, _validate_jpeg
        if path.stat().st_size > maximum:
            raise _ControlError("remote_capture_size_invalid")
        data = path.read_bytes()
        width, height = validator(data)
        if (hashlib.sha256(data).hexdigest() != digest or metadata.get("sha256") != digest
                or metadata.get("width") != width or metadata.get("height") != height
                or metadata.get("size", metadata.get("size_bytes")) != len(data)
                or receipt.get("mime_type", mime_type) != mime_type
                or (width, height) != (bounds["width"], bounds["height"])
                or receipt.get("width", width) != width or receipt.get("height", height) != height):
            raise _ControlError("remote_capture_metadata_mismatch")
        foreground = receipt.get("foreground")
        if not isinstance(foreground, dict) or type(foreground.get("available")) is not bool:
            raise _ControlError("remote_capture_foreground_invalid")
        if foreground["available"]:
            fingerprint = {key: foreground.get(key) for key in ("hwnd", "pid", "creation_time_utc_ticks")}
            RemoteKeyboardSession._validate("key", {"keys": "TAB", "expect": fingerprint, "activate": True})
            foreground = {"available": True, **fingerprint}
        else:
            foreground = {"available": False}
        capture = {"path": str(path), "mime_type": mime_type, "width": width, "height": height,
                   "size_bytes": len(data), "sha256": digest}
        return capture, _Snapshot(operation.submitted_at, dict(bounds), foreground)
