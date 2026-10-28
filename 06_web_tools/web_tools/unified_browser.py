"""Explicit browser routing, bounded coordination and durable write receipts.

Only fixed browser actions cross this facade. It neither starts a browser nor
evaluates caller-provided JavaScript. Page contents never grant authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
import uuid

WIRE = {"status": None, "list_tabs": "list_tabs", "open_tab": "open_tab", "close_tab": "close_tab",
        "activate_tab": "activate_tab", "navigate": "navigate", "read": "read_page", "click": "click",
        "type": "type_text", "press": "press_key", "select": "select_option", "scroll": "scroll",
        "wait": "wait", "screenshot": "screenshot", "downloads": "list_downloads",
        "debug_status": "debug_status", "debug_attach": "debug_attach", "debug_snapshot": "debug_snapshot",
        "debug_screenshot": "debug_screenshot", "debug_detach": "debug_detach", "reconcile": None, "task_status": "task_status"}
WRITE_ACTIONS = {"open_tab", "close_tab", "activate_tab", "navigate", "click", "type", "press", "select", "scroll", "debug_attach", "debug_detach"}
NO_TAB = {"status", "list_tabs", "open_tab", "downloads", "task_status"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}$")
COMMON = {"expected_generation"}
FIELDS = {
    "task_status": {"task_id"}, "status": {"operation_id"}, "list_tabs": set(), "open_tab": {"url", "engine"}, "close_tab": set(), "activate_tab": set(),
    "navigate": {"url", "engine"}, "read": {"max_chars", "settle_ms", "include_frames", "selector", "mode", "frame_id", "shadow_path"},
    "click": {"by", "selector", "name", "exact", "frame_id", "shadow_path"},
    "type": {"by", "selector", "name", "exact", "frame_id", "shadow_path", "text", "clear"},
    "press": {"by", "selector", "name", "exact", "frame_id", "shadow_path", "key"},
    "select": {"by", "selector", "name", "exact", "frame_id", "shadow_path", "option", "option_by"},
    "scroll": {"selector", "frame_id", "direction", "pixels", "x", "y"},
    "wait": {"milliseconds"}, "screenshot": {"full_page", "mode"}, "downloads": {"limit"},
    "debug_status": set(), "debug_attach": set(), "debug_detach": set(),
    "debug_snapshot": {"max_chars"}, "debug_screenshot": set(),
    "reconcile": {"operation_id", "observation_id", "decision", "evidence"},
}


class BrowserControlError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(message)


def result(browser_id: str, tab_id: str | None, action: str, *, status: str = "completed", data: Any = None,
           code: str | None = None, message: str | None = None, retryable: bool = False,
           executed: bool | None = False, verified: bool = False, **extra: Any) -> dict[str, Any]:
    return {"ok": status == "completed", "status": status, "browser_id": browser_id, "tab_id": tab_id,
            "action": action, "result": data, "error": {"code": code, "message": message} if code else None,
            "retryable": retryable, "evidence": {"executed": executed, "verified": verified}, **extra}


def _receipt_data(value: Any, depth: int = 0) -> Any:
    """Keep bounded diagnostic state, never raw DOM, screenshots or credentials."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        denied = {"data_url", "text", "html", "body", "content", "cookies", "cookie", "password", "token", "authorization", "data_base64"}
        return {str(k): _receipt_data(v, depth + 1) for k, v in list(value.items())[:100] if str(k).lower() not in denied}
    if isinstance(value, list):
        return [_receipt_data(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    return value if value is None or isinstance(value, (int, bool, float)) else None


class BrowserReceipts:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS unified_browser_operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, browser_id TEXT NOT NULL, tab_id TEXT, generation TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, updated REAL NOT NULL)")

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=.05)
        db.row_factory = sqlite3.Row
        return db

    def get(self, operation_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM unified_browser_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return dict(row) if row else None

    def pending(self, resource: str):
        with self.connect() as db:
            row = db.execute("SELECT operation_id FROM unified_browser_operations WHERE resource=? AND state IN ('dispatching','outcome_unknown','dispatched_unverified') LIMIT 1", (resource,)).fetchone()
        return row["operation_id"] if row else None

    def reserve(self, operation_id: str, fingerprint: str, browser_id: str, tab_id: str | None,
                generation: str, action: str, resource: str, url: str, pending: dict[str, Any]):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT operation_id FROM unified_browser_operations WHERE operation_id=?", (operation_id,)).fetchone()
            fence = db.execute("SELECT operation_id FROM unified_browser_operations WHERE resource=? AND state IN ('dispatching','outcome_unknown','dispatched_unverified') LIMIT 1", (resource,)).fetchone()
            if previous or fence:
                raise BrowserControlError("browser_target_fenced", "Another operation already owns this target; read status and inspect the page before reconciling", False)
            db.execute("INSERT INTO unified_browser_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (operation_id, fingerprint, browser_id, tab_id, generation, action, resource, url, "dispatching", json.dumps(pending, ensure_ascii=False), time.time()))

    def save(self, operation_id: str, value: dict[str, Any]):
        with self.connect() as db:
            db.execute("UPDATE unified_browser_operations SET state=?,result=?,updated=? WHERE operation_id=?",
                       (value["status"], json.dumps(_receipt_data(value), ensure_ascii=False), time.time(), operation_id))

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(row["result"])
        if row["state"] == "dispatching":
            value.update(ok=False, status="outcome_unknown", retryable=False,
                         error={"code": "browser_outcome_unknown", "message": "Operation is running or was interrupted; inspect status and page before any retry"})
            value["evidence"] = {"executed": None, "verified": False}
        return value


class UnifiedBrowserService:
    def __init__(self, database: Path, discover: Callable[[], Awaitable[dict[str, Any]]],
                 dispatch: Callable[..., Awaitable[Any]],
                 desktop_check: Callable[[], Awaitable[None]] | None = None):
        self.receipts = BrowserReceipts(database)
        self.discover = discover
        self.dispatch = dispatch
        self.desktop_check = desktop_check
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._observations: dict[str, dict[str, Any]] = {}

    def target_lock(self, browser_id: str, tab_id: str | None):
        key = (browser_id, tab_id or "$browser")
        if key not in self._locks:
            if len(self._locks) >= 2048:
                raise BrowserControlError("browser_coordination_limit", "Too many tracked targets; retain receipts and restart the bridge when idle")
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def resource(browser_id: str, tab_id: str | None):
        return hashlib.sha256(json.dumps([browser_id, tab_id or "$browser"]).encode()).hexdigest()

    def _observe(self, browser_id: str, tab_id: str, generation: str, url: str):
        now = time.monotonic()
        self._observations = {key: item for key, item in self._observations.items() if now - item["at"] < 300}
        if len(self._observations) >= 512:
            self._observations.pop(next(iter(self._observations)))
        key = "observation_" + uuid.uuid4().hex
        self._observations[key] = {"browser_id": browser_id, "tab_id": tab_id, "generation": generation, "url": url, "at": now}
        return key

    @staticmethod
    def _validate(action: str, arguments: dict[str, Any]):
        if action not in WIRE:
            raise BrowserControlError("browser_action_unsupported", "Action is outside the fixed browser allowlist")
        extras = set(arguments) - FIELDS[action] - COMMON
        if extras:
            raise BrowserControlError("browser_arguments_invalid", "Unsupported arguments for this action; arbitrary scripts and protocol commands are unavailable")
        for key in {"selector", "name", "key", "option", "option_by", "by", "mode", "expected_generation"} & arguments.keys():
            if not isinstance(arguments[key], str) or len(arguments[key]) > 2000:
                raise BrowserControlError("browser_arguments_invalid", "Locator and control arguments must be bounded strings")
        if "text" in arguments and (not isinstance(arguments["text"], str) or len(arguments["text"]) > 100000):
            raise BrowserControlError("browser_arguments_invalid", "Text must be a bounded string")
        if "url" in arguments:
            try:
                url = urlsplit(arguments["url"])
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or len(arguments["url"]) > 4000:
                    raise ValueError()
            except (ValueError, TypeError):
                raise BrowserControlError("browser_url_denied", "Only credential-free HTTP(S) URLs are allowed")
        required = {"navigate": "url", "type": "text", "press": "key", "select": "option"}.get(action)
        if required and required not in arguments:
            raise BrowserControlError("browser_arguments_invalid", "Required action argument is missing")
        if "engine" in arguments and arguments["engine"] not in {"chromium", "cef", "auto", "lightpanda"}:
            raise BrowserControlError("browser_arguments_invalid", "Select a supported native browser engine explicitly")
        if action == "screenshot" and arguments.get("mode", "page") not in {"page", "window"}:
            raise BrowserControlError("browser_arguments_invalid", "Screenshot mode must be page or window")
        if action == "task_status" and (not isinstance(arguments.get("task_id"), str) or not 1 <= len(arguments["task_id"]) <= 256):
            raise BrowserControlError("browser_arguments_invalid", "A bounded native task ID is required")
        for key, lower, upper in (("frame_id", 0, 2147483647), ("max_chars", 100, 100000), ("settle_ms", 0, 5000), ("milliseconds", 0, 120000), ("limit", 1, 100), ("pixels", -100000, 100000), ("x", 0, 100000), ("y", 0, 100000)):
            if key in arguments and (type(arguments[key]) is not int or not lower <= arguments[key] <= upper):
                raise BrowserControlError("browser_arguments_invalid", "Numeric action argument is outside its bounded range")
        for key in ("exact", "clear", "include_frames", "full_page"):
            if key in arguments and type(arguments[key]) is not bool:
                raise BrowserControlError("browser_arguments_invalid", "Boolean action argument must be a real boolean")
        if "shadow_path" in arguments and (not isinstance(arguments["shadow_path"], list) or len(arguments["shadow_path"]) > 8 or any(not isinstance(item, str) or len(item) > 1000 for item in arguments["shadow_path"])):
            raise BrowserControlError("browser_arguments_invalid", "Shadow path must contain up to eight bounded selectors")

    async def instances(self, timeout: int = 10000):
        if type(timeout) is not int or not 500 <= timeout <= 30000:
            return result("", None, "instances", status="failed", code="browser_timeout_invalid", message="Discovery timeout must be 500 through 30000 milliseconds")
        try:
            return await asyncio.wait_for(self.discover(), timeout / 1000)
        except Exception:
            return {"ok": False, "status": "failed", "browsers": [], "error": {"code": "browser_discovery_unavailable", "message": "Browser discovery did not complete"}, "retryable": True}

    async def control(self, browser_id: str, action: str, tab_id: str | None = None,
                      arguments: dict[str, Any] | None = None, operation_id: str | None = None,
                      confirm: bool = False, timeout: int = 30000):
        arguments = arguments if arguments is not None else {}
        dispatched = False
        lock = None
        acquired = False
        generation = ""
        try:
            if not isinstance(browser_id, str) or not ID.fullmatch(browser_id):
                raise BrowserControlError("browser_id_required", "Use an explicit browser_id returned by browser_instances")
            if not isinstance(arguments, dict):
                raise BrowserControlError("browser_arguments_invalid", "arguments must be an object")
            self._validate(action, arguments)
            if type(timeout) is not int or not 500 <= timeout <= 120000:
                raise BrowserControlError("browser_timeout_invalid", "Timeout must be an integer from 500 through 120000 milliseconds")
            deadline = asyncio.get_running_loop().time() + timeout / 1000
            def remaining():
                budget = deadline - asyncio.get_running_loop().time()
                if budget <= .02:
                    raise BrowserControlError("browser_timeout_before_dispatch", "Operation budget expired before dispatch", True)
                return budget
            if tab_id is not None and (not isinstance(tab_id, str) or not tab_id or len(tab_id) > 128):
                raise BrowserControlError("browser_tab_invalid", "tab_id must be a bounded explicit string")
            if action not in NO_TAB and not tab_id:
                raise BrowserControlError("browser_tab_required", "This action requires an explicit observed tab_id")
            if action == "status" and (operation_id or arguments.get("operation_id")):
                key = operation_id or arguments["operation_id"]
                row = self.receipts.get(str(key))
                if not row or row["browser_id"] != browser_id:
                    raise BrowserControlError("browser_operation_not_found", "No operation receipt exists for this browser and identifier")
                return BrowserReceipts.public(row)
            write = action in WRITE_ACTIONS
            if write:
                if confirm is not True:
                    raise BrowserControlError("browser_confirmation_required", "Explicit confirm=true is required for a browser write")
                if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
                    raise BrowserControlError("browser_operation_id_required", "Use a stable unique operation_id for each intended write")
                fingerprint = hashlib.sha256(json.dumps([browser_id, tab_id, action, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                old = self.receipts.get(operation_id)
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise BrowserControlError("browser_operation_conflict", "This operation_id belongs to a different action or target")
                    return {**BrowserReceipts.public(old), "duplicate_call": True, "redispatched": False}
            found = await asyncio.wait_for(self.discover(), remaining())
            matches = [item for item in found.get("browsers", []) if item.get("browser_id") == browser_id and item.get("connected")]
            if len(matches) != 1:
                raise BrowserControlError("browser_disconnected_or_ambiguous", "Browser instance is disconnected or ambiguous; discover it again", True)
            browser = matches[0]
            generation = str(browser.get("connection_generation", ""))
            if not generation:
                raise BrowserControlError("browser_identity_incomplete", "Extension must provide stable identity and a connection generation")
            expected = arguments.get("expected_generation")
            if write and not expected:
                raise BrowserControlError("browser_generation_required", "Pass arguments.expected_generation from current browser discovery before a write")
            if expected is not None and expected != generation:
                raise BrowserControlError("browser_generation_changed", "Browser reconnected; discover and observe its current tabs before writing", True)
            supported = browser.get("supported_actions", [])
            if browser.get("adapter") != "native_ipc" and ("engine" in arguments or arguments.get("mode") == "window"):
                raise BrowserControlError("unsupported", "This extension adapter cannot select native engines or capture a native window")
            if browser.get("browser_kind") == "xiaoruo_browser" and str(browser.get("version", "")).startswith("1.8.") and (arguments.get("shadow_path") or arguments.get("frame_id", 0) != 0):
                raise BrowserControlError("browser_scope_unsupported", "Legacy CEF cannot execute scoped frame or Shadow DOM actions; no fallback was attempted")
            if action not in {"status", "reconcile"} and action not in supported:
                raise BrowserControlError("browser_action_unsupported", "The selected adapter does not advertise this action")
            lock = self.target_lock(browser_id, tab_id)
            await asyncio.wait_for(lock.acquire(), remaining())
            acquired = True
            # Revalidate after waiting: never act on a replacement connection.
            again = await asyncio.wait_for(self.discover(), remaining())
            if not any(item.get("browser_id") == browser_id and item.get("connection_generation") == generation and item.get("connected") for item in again.get("browsers", [])):
                raise BrowserControlError("browser_generation_changed", "Browser connection changed while the operation was waiting", True)
            current_tabs = None
            target = None
            if tab_id or action == "list_tabs":
                current_tabs = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                tabs = current_tabs.get("tabs", []) if isinstance(current_tabs, dict) else []
                if tab_id:
                    owners = [item for item in tabs if str(item.get("tab_id", item.get("id", ""))) == tab_id]
                    if len(owners) != 1:
                        raise BrowserControlError("browser_tab_not_owned", "Explicit tab is absent or ambiguous in the selected browser; no fallback was attempted")
                    target = owners[0]
            if action == "status":
                return result(browser_id, tab_id, action, data=browser, verified=True, connection_generation=generation)
            if action == "list_tabs":
                return result(browser_id, None, action, data=current_tabs, executed=True, verified=True, connection_generation=generation)
            if action == "reconcile":
                if confirm is not True:
                    raise BrowserControlError("browser_confirmation_required", "Explicit confirm=true is required to reconcile an uncertain action")
                key = str(arguments.get("operation_id") or operation_id or "")
                old = self.receipts.get(key)
                observation = self._observations.get(str(arguments.get("observation_id", "")))
                decision = arguments.get("decision")
                explanation = arguments.get("evidence")
                if not old or old["browser_id"] != browser_id or old["tab_id"] != tab_id:
                    raise BrowserControlError("browser_operation_not_found", "Operation does not belong to this browser and tab")
                if not observation or any(observation[k] != value for k, value in {"browser_id": browser_id, "tab_id": tab_id, "generation": generation}.items()) or time.monotonic() - observation["at"] > 300:
                    raise BrowserControlError("browser_reconciliation_observation_required", "Use a fresh successful read observation from this exact browser, tab and connection")
                if old["url"] and observation["url"] != old["url"] and old["action"] != "navigate":
                    raise BrowserControlError("browser_reconciliation_page_changed", "Fresh observation is from a different page than the uncertain action")
                if decision not in {"verified", "not_applied"} or not isinstance(explanation, str) or not 10 <= len(explanation) <= 2000:
                    raise BrowserControlError("browser_reconciliation_evidence_required", "Provide verified/not_applied plus specific observed evidence")
                saved = result(browser_id, tab_id, old["action"], data={"reconciliation": decision, "evidence": explanation}, operation_id=key,
                               executed=decision == "verified", verified=True, connection_generation=generation, observation_id=arguments["observation_id"])
                saved["status"] = "reconciled_" + decision
                saved["ok"] = True
                self.receipts.save(key, saved)
                return saved
            resource = self.resource(browser_id, tab_id)
            if write:
                pending_id = self.receipts.pending(resource)
                if pending_id:
                    raise BrowserControlError("browser_target_fenced", "A prior write has an uncertain result; use its status, fresh read and explicit reconciliation")
                if self.desktop_check is not None:
                    await asyncio.wait_for(self.desktop_check(), remaining())
                pending = result(browser_id, tab_id, action, status="dispatching", executed=None,
                                 operation_id=operation_id, connection_generation=generation)
                self.receipts.reserve(operation_id, fingerprint, browser_id, tab_id, generation, action, resource, str((target or {}).get("url", "")), pending)
            payload = {key: value for key, value in arguments.items() if key != "expected_generation"}
            if action in {"debug_attach", "debug_detach"}:
                payload["confirm"] = confirm
            if tab_id:
                payload["tab_id"] = tab_id
            payload["timeout"] = max(1, int(remaining() * 1000))
            dispatched = True
            data = await asyncio.wait_for(self.dispatch(browser_id, generation, WIRE[action], payload, remaining()), remaining())
            if not isinstance(data, dict):
                raise BrowserControlError("browser_response_invalid", "Adapter did not return structured evidence")
            if data.get("ok") is False or data.get("error"):
                if browser.get("adapter") == "native_ipc":
                    failure = data.get("error") if isinstance(data.get("error"), dict) else {}
                    no_effect = data.get("native_dispatched") is False or data.get("action_may_have_run") is False
                    known_failure = data.get("native_terminal") is True and data.get("native_state") in {"failed", "timed_out", "cancelled"}
                    known = no_effect or known_failure or not write
                    response = result(browser_id, tab_id, action,
                        status="failed" if known else "outcome_unknown",
                        data=data, code=str(failure.get("code") or "native_operation_failed"),
                        message=str(failure.get("message") or "Native browser operation did not succeed"),
                        executed=False if no_effect else None,
                        verified=False, operation_id=operation_id,
                        native_task_id=data.get("native_task_id"), connection_generation=generation)
                    if write:
                        self.receipts.save(operation_id, response)
                    return response
                raise BrowserControlError("browser_adapter_refused", "Adapter returned a real failure; inspect the current page before any new write", not write)
            verified = not write
            if write:
                verified = data.get("verified") is True and action != "press"
                if action in {"navigate", "open_tab", "close_tab", "activate_tab"}:
                    check = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                    items = check.get("tabs", [])
                    actual_id = str(data.get("tab_id", tab_id or ""))
                    actual = [item for item in items if str(item.get("tab_id", item.get("id", ""))) == actual_id]
                    verified = not actual if action == "close_tab" else len(actual) == 1
                    if verified and action == "navigate":
                        verified = actual[0].get("url") == arguments["url"]
                    if verified and action == "open_tab" and arguments.get("url"):
                        verified = actual[0].get("url") == arguments["url"]
                    if verified and action == "activate_tab":
                        verified = actual[0].get("active") is True
                    if verified and browser.get("adapter") == "native_ipc" and action in {"open_tab", "navigate"}:
                        effective = actual[0].get("committed_url") or actual[0].get("pendingUrl") or actual[0].get("pending_url") or actual[0].get("url")
                        verified = (effective == arguments.get("url") and actual[0].get("loading") is False
                                    and (actual[0].get("documentReady") is True or actual[0].get("document_ready") is True))
                if data.get("ok") is False:
                    verified = False
            response = result(browser_id, tab_id, action, data=data, executed=True, verified=verified,
                              status="completed" if verified else "dispatched_unverified", operation_id=operation_id,
                              connection_generation=generation, retryable=False)
            if action == "screenshot" and verified and isinstance(data.get("path"), str):
                response["artifacts"] = [{"path": data["path"], "mime_type": data.get("mime_type", "image/png"),
                    "size_bytes": data.get("size_bytes"), "private": True}]
            if action in {"read", "debug_snapshot"} and tab_id:
                response["observation_id"] = self._observe(browser_id, tab_id, generation, str(data.get("url") or (target or {}).get("url") or ""))
            if not verified:
                response["error"] = {"code": "browser_verification_required", "message": "Action returned, but its effect is not confirmed; read this page and reconcile before another write"}
            if write:
                self.receipts.save(operation_id, response)
            return response
        except asyncio.CancelledError:
            if dispatched and action in WRITE_ACTIONS and operation_id:
                try:
                    self.receipts.save(operation_id, result(browser_id, tab_id, action, status="outcome_unknown", code="browser_cancelled_after_dispatch", message="Caller cancelled after dispatch; inspect the page rather than replaying", executed=None, operation_id=operation_id, connection_generation=generation))
                except sqlite3.Error:
                    pass
            raise
        except Exception as exc:
            write_dispatched = dispatched and action in WRITE_ACTIONS
            if isinstance(exc, BrowserControlError):
                code, message, retryable = exc.code, str(exc), exc.retryable
            elif isinstance(exc, sqlite3.Error):
                code, message, retryable = "browser_receipt_database_busy", "Local operation receipts are unavailable; no automatic retry was attempted", not write_dispatched
            elif isinstance(exc, TimeoutError):
                code, message, retryable = "browser_timeout", "Browser operation exceeded its bounded timeout", not write_dispatched
            else:
                code, message, retryable = "browser_transport_unavailable", "Browser adapter disconnected or rejected the operation; inspect the current browser state", not write_dispatched
            value = result(browser_id, tab_id, action, status="outcome_unknown" if write_dispatched else "failed", code=code, message=message,
                           retryable=retryable and not write_dispatched, executed=None if write_dispatched else False,
                           operation_id=operation_id, connection_generation=generation)
            if write_dispatched and operation_id:
                try:
                    self.receipts.save(operation_id, value)
                except sqlite3.Error:
                    pass
            return value
        finally:
            if lock is not None and acquired:
                lock.release()