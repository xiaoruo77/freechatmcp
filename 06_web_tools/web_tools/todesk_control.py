"""Stateful, fail-closed control of one explicitly selected ToDesk viewer.

This module never changes foreground focus or sends global desktop input.
The native and capture backends are lazy so importing it is safe on any OS.
Operation receipts are retained for the session object's entire lifetime;
uncertain delivery blocks new input until the exact viewer has been closed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import threading
import time
import uuid


class ToDeskSessionError(RuntimeError):
    def __init__(self, code: str, *, may_have_run: bool = False):
        super().__init__(code)
        self.code = code
        self.may_have_run = may_have_run


_MESSAGES = {
    'background_input_unverified': 'ToDesk 后台键鼠尚未验证有效，已在派发前停止；不会切换前台或使用本机鼠标键盘。',
    'background_connection_unverified': '尚无已连接的 ToDesk 远控窗口；严格后台模式不会弹出或激活主窗口自动连接。',
    'background_viewer_minimized': 'ToDesk 远控窗口已最小化；严格后台模式不会擅自恢复窗口。',
    "action_invalid": "Choose a supported ToDesk action.",
    "arguments_invalid": "The action arguments are invalid.",
    "device_not_allowed": "Choose one configured device_name from devices.",
    "device_name_required": "connect requires one exact configured device_name.",
    "session_already_attached": "Close the attached viewer before selecting another device.",
    "session_not_attached": "Connect to an allowed ToDesk viewer first.",
    "viewer_identity_invalid": "The ToDesk viewer identity could not be verified.",
    "viewer_identity_changed": "The attached ToDesk viewer identity changed.",
    "screenshot_required": "Capture a fresh screenshot of this viewer before input.",
    "screenshot_stale": "The screenshot is older than 60 seconds; capture it again.",
    "viewer_geometry_changed": "The viewer geometry changed; capture a new screenshot.",
    "coordinate_outside_client": "Input coordinates must lie inside the captured client area.",
    "operation_id_required": "Input requires a bounded, stable operation_id.",
    "operation_id_conflict": "This operation_id already belongs to different input arguments.",
    "operation_cache_full": "The operation receipt limit was reached; new input is disabled.",
    "input_delivery_unknown": "Input delivery is uncertain. Verify the screenshot and close this session before further input.",
    "input_fenced": "Uncertain delivery blocks new input until this exact viewer is closed and a new session is connected.",
    "input_rejected": "The viewer rejected this input before delivery.",
    "capture_invalid": "The capture backend did not return a valid viewer PNG.",
    "cleanup_unconfirmed": "The exact viewer has not been confirmed closed; its session ownership was preserved.",
    "native_failed": "The verified ToDesk operation could not be completed.",
}

# Only code identifiers from our own backends may reach public results. Native
# exception text, process command lines and complete configuration never do.
_BACKEND_CODES = frozenset({
    'connection_not_established','saved_device_main_identity_changed','saved_device_uia_unavailable',
    'saved_device_tree_too_large','saved_device_main_ambiguous','saved_device_main_unavailable',
    'saved_device_selection_required','saved_device_identity_unverified','saved_device_action_ambiguous',
    'saved_device_pattern_unsupported','saved_device_foreground_changed','saved_device_invocation_failed',
    'saved_device_code_ambiguous','saved_device_com_unavailable','saved_device_config_invalid',
    'saved_device_control_ambiguous','saved_device_control_disabled','saved_device_delivery_unknown',
    'saved_device_detail_unverified','saved_device_selected_code_mismatch','saved_device_target_stale',
    'saved_device_tree_unavailable','saved_device_unavailable','connection_foreground_changed',
    'connection_request_unavailable','input_focus_unavailable',
    "windows_required", "native_unavailable", "executable_invalid",
    "executable_not_found", "device_not_allowed", "device_name_required",
    "viewer_not_found", "viewer_ambiguous", "viewer_required",
    "background_connect_unsupported", "connect_unsupported", "connection_unsupported",
    "window_missing", "window_identity_changed", "window_identity_invalid",
    "window_verification_failed", "process_identity_changed", "process_identity_invalid",
    "process_verification_failed", "window_minimized", "window_hung",
    "window_restore_failed", "window_not_renderable", "window_size_invalid",
    "window_bounds_invalid", "client_bounds_invalid", "arguments_invalid",
    "input_invalid", "key_unsupported", "modifier_chord_unsupported",
    "input_delivery_unknown", "delivery_unknown", "message_delivery_failed",
    "close_pending", "capture_failed", "capture_unsupported", "capture_invalid",
    "capture_blank", "capture_unrendered", "printwindow_failed",
    "capture_geometry_changed", "capture_identity_changed", "capture_size_invalid",
    "invalid_executable", "invalid_devices", "invalid_input", "native_api_unavailable",
    "dpi_context_unavailable", "dpi_context_restore_failed", "desktop_unavailable",
    "desktop_enumeration_failed", "desktop_mismatch", "window_unavailable",
    "process_unavailable", "executable_mismatch", "invalid_identity",
    "process_identity_mismatch", "viewer_mismatch", "window_identity_mismatch",
    "invalid_geometry", "ambiguous_viewer", "background_connection_unavailable",
    "viewer_restore_unknown", "input_target_unavailable", "ambiguous_input_target",
    "point_outside_canvas", "invalid_coordinates", "coordinate_mapping_failed",
    "unsupported_key", "unsupported_action", "geometry_changed", "input_target_changed",
    "message_delivery_unknown", "native_delivery_failed", "window_capture_unsupported",
    "window_dpi_unavailable", "window_dpi_restore_failed", "window_geometry_unavailable",
    "window_dc_unavailable", "window_bitmap_unavailable", "window_geometry_unsupported",
    "window_capture_blank", "invalid_window_record", "window_geometry_changed",
})


class ToDeskSession:
    ACTIONS = frozenset({"status", "devices", "connect", "inspect", "screenshot",
                         "click", "type", "key", "scroll", "drag", "disconnect", "close"})
    INPUT_ACTIONS = frozenset({"click", "type", "key", "scroll", "drag"})
    MAX_RECEIPTS = 4096
    SCREENSHOT_MAX_AGE = 60.0
    MAX_TEXT_UNITS = 16384

    def __init__(self, data_root, executable, allowed_devices, native=None, capture=None):
        self.data_root = Path(data_root).resolve()
        self.executable = Path(executable).resolve()
        # Retain only names for this layer. Device IDs/credentials stay in the
        # native backend and are never serialized by this session.
        self._device_names = frozenset(
            name for name in allowed_devices
            if isinstance(name, str) and name and len(name) <= 200
            and all(char.isprintable() for char in name)
        ) if isinstance(allowed_devices, dict) else frozenset()
        self._allowed_devices = deepcopy(allowed_devices) if isinstance(allowed_devices, dict) else {}
        self._native = native
        self._capture = capture
        self._record = None
        self._device_name = None
        self._session_id = None
        self._last_capture = None
        self._fenced = False
        self._receipts = {}
        self._lock = threading.RLock()

    def _backend(self):
        if self._native is None:
            from .todesk_native import NativeToDesk
            self._native = NativeToDesk(str(self.executable), self._allowed_devices)
        return self._native

    @staticmethod
    def _base(action, **values):
        return {"ok": True, "mode": "todesk_window_control", "action": action,
                "host_input_used": False, "foreground_input_used": False,
                "remote_effect_verified": False, **values}

    @classmethod
    def _error(cls, action, code, *, may_have_run=False, **values):
        # All messages are local constants. Never format arbitrary input or an
        # exception into them (including failed device names and text).
        if code not in _MESSAGES and code not in _BACKEND_CODES:
            code = "native_failed"
        message = _MESSAGES.get(code, "The verified ToDesk backend could not complete this operation.")
        return {**cls._base(action), "ok": False, "error_code": code,
                "message": message, "action_may_have_run": bool(may_have_run), **values}

    @staticmethod
    def _exception_code(exc):
        code = getattr(exc, "code", "native_failed")
        return code if isinstance(code, str) and code in (_BACKEND_CODES | _MESSAGES.keys()) else "native_failed"

    def perform(self, action: str, args: dict | None = None) -> dict:
        with self._lock:
            safe_action = action if isinstance(action, str) and action in self.ACTIONS else "invalid"
            try:
                if safe_action == "invalid":
                    raise ToDeskSessionError("action_invalid")
                if args is not None and type(args) is not dict:
                    raise ToDeskSessionError("arguments_invalid")
                args = {} if args is None else args
                if action in self.INPUT_ACTIONS:
                    return self._input(action, args)
                if action == "connect":
                    if set(args) != {"device_name"} or not isinstance(args["device_name"], str):
                        raise ToDeskSessionError("device_name_required")
                    return self._connect(args["device_name"])
                if args:
                    raise ToDeskSessionError("arguments_invalid")
                if action in {"status", "inspect"}:
                    return self._status(action)
                if action == "devices":
                    return self._devices()
                if action == "screenshot":
                    return self._screenshot()
                return self._close(action)
            except Exception as exc:
                return self._error(safe_action, self._exception_code(exc),
                                   may_have_run=getattr(exc, "may_have_run", False) is True,
                                   **({"cleanup_complete": False} if safe_action in {"close", "disconnect"} else {}))

    def action(self, action, arguments=None):
        return self.perform(action, arguments)

    @staticmethod
    def _identity(record):
        if not isinstance(record, dict):
            raise ToDeskSessionError("viewer_identity_invalid")
        hwnd, pid, created = record.get("hwnd"), record.get("pid"), record.get("create_time")
        if type(hwnd) is not int or hwnd <= 0 or type(pid) is not int or pid <= 0:
            raise ToDeskSessionError("viewer_identity_invalid")
        if type(created) not in {int, float} or not 0 < created < 1e15:
            raise ToDeskSessionError("viewer_identity_invalid")
        if record.get("viewer") is not True:
            raise ToDeskSessionError("viewer_identity_invalid")
        return hwnd, pid, created

    @staticmethod
    def _geometry(record):
        width, height = record.get("client_width"), record.get("client_height")
        if type(width) is not int or type(height) is not int or not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise ToDeskSessionError("viewer_identity_invalid")
        return width, height

    def _verified_record(self):
        if self._record is None:
            raise ToDeskSessionError("session_not_attached")
        identity = self._identity(self._record)
        refreshed = self._backend().verify(deepcopy(self._record))
        if refreshed is True:
            refreshed = deepcopy(self._record)
        if self._identity(refreshed) != identity:
            raise ToDeskSessionError("viewer_identity_changed")
        self._geometry(refreshed)
        self._record = deepcopy(refreshed)
        return deepcopy(refreshed)

    def _connect(self, device_name):
        if device_name not in self._device_names:
            raise ToDeskSessionError("device_not_allowed")
        if self._record is not None:
            if device_name != self._device_name:
                raise ToDeskSessionError("session_already_attached")
            self._verified_record()
            return self._status("connect")
        # connect is the explicit authorization to attach/restore this viewer.
        # The backend binds device identity and never automates a main window.
        record = self._backend().connect(device_name)
        self._identity(record)
        self._geometry(record)
        refreshed = self._backend().verify(deepcopy(record))
        if refreshed is True:
            refreshed = record
        if self._identity(refreshed) != self._identity(record):
            raise ToDeskSessionError("viewer_identity_changed")
        self._geometry(refreshed)
        self._record = deepcopy(refreshed)
        self._device_name = device_name
        self._session_id = uuid.uuid4().hex
        self._last_capture = None
        self._fenced = False
        return self._status("connect")

    def _status(self, action="status"):
        verified = False
        code = None
        if self._record is not None:
            try:
                record = self._verified_record()
                verified = True
            except Exception as exc:
                record = self._record
                code = self._exception_code(exc)
        else:
            record = None
        fresh = bool(verified and self._last_capture and
                     time.monotonic() - self._last_capture["monotonic"] <= self.SCREENSHOT_MAX_AGE and
                     self._geometry(record) == self._last_capture["geometry"])
        result = self._base(action, attached=record is not None, verified=verified,
                            device_name=self._device_name, session_id=self._session_id,
                            screenshot_fresh=fresh, input_blocked=self._fenced,
                            verification_required=True,
                            supported_actions=((["screenshot", "disconnect"] +
                                                (["click", "type", "key", "scroll", "drag"]
                                                 if fresh and not self._fenced else [])) if verified else []))
        capabilities=getattr(self._native,'capabilities',None)
        if callable(capabilities):
            capability=capabilities()
            if isinstance(capability,dict) and capability.get('background_only') is True:
                result['background_only']=True
                result['background_input_verified']=capability.get('input_verified') is True
                result['automatic_connection_verified']=capability.get('auto_connect_verified') is True
                if not result['background_input_verified']:
                    result['supported_actions']=[x for x in result['supported_actions'] if x not in self.INPUT_ACTIONS]
        if record is not None:
            result["window"] = {key: record[key] for key in
                                ("hwnd", "pid", "create_time", "client_width", "client_height")}
            result["created_by_us"] = False
        if code:
            result["verification_error_code"] = code
        return result

    def _devices(self):
        inventory = self._backend().inventory()
        if not isinstance(inventory, list):
            raise ToDeskSessionError("native_failed")
        count = 0
        for record in inventory:
            try:
                self._identity(record)
                verified = self._backend().verify(deepcopy(record))
                if verified is True or self._identity(verified) == self._identity(record):
                    count += 1
            except Exception:
                continue
        return self._base("devices", devices=[{"device_name": name, "configured": True}
                                               for name in sorted(self._device_names)],
                          verified_viewer_count=count,
                          connection_mode="attach_verified_viewer", automatic_connection_supported=False)

    def _screenshot(self):
        record = self._verified_record()
        if self._capture is None:
            from .todesk_capture import capture_todesk_window
            self._capture = capture_todesk_window
        directory = self.data_root / "background_desktop" / "captures"
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(self.data_root):
            raise ToDeskSessionError("capture_invalid")
        path = directory / ("todesk-" + uuid.uuid4().hex + ".png")
        self._last_capture = None
        try:
            metadata = self._capture(deepcopy(record), path, verifier=self._backend().verify)
            capture = self._capture_metadata(metadata, path, record)
            current = self._verified_record()
            if self._geometry(current) != self._geometry(record):
                raise ToDeskSessionError("viewer_geometry_changed")
        except Exception:
            # No unverified output file is returned to the client.
            path.unlink(missing_ok=True)
            raise
        capture_id = uuid.uuid4().hex
        self._last_capture = {"monotonic": time.monotonic(), "geometry": self._geometry(current),
                              "identity": self._identity(current), "capture": capture,
                              "capture_id": capture_id}
        return self._base("screenshot", device_name=self._device_name, session_id=self._session_id,
                          capture_id=capture_id, capture=capture,
                          captured_at=datetime.now(timezone.utc).isoformat(),
                          coordinate_space="screenshot_pixels", input_blocked=self._fenced,
                          verification_required=True)

    def _capture_metadata(self, metadata, path, record):
        if not isinstance(metadata, dict) or Path(metadata.get("path", "")).resolve() != path.resolve():
            raise ToDeskSessionError("capture_invalid")
        width, height = metadata.get("width"), metadata.get("height")
        origin = metadata.get("client_origin")
        if (type(width) is not int or type(height) is not int or not 1 <= width <= 8192 or
                not 1 <= height <= 8192 or width * height > 32_000_000 or
                not isinstance(origin, dict) or set(origin) != {"x", "y"} or
                type(origin["x"]) is not int or type(origin["y"]) is not int):
            raise ToDeskSessionError("capture_invalid")
        cw, ch = self._geometry(record)
        ox, oy = origin["x"], origin["y"]
        if (metadata.get("client_width") != cw or metadata.get("client_height") != ch or
                ox < 0 or oy < 0 or ox + cw > width or oy + ch > height):
            raise ToDeskSessionError("capture_invalid")
        size = path.stat().st_size
        if not 24 <= size <= 128_000_000:
            raise ToDeskSessionError("capture_invalid")
        with path.open("rb") as stream:
            header = stream.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR" or struct.unpack(">II", header[16:24]) != (width, height):
            raise ToDeskSessionError("capture_invalid")
        bounds = {"left": ox, "top": oy, "right": ox + cw, "bottom": oy + ch}
        result = {"path": str(path), "mime_type": "image/png", "width": width, "height": height,
                  "size_bytes": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "client_origin": {"x": ox, "y": oy}, "client_width": cw, "client_height": ch,
                  "client_bounds": bounds, "content_bounds": bounds.copy(),
                  "content_scope": "client_area", "source_hwnd": record["hwnd"],
                  "capture_method": "PrintWindow(PW_RENDERFULLCONTENT)",
                  "capture_scope": "todesk_window", "host_input_used": False}
        outer = metadata.get("outer_bounds")
        if isinstance(outer, dict) and set(outer) == {"left", "top", "right", "bottom"} and all(type(v) is int for v in outer.values()):
            result["outer_bounds"] = dict(outer)
        return result

    @staticmethod
    def _integer(value, low, high):
        return type(value) is int and low <= value <= high

    def _validated_input(self, action, args):
        op = args.get("operation_id")
        if not isinstance(op, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", op) is None:
            raise ToDeskSessionError("operation_id_required")
        payload = {key: value for key, value in args.items() if key != "operation_id"}
        coordinate = lambda value: self._integer(value, 0, 8191)
        if action == "click":
            valid = (set(payload) <= {"x", "y", "button", "count"} and {"x", "y"} <= set(payload)
                     and coordinate(payload["x"]) and coordinate(payload["y"]))
            payload.setdefault("button", "left")
            payload.setdefault("count", 1)
            valid = valid and payload["button"] in ("left", "right", "middle") and self._integer(payload["count"], 1, 2)
        elif action == "type":
            valid = set(payload) == {"text"} and isinstance(payload.get("text"), str)
            if valid:
                text = payload["text"]
                try:
                    units = len(text.encode("utf-16-le")) // 2
                except UnicodeError:
                    units = self.MAX_TEXT_UNITS + 1
                valid = 1 <= units <= self.MAX_TEXT_UNITS and not any(ord(c) < 32 and c not in "\r\n\t" for c in text)
        elif action == "key":
            valid = set(payload) == {"keys"} and isinstance(payload.get("keys"), str) and len(payload["keys"]) <= 64
            key = payload.get("keys", "")
            aliases = {"ESC": "ESCAPE", "RETURN": "ENTER", "DEL": "DELETE", "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "BACK": "BACKSPACE"}
            key = aliases.get(key.upper(), key.upper()) if isinstance(key, str) else ""
            if valid and ("+" in key or key in {"CTRL", "CONTROL", "ALT", "SHIFT", "WIN", "LWIN", "RWIN"}):
                raise ToDeskSessionError("modifier_chord_unsupported")
            named = {"ENTER", "TAB", "ESCAPE", "SPACE", "BACKSPACE", "DELETE", "INSERT", "HOME", "END",
                     "LEFT", "RIGHT", "UP", "DOWN", "PAGEUP", "PAGEDOWN"}
            valid = valid and (key in named or re.fullmatch(r"[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])", key) is not None)
            payload["keys"] = key
        elif action == "scroll":
            valid = (set(payload) <= {"x", "y", "vertical", "horizontal"} and {"x", "y"} <= set(payload)
                     and coordinate(payload["x"]) and coordinate(payload["y"]))
            payload.setdefault("vertical", 0)
            payload.setdefault("horizontal", 0)
            valid = valid and all(self._integer(payload[k], -12000, 12000) and payload[k] % 120 == 0 for k in ("vertical", "horizontal"))
            valid = valid and bool(payload["vertical"] or payload["horizontal"])
        else:
            valid = (set(payload) <= {"x", "y", "to_x", "to_y", "duration_ms"} and {"x", "y", "to_x", "to_y"} <= set(payload)
                     and all(coordinate(payload[k]) for k in ("x", "y", "to_x", "to_y")))
            payload.setdefault("duration_ms", 250)
            valid = valid and self._integer(payload["duration_ms"], 1, 5000)
        if not valid:
            raise ToDeskSessionError("arguments_invalid")
        return op, payload

    def _input(self, action, args):
        operation_id, payload = self._validated_input(action, args)
        digest = hashlib.sha256(json.dumps([action, payload], sort_keys=True, ensure_ascii=True,
                                           separators=(",", ":")).encode("ascii")).hexdigest()
        cached = self._receipts.get(operation_id)
        if cached is not None:
            if cached[0] != digest:
                return self._error(action, "operation_id_conflict", operation_id=operation_id)
            return deepcopy(cached[1])
        if len(self._receipts) >= self.MAX_RECEIPTS:
            return self._error(action, "operation_cache_full", operation_id=operation_id)
        dispatched = False
        try:
            if self._fenced:
                raise ToDeskSessionError("input_fenced")
            record = self._verified_record()
            snapshot = self._last_capture
            if snapshot is None:
                raise ToDeskSessionError("screenshot_required")
            if time.monotonic() - snapshot["monotonic"] > self.SCREENSHOT_MAX_AGE:
                raise ToDeskSessionError("screenshot_stale")
            if self._identity(record) != snapshot["identity"] or self._geometry(record) != snapshot["geometry"]:
                raise ToDeskSessionError("viewer_geometry_changed")
            converted = deepcopy(payload)
            cap = snapshot["capture"]
            for xkey, ykey in (("x", "y"), ("to_x", "to_y")):
                if xkey not in converted:
                    continue
                x = converted[xkey] - cap["client_origin"]["x"]
                y = converted[ykey] - cap["client_origin"]["y"]
                if not 0 <= x < cap["client_width"] or not 0 <= y < cap["client_height"]:
                    raise ToDeskSessionError("coordinate_outside_client")
                converted[xkey], converted[ykey] = x, y
            dispatched = True
            receipt = self._backend().perform(deepcopy(record), action, converted)
            if not isinstance(receipt, dict) or receipt.get("ok") is not True:
                known_safe = (isinstance(receipt, dict) and receipt.get("status") != "delivery_unknown"
                              and receipt.get("delivery_unknown") is not True
                              and receipt.get("action_may_have_run") is not True
                              and receipt.get("may_have_run") is not True
                              and (receipt.get("action_may_have_run") is False or receipt.get("may_have_run") is False))
                raise ToDeskSessionError("input_rejected" if known_safe else "input_delivery_unknown", may_have_run=not known_safe)
            if receipt.get("delivery_unknown") is True or receipt.get("retry_safe") is False:
                raise ToDeskSessionError("input_delivery_unknown", may_have_run=True)
            result = self._base(action, device_name=self._device_name, session_id=self._session_id,
                                operation_id=operation_id, dispatched=True,
                                verification_required=True, action_may_have_run=True)
        except Exception as exc:
            may_have_run = (getattr(exc, "may_have_run", None) is True or
                            (dispatched and getattr(exc, "may_have_run", None) is not False))
            if may_have_run:
                self._fenced = True
            code = "input_delivery_unknown" if may_have_run else self._exception_code(exc)
            result = self._error(action, code, may_have_run=may_have_run,
                                 device_name=self._device_name, session_id=self._session_id,
                                 operation_id=operation_id, input_blocked=self._fenced,
                                 verification_required=True)
        self._receipts[operation_id] = (digest, deepcopy(result))
        return result

    def _close(self, action):
        if self._record is None:
            return self._base(action, cleanup_complete=True, closed=True)
        # The native close backend must prove disappearance of the original
        # exact identity. Failed verification or missing inventory is not proof.
        receipt = self._backend().perform(deepcopy(self._record), "close", {})
        if not isinstance(receipt, dict) or receipt.get("closed") is not True:
            return self._error(action, "cleanup_unconfirmed", cleanup_complete=False,
                               attached=True, device_name=self._device_name, session_id=self._session_id)
        self._record = None
        self._device_name = None
        self._session_id = None
        self._last_capture = None
        self._fenced = False
        return self._base(action, cleanup_complete=True, closed=True)
