"""Prefer the existing UU channel; bind each session to one transport.

No configuration is changed and no local input API is imported here. A proven,
pre-input availability failure can select the receiver. The optional connect
acceptance_test=True is reserved for receiver acceptance with explicit user
consent checked by the authenticated caller; it never changes default priority
or replaces an active or uncertain route.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any
import uuid


_INPUT = frozenset({"type", "key", "click", "scroll", "drag"})
_WRITE = _INPUT | {"connect", "close", "enroll", "setup"}
_DIRECT = frozenset({"devices", "enroll", "setup", "status"})
_SESSION = frozenset({"session_status", "screenshot", "command_status", "close"}) | _INPUT
_AVAILABLE_FAILURES = frozenset({
    "uu_not_configured", "background_desktop_not_configured", "uu_device_offline",
    "uu_cli_unavailable", "uu_cli_worker_unavailable", "uu_login_required", "uu_login_pending",
    "uu_graphics_proxy_unavailable", "uu_startup_timeout", "uu_connect_timeout", "uu_operation_timeout",
})
_ENDPOINT_TRANSPORT_FAILURES = frozenset({
    "uu_terminal_management_failed", "uu_terminal_management_rejected", "uu_remote_input_timeout",
    "uu_terminal_exited", "uu_terminal_read_failed", "uu_terminal_poll_failed",
})
_ID = re.compile(r"[a-f0-9]{32}\Z")
_OP = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,119}\Z")
_MAX_ROUTES = 64
_MAX_OPERATIONS = 256
_SECRET_KEYS = frozenset({"text", "keys", "arguments", "command", "stdout", "stderr", "device_id"})


def _copy(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {key: _copy(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and key.lower() not in _SECRET_KEYS
                and not any(word in key.lower() for word in ("token", "password", "secret", "credential"))}
    if isinstance(value, (list, tuple)):
        return [_copy(item, depth + 1) for item in value[:1024]]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    return None


def _base(action: str, status: str, *, transport: str | None = None, terminal: bool = True, **extra) -> dict:
    return {"ok": status not in {"failed", "error", "outcome_unknown", "fenced", "not_ready"},
            "action": action, "status": status, "state": status, "terminal": terminal,
            "transport": transport, "preferred_transport": "uu", "background_only": True,
            "host_input_used": False, "foreground_input_used": False,
            "remote_effect_verified": False, **extra}


def _failure(action: str, code: str, transport: str | None = None, **extra) -> dict:
    return _base(action, "error", transport=transport, error_code=code, **extra)


def _digest(action: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"action": action, "arguments": args}, sort_keys=True,
                                    ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _Route:
    session_id: str
    device_name: str
    transport: str
    state: str
    connect_result: dict
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operations: OrderedDict = field(default_factory=OrderedDict)
    uncertain: bool = False


class PreferredRemoteController:
    def __init__(self, uu_call, receiver_controller, aliases: dict):
        if not callable(uu_call) or not isinstance(aliases, dict):
            raise ValueError("a UU callable and device aliases are required")
        self.uu_call = uu_call
        self.receiver_controller = receiver_controller
        self.aliases = {}
        for name, value in aliases.items():
            if (not isinstance(name, str) or not name or len(name) > 200 or not isinstance(value, dict)
                    or set(value) != {"uu_device_name", "receiver_device_name"}
                    or any(not isinstance(item, str) or not item or len(item) > 200
                           or any(ord(char) < 32 for char in item) for item in value.values())):
                raise ValueError("remote device aliases are invalid")
            self.aliases[name] = dict(value)
        self._routes: OrderedDict[str, _Route] = OrderedDict()
        self._connect_operations: OrderedDict[tuple[str, str], tuple[str, str | None, dict]] = OrderedDict()
        self._connect_lock = asyncio.Lock()

    async def perform(self, action: str, args: dict | None, confirm: bool = False) -> dict:
        if not isinstance(action, str) or action not in _DIRECT | _SESSION | {"connect"}:
            return _failure("invalid", "unsupported_remote_action")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _failure(action, "invalid_arguments")
        if action in _WRITE and confirm is not True:
            return _failure(action, "confirmation_required")
        try:
            if action == "connect":
                return await self._connect(args, confirm)
            if action in _DIRECT:
                return await self._receiver(action, args, confirm)
            session_id = args.get("session_id")
            if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
                return _failure(action, "invalid_session_id")
            route = self._routes.get(session_id)
            if route is None:
                return _failure(action, "remote_route_not_found")
            async with route.lock:
                if route.state == "closed" and action != "session_status":
                    if action == "close":
                        return _base(action, "closed", transport=route.transport, session_id=session_id, session_state="closed")
                    return _failure(action, "remote_session_closed", route.transport, session_id=session_id)
                if route.transport == "receiver":
                    result = await self._receiver(action, args, confirm)
                    if route.connect_result.get("explicit_receiver_acceptance") is True:
                        result["explicit_receiver_acceptance"] = True
                    state = result.get("session_state", result.get("status"))
                    if action == "close" and result.get("ok") is True:
                        route.state = "closed" if state == "closed" else "closing"
                    elif state in {"ready", "closed", "fenced", "failed"}:
                        route.state = state
                    return result
                return await self._uu_session(route, action, args, confirm)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return _failure(action, "invalid_arguments")
        except Exception:
            # An exception supplies no proof that an operation did not run.
            return _failure(action, "remote_route_failed")

    async def _receiver(self, action: str, args: dict, confirm: bool) -> dict:
        if self.receiver_controller is None:
            return _failure(action, "remote_receiver_not_configured", "receiver")
        result = await self.receiver_controller.perform(action, args, confirm=confirm)
        if not isinstance(result, dict):
            return _failure(action, "remote_receiver_response_invalid", "receiver")
        enrollment_token = result.get("enrollment_token") if action == "enroll" else None
        result = _copy(result)
        if isinstance(enrollment_token, str):
            # The confirmed enrollment action is the one intentional, single-
            # response credential handoff to the private installer.
            result["enrollment_token"] = enrollment_token
        result.update(transport="receiver", preferred_transport="uu")
        result.setdefault("state", result.get("status", "unknown"))
        return result

    @staticmethod
    def _unpack(response) -> tuple[dict, bool, str | None, bool | None]:
        if not isinstance(response, dict):
            return {}, False, "uu_response_invalid", None
        wrapped = response.get("result")
        data = wrapped.get("data") if isinstance(wrapped, dict) else None
        if not isinstance(data, dict):
            data = response.get("data")
        if not isinstance(data, dict):
            data = {}
        ok = response.get("ok") is True and data.get("ok") is True
        code = data.get("error_code") or response.get("error_code")
        if not code and isinstance(response.get("error"), dict):
            code = response["error"].get("code")
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else (None if ok else "uu_connection_failed")
        values = [item.get("action_may_have_run") for item in (data, response.get("evidence"), response)
                  if isinstance(item, dict)]
        may_have_run = True if any(value is True for value in values) else (False if any(value is False for value in values) else None)
        return data, ok, code, may_have_run

    @staticmethod
    def _can_fallback(data: dict, code: str | None, may_have_run: bool | None) -> bool:
        if may_have_run is not False:
            return False
        if code in _AVAILABLE_FAILURES:
            return True
        # In SharedUUGuiSession this timeout can also follow helper startup;
        # only its initial, read-only connect checks prove availability loss.
        return code == "uu_cli_timeout" and data.get("phase") == "connect"

    @staticmethod
    def _startup_cleanup_required(data: dict, code: str | None, may_have_run: bool | None) -> bool:
        stage = data.get("stage")
        if (may_have_run is not False or data.get("phase") != "remote_endpoint_start"
                or not isinstance(stage, str)):
            return False
        bootstrap_stage = re.fullmatch(r"(?:bootstrap_reader|bootstrap_chunk_[0-9]{1,3}|verify|helper_ready)", stage) is not None
        if code == "uu_bootstrap_integrity_failed":
            return bootstrap_stage
        return code in _ENDPOINT_TRANSPORT_FAILURES and (bootstrap_stage
                or stage in {"term_create", "orphan_cleanup", "probe", "shell_prompt"})

    def _space(self) -> bool:
        if len(self._routes) < _MAX_ROUTES:
            return True
        for session_id, route in list(self._routes.items()):
            if route.state == "closed":
                del self._routes[session_id]
                for key, item in list(self._connect_operations.items()):
                    if item[1] == session_id:
                        del self._connect_operations[key]
                return True
        return False

    def _remember_connect(self, key, digest, session_id, result):
        self._connect_operations[key] = (digest, session_id, _copy(result))
        while len(self._connect_operations) > 128:
            self._connect_operations.popitem(last=False)

    def _existing(self, route: _Route) -> dict:
        marker = ({"explicit_receiver_acceptance": True}
                  if route.connect_result.get("explicit_receiver_acceptance") is True else {})
        if route.state == "fenced":
            return _failure("connect", "remote_outcome_unknown", route.transport, session_id=route.session_id,
                            session_state="fenced", action_may_have_run=True, **marker)
        if route.state == "ready":
            return _base("connect", "ready", transport=route.transport, terminal=False,
                         session_id=route.session_id, session_state="ready", device_name=route.device_name, **marker)
        result = _copy(route.connect_result)
        result.update(session_state=route.state)
        if route.state == "closed":
            result.update(ok=False, status="closed", state="closed", terminal=True)
        return result

    async def _connect(self, args: dict, confirm: bool) -> dict:
        if (not {"device_name", "operation_id"} <= set(args)
                or set(args) - {"device_name", "operation_id", "acceptance_test"}
                or "acceptance_test" in args and args["acceptance_test"] is not True):
            return _failure("connect", "invalid_arguments")
        acceptance = args.get("acceptance_test") is True
        name, operation_id = args["device_name"], args["operation_id"]
        if (not isinstance(name, str) or name not in self.aliases or not isinstance(operation_id, str)
                or not _OP.fullmatch(operation_id)):
            return _failure("connect", "remote_device_alias_required")
        key, digest = (name, operation_id), _digest("connect", {"device_name": name, "acceptance_test": acceptance})
        async with self._connect_lock:
            previous = self._connect_operations.get(key)
            if previous:
                if previous[0] != digest:
                    return _failure("connect", "operation_id_conflict")
                route = self._routes.get(previous[1]) if previous[1] else None
                return self._existing(route) if route else _copy(previous[2])
            if acceptance and any(route.state != "closed" and
                    (route.transport == "uu" or route.uncertain or route.state == "fenced")
                    for route in self._routes.values()):
                return _failure("connect", "remote_route_busy")
            for route in self._routes.values():
                if route.state != "closed" and route.device_name == name:
                    if acceptance:
                        return _failure("connect", "remote_route_busy", route.transport,
                                        session_id=route.session_id)
                    result = self._existing(route)
                    self._remember_connect(key, digest, route.session_id, result)
                    return result
            if any(route.transport == "uu" and route.state != "closed" for route in self._routes.values()):
                return _failure("connect", "uu_route_busy", "uu")
            if not self._space():
                return _failure("connect", "remote_route_limit")
            alias = self.aliases[name]
            if acceptance:
                return await self._connect_receiver(name, operation_id, key, digest, confirm,
                                                    acceptance=True)
            try:
                response = await self.uu_call("connect", {"device_name": alias["uu_device_name"]}, confirm)
            except Exception:
                result = _failure("connect", "uu_transport_failure", "uu", action_may_have_run=None)
                self._remember_connect(key, digest, None, result)
                return result
            data, ok, code, may_have_run = self._unpack(response)
            if ok and data.get("connected") is True and data.get("keyboard_ready") is True:
                session_id = uuid.uuid4().hex
                result = {**_copy(data), **_base("connect", "ready", transport="uu", terminal=False,
                           session_id=session_id, session_state="ready", device_name=name)}
                self._routes[session_id] = _Route(session_id, name, "uu", "ready", _copy(result))
                self._remember_connect(key, digest, session_id, result)
                return result
            if not ok and self._startup_cleanup_required(data, code, may_have_run):
                try:
                    cleanup = await self.uu_call("close", {}, True)
                    cleanup_data, cleanup_ok, cleanup_code, cleanup_unknown = self._unpack(cleanup)
                except Exception:
                    cleanup_data, cleanup_ok, cleanup_code, cleanup_unknown = {}, False, "uu_cleanup_transport_failure", None
                if (cleanup_ok and cleanup_data.get("cleanup_complete") is True
                        and cleanup_code is None and cleanup_unknown is not True):
                    return await self._connect_receiver(name, operation_id, key, digest, confirm,
                                                        unavailable_code=code, cleanup_confirmed=True)
                result = {**_copy(data), **_failure("connect", "uu_fallback_cleanup_unconfirmed", "uu",
                           uu_unavailable_code=code, cleanup_complete=False,
                           cleanup_error_code=cleanup_code, action_may_have_run=cleanup_unknown)}
                self._remember_connect(key, digest, None, result)
                return result
            if not ok and self._can_fallback(data, code, may_have_run):
                return await self._connect_receiver(name, operation_id, key, digest, confirm,
                                                    unavailable_code=code)
            result = {**_copy(data), **_failure("connect", code or "uu_not_ready", "uu",
                       action_may_have_run=may_have_run)}
            self._remember_connect(key, digest, None, result)
            return result

    async def _connect_receiver(self, name: str, operation_id: str, key, digest: str,
                                confirm: bool, *, acceptance: bool = False,
                                unavailable_code: str | None = None,
                                cleanup_confirmed: bool = False) -> dict:
        result = await self._receiver("connect", {"device_name": self.aliases[name]["receiver_device_name"],
                                                  "operation_id": operation_id}, confirm)
        if acceptance:
            result["explicit_receiver_acceptance"] = True
        if unavailable_code is not None:
            result["uu_unavailable_code"] = unavailable_code
        if cleanup_confirmed:
            result["uu_cleanup_confirmed"] = True
        session_id = result.get("session_id")
        if isinstance(session_id, str) and _ID.fullmatch(session_id) and result.get("ok") is True:
            if session_id in self._routes:
                return _failure("connect", "remote_route_identity_conflict", "receiver")
            state = result.get("session_state", result.get("status", "starting"))
            self._routes[session_id] = _Route(session_id, name, "receiver", state, _copy(result))
            self._remember_connect(key, digest, session_id, result)
        else:
            self._remember_connect(key, digest, None, result)
        return result

    async def _uu_session(self, route: _Route, action: str, args: dict, confirm: bool) -> dict:
        if action == "command_status":
            return _failure(action, "uu_command_status_not_supported", "uu", session_id=route.session_id)
        if action == "session_status" and route.state == "closed":
            return _base(action, "closed", transport="uu", session_id=route.session_id, session_state="closed")
        payload = {key: value for key, value in args.items() if key != "session_id"}
        if "args" in payload:
            if set(payload) - {"args", "operation_id"} or not isinstance(payload["args"], dict):
                return _failure(action, "invalid_arguments", "uu", session_id=route.session_id)
            payload = {**payload["args"], **({"operation_id": payload["operation_id"]} if "operation_id" in payload else {})}
        operation_id, digest = payload.get("operation_id"), None
        if action in _INPUT:
            if (not isinstance(operation_id, str) or not _OP.fullmatch(operation_id) or len(operation_id) > 64):
                return _failure(action, "invalid_operation_id", "uu", session_id=route.session_id)
            digest = _digest(action, {key: value for key, value in payload.items() if key != "operation_id"})
            previous = route.operations.get(operation_id)
            if previous:
                return _copy(previous[1]) if previous[0] == digest else _failure(action, "operation_id_conflict", "uu", session_id=route.session_id)
            if route.uncertain:
                return _failure(action, "remote_outcome_unknown", "uu", session_id=route.session_id, action_may_have_run=True)
            if len(route.operations) >= _MAX_OPERATIONS:
                return _failure(action, "remote_operation_limit", "uu", session_id=route.session_id)
        else:
            allowed = {"operation_id"} if action in {"screenshot", "close"} else set()
            if set(payload) - allowed:
                return _failure(action, "invalid_arguments", "uu", session_id=route.session_id)
            payload = {}
        subaction = "status" if action == "session_status" else action
        try:
            response = await self.uu_call(subaction, payload, confirm)
            data, ok, code, may_have_run = self._unpack(response)
        except Exception:
            data, ok, code, may_have_run = {}, False, "uu_transport_failure", None
        if ok:
            state = "completed"
            terminal = True
            if action == "session_status":
                state, terminal = ("ready" if data.get("connected") is True and data.get("keyboard_ready") is True else "not_ready"), False
                if route.uncertain:
                    state = "fenced"
            elif action == "close":
                if data.get("cleanup_complete") is True:
                    route.state = state = "closed"
                else:
                    ok, state = False, "failed"
            result = {**_copy(data), **_base(action, state, transport="uu", terminal=terminal,
                       session_id=route.session_id, session_state=route.state)}
            if not ok:
                result.update(ok=False, error_code="uu_cleanup_unconfirmed")
        else:
            unknown = action in _INPUT and may_have_run is not False
            route.uncertain = route.uncertain or unknown
            if unknown:
                route.state = "fenced"
            state = "outcome_unknown" if unknown else "failed"
            result = {**_copy(data), **_base(action, state, transport="uu", session_id=route.session_id,
                       session_state=route.state, error_code=code or "uu_operation_failed", action_may_have_run=may_have_run)}
        if action in _INPUT:
            result.update(verification_required=True, delivery_only=ok)
            route.operations[operation_id] = (digest, _copy(result))
        return result
