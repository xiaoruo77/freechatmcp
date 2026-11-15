"""Explicit browser routing, bounded coordination and durable write receipts.

Only fixed browser actions cross this facade. It neither starts a browser nor
evaluates caller-provided JavaScript. Page contents never grant authority.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
import uuid
from weakref import WeakValueDictionary

from .browser_upload import UploadError
from . import extension_background as edge_background_policy
from .browser_navigation import navigation_target as verified_navigation_target
from .browser_navigation import download_record as verified_download_record
from .browser_navigation import current_navigation, current_download

WIRE = {"status": None, "list_tabs": "list_tabs", "open_tab": "open_tab", "close_tab": "close_tab",
        "activate_tab": "activate_tab", "navigate": "navigate", "read": "read_page", "click": "click",
        "type": "type_text", "press": "press_key", "select": "select_option", "scroll": "scroll",
        "wait": "wait", "screenshot": "screenshot", "downloads": "list_downloads",
        "debug_status": "debug_status", "debug_attach": "debug_attach", "debug_snapshot": "debug_snapshot",
        "debug_screenshot": "debug_screenshot", "debug_detach": "debug_detach", "reconcile": None, "task_status": "task_status",
        "upload_file": "upload_file"}
WRITE_ACTIONS = {"open_tab", "close_tab", "activate_tab", "navigate", "click", "type", "press", "select", "scroll", "debug_attach", "debug_detach", "upload_file"}
NO_TAB = {"status", "list_tabs", "open_tab", "downloads", "task_status", "reconcile"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}$")
COMMON = {"expected_generation", "execution_mode"}
BACKGROUND_ACTIONS = {"click", "type", "press", "select", "scroll", "upload_file", "read", "screenshot"}
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
    "reconcile": {"operation_id", "observation_id", "decision", "evidence", "acknowledge_unknown"},
    "upload_file": {"file_path", "selector", "wait_timeout_ms"},
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


def _recovered_native_screenshot(value: Any, *, task_id: str | None = None) -> bool:
    """Only an explicitly verified native capture receipt can expose pixels."""
    if not isinstance(value, dict):
        return False
    task = value.get("native_task")
    if (value.get("adapter") != "native_ipc" or value.get("recovered_from_task_status") is not True
            or value.get("verified") is not True or value.get("native_task_succeeded") is not True
            or value.get("native_terminal") is not True or value.get("native_state") != "succeeded"
            or ("capture_pixels_available" in value and value["capture_pixels_available"] is not True)
            or value.get("ok") is False or value.get("error") or not isinstance(task, dict)
            or task.get("state") != "succeeded" or task.get("errorCode") or task.get("error")):
        return False
    action = task.get("action")
    capture = {"screenshot": ("page", "page"), "screenshot_window": ("window", "native_window")}.get(action) if isinstance(action, str) else None
    identifier = value.get("native_task_id")
    task_ids = [task[key] for key in ("id", "taskId", "task_id") if key in task]
    size = value.get("size_bytes")
    return (capture is not None and (value.get("screenshot_mode"), value.get("pixel_scope")) == capture
            and isinstance(identifier, str) and bool(identifier) and bool(task_ids)
            and all(item == identifier for item in task_ids) and (task_id is None or identifier == task_id)
            and isinstance(value.get("path"), str) and bool(value["path"])
            and value.get("mime_type") == "image/png" and type(size) is int and 0 < size <= 16 * 1024 * 1024)


def _receipt_data(value: Any, depth: int = 0) -> Any:
    """Keep bounded diagnostic state, never raw DOM, screenshots or credentials."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        denied = {"data_url", "text", "html", "body", "content", "cookies", "cookie", "password", "token", "authorization", "data_base64", "file_data_base64"}
        return {str(k): _receipt_data(v, depth + 1) for k, v in list(value.items())[:100] if str(k).lower() not in denied}
    if isinstance(value, list):
        return [_receipt_data(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    return value if value is None or isinstance(value, (int, bool, float)) else None


def _native_diagnostic_summary(value: Any) -> dict[str, Any] | None:
    """Retain task receipts when a later guard fails, without copying payloads."""
    if not isinstance(value, dict):
        return None

    def token(item, limit=256):
        return item if isinstance(item, str) and 0 < len(item) <= limit and re.fullmatch(r"[A-Za-z0-9_.:-]+", item) else None

    summary = {}
    for key in ("ok", "verified", "native_dispatched", "action_may_have_run"):
        if type(value.get(key)) is bool:
            summary[key] = value[key]
    for key in ("adapter", "execution_mode", "verification"):
        if (item := token(value.get(key))) is not None:
            summary[key] = item
    task = value.get("native_task")
    task = task if isinstance(task, dict) else {}
    public_task = {}
    for key in ("id", "taskId", "task_id", "action", "errorCode"):
        if (item := token(task.get(key))) is not None:
            public_task[key] = item
    for key in ("tab_id", "tabId"):
        item = task.get(key)
        if (type(item) is int and 0 <= item <= 2147483647) or token(item, 128) is not None:
            public_task[key] = item
    states = {"accepted", "running", "succeeded", "failed", "timed_out", "cancelled"}
    state = token(task.get("state"))
    if state in states:
        public_task["state"] = state
    else:
        state = token(value.get("native_state"))
    if state in states:
        summary.update(native_state=state, native_terminal=state in {"succeeded", "failed", "timed_out", "cancelled"})
    if public_task:
        summary["native_task"] = public_task
    task_id = next((item for item in (token(value.get("native_task_id")),
                   *(token(task.get(key)) for key in ("id", "taskId", "task_id"))) if item is not None), None)
    if task_id is not None:
        summary["native_task_id"] = task_id
    error = value.get("error")
    if isinstance(error, dict) and (code := token(error.get("code"))) is not None:
        summary["error"] = {"code": code}
    stages = value.get("stages")
    if isinstance(stages, dict):
        summary["stages"] = {key: stages[key] for key in ("file_selected", "upload_started", "server_received", "platform_processed")
                             if key in stages and (stages[key] is None or type(stages[key]) is bool)}
    return summary or None


class BrowserReceipts:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS unified_browser_operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, browser_id TEXT NOT NULL, tab_id TEXT, generation TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, updated REAL NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=.05)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, operation_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM unified_browser_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return dict(row) if row else None

    def pending(self, resource: str):
        with self.connect() as db:
            row = db.execute("SELECT operation_id FROM unified_browser_operations WHERE resource=? AND state IN ('dispatching','outcome_unknown','dispatched_unverified') LIMIT 1", (resource,)).fetchone()
        return row["operation_id"] if row else None

    def prior_upload(self, browser_id: str, tab_id: str, generation: str, upload_target: str):
        # A second key must not replace a selected/uncertain file. Other file
        # inputs in the same tab remain independent (for example a cover).
        with self.connect() as db:
            rows = db.execute("SELECT operation_id,result FROM unified_browser_operations WHERE browser_id=? AND tab_id=? AND generation=? AND action='upload_file' AND state IN ('dispatching','outcome_unknown','dispatched_unverified','completed','reconciled_verified')",
                              (browser_id, tab_id, generation)).fetchall()
        return next((row["operation_id"] for row in rows if json.loads(row["result"]).get("upload_target") == upload_target), None)

    def reserve(self, operation_id: str, fingerprint: str, browser_id: str, tab_id: str | None,
                generation: str, action: str, resource: str, url: str, pending: dict[str, Any]):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT operation_id FROM unified_browser_operations WHERE operation_id=?", (operation_id,)).fetchone()
            fence = db.execute("SELECT operation_id FROM unified_browser_operations WHERE resource=? AND state IN ('dispatching','outcome_unknown','dispatched_unverified') LIMIT 1", (resource,)).fetchone()
            if previous or fence:
                raise BrowserControlError("browser_target_fenced", "Another operation already owns this target; read status and inspect the page before reconciling", False)
            db.execute("INSERT INTO unified_browser_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (operation_id, fingerprint, browser_id, tab_id, generation, action, resource, url, "dispatching", json.dumps(pending, ensure_ascii=False), time.time()))

    def save(self, operation_id: str, value: dict[str, Any], *, expected_state: str | None = None, expected_generation: str | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT state,generation,result FROM unified_browser_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if previous is None:
                if expected_state is not None or expected_generation is not None:
                    raise BrowserControlError("browser_receipt_changed", "The original operation receipt disappeared while reconciling")
                return False
            if ((expected_state is not None and previous["state"] != expected_state)
                    or (expected_generation is not None and previous["generation"] != expected_generation)):
                raise BrowserControlError("browser_receipt_changed", "The original operation changed while reconciling; inspect its current receipt and take a fresh observation")
            if previous["state"] not in {"dispatching", "outcome_unknown", "dispatched_unverified"}:
                # A late adapter response may not overwrite a reconciled or
                # finished operation owned by another caller/process.
                return False
            if str(value.get("status", "")).startswith("reconciled_"):
                if previous:
                    prior = json.loads(previous["result"])
                    value = {**value, "original_receipt": prior.get("original_receipt") or _receipt_data({key: prior[key]
                        for key in ("status", "error", "evidence", "result", "operation_id", "connection_generation") if key in prior})}
            db.execute("UPDATE unified_browser_operations SET state=?,result=?,updated=? WHERE operation_id=?",
                       (value["status"], json.dumps(_receipt_data(value), ensure_ascii=False), time.time(), operation_id))
            return True

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(row["result"])
        if row["state"] == "dispatching":
            value.update(ok=False, status="outcome_unknown", retryable=False,
                         error={"code": "browser_outcome_unknown", "message": "Operation is running or was interrupted; inspect status and page before any retry"})
            value["evidence"] = {"executed": None, "verified": False}
        if row["state"] in {"reconciled_observed_page", "reconciled_cancelled_tracking"}:
            value.update(ok=False, status="outcome_unknown", retryable=False,
                tracking_state="cancelled_tracking" if row["state"] == "reconciled_cancelled_tracking" else "continued_from_observed_page", repeat_write_allowed=False,
                error={"code": "browser_original_outcome_unknown", "message": "The original write remains unknown. Continue from a fresh page read; this operation identifier never replays the write."})
            value["evidence"] = {"executed": None, "verified": False}
        return value


class UnifiedBrowserService:
    def __init__(self, database: Path, discover: Callable[[], Awaitable[dict[str, Any]]],
                 dispatch: Callable[..., Awaitable[Any]],
                 desktop_check: Callable[[], Awaitable[None]] | None = None,
                 background_check: Callable[[], Awaitable[dict[str, Any] | None]] | None = None):
        self.receipts = BrowserReceipts(database)
        self.discover = discover
        self.dispatch = dispatch
        self.desktop_check = desktop_check
        self.background_check = background_check
        # Each active/waiting control keeps its own strong reference. Idle tab
        # IDs must not accumulate forever and eventually exhaust coordination.
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
        self._observations: dict[str, dict[str, Any]] = {}

    def target_lock(self, browser_id: str, tab_id: str | None):
        key = (browser_id, tab_id or "$browser")
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) >= 2048:
                raise BrowserControlError("browser_coordination_limit", "Too many tracked targets; retain receipts and restart the bridge when idle")
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def resource(browser_id: str, tab_id: str | None):
        return hashlib.sha256(json.dumps([browser_id, tab_id or "$browser"]).encode()).hexdigest()

    def _observe(self, browser_id: str, tab_id: str | None, generation: str, url: str, *, target_exists=None, content_observed=False, metadata_observed=False, inventory_observed=False):
        now = time.monotonic()
        self._observations = {key: item for key, item in self._observations.items() if now - item["at"] < 300}
        if len(self._observations) >= 512:
            self._observations.pop(next(iter(self._observations)))
        key = "observation_" + uuid.uuid4().hex
        self._observations[key] = {"browser_id": browser_id, "tab_id": tab_id, "generation": generation,
            "url": url, "at": now, "target_exists": target_exists, "content_observed": content_observed,
            "metadata_observed": metadata_observed, "inventory_observed": inventory_observed}
        return key

    def _pending_row(self, browser_id: str, tab_id: str | None):
        key = self.receipts.pending(self.resource(browser_id, tab_id))
        if key:
            return self.receipts.get(key)
        # A known newly created target belongs to the browser-level open-tab
        # receipt, even though subsequent observations address the actual tab.
        if tab_id:
            key = self.receipts.pending(self.resource(browser_id, None))
            row = self.receipts.get(key) if key else None
            if row and row["action"] == "open_tab":
                data = json.loads(row["result"]).get("result")
                if isinstance(data, dict) and str(data.get("tab_id") or "") == tab_id:
                    return row
        return None

    def _save_response(self, operation_id: str, value: dict[str, Any]):
        if self.receipts.save(operation_id, value):
            return value
        authoritative = self.receipts.get(operation_id)
        if authoritative:
            return {**BrowserReceipts.public(authoritative), "late_response_ignored": True, "redispatched": False}
        return {**value, "ok": False, "status": "outcome_unknown", "retryable": False,
            "error": {"code": "browser_receipt_missing", "message": "The action's durable receipt is unavailable; inspect the exact page before any new write"},
            "evidence": {"executed": None, "verified": False}}

    def _with_pending_recovery(self, value: dict[str, Any], row: dict[str, Any] | None, *,
                               generation: str = "", browser: dict[str, Any] | None = None,
                               execution_mode: str | None = None):
        """Describe the next exact calls without replaying or retiring a write.

        Build this dynamically even for historical receipts. A read remains a
        successful read; the unresolved write is a separate continuation checkpoint.
        No page values, original arguments or credential-bearing URLs enter the plan.
        """
        if not row or row["state"] not in {"dispatching", "outcome_unknown", "dispatched_unverified"}:
            return value
        browser_id, tab_id, key = row["browser_id"], row["tab_id"], row["operation_id"]
        current_generation = generation or row["generation"]
        live_dispatch = row["state"] == "dispatching" and (not generation or row["generation"] == generation)
        original = json.loads(row["result"])
        data = original.get("result") if isinstance(original.get("result"), dict) else {}
        if not tab_id and row["action"] == "open_tab" and isinstance(data.get("tab_id"), (str, int)) and str(data["tab_id"]):
            tab_id = str(data["tab_id"])
        edge_dom = ((browser or {}).get("dom_protocol") == edge_background_policy.DOM_PROTOCOL
                    or data.get("dom_protocol") == edge_background_policy.DOM_PROTOCOL)
        read_arguments = {"mode": "visible", "include_frames": True}
        if execution_mode:
            read_arguments["execution_mode"] = execution_mode
        elif edge_dom:
            read_arguments["execution_mode"] = "extension_dom"
        elif data.get("background_protocol") == edge_background_policy.PROTOCOL:
            read_arguments["execution_mode"] = "extension_background"
        if generation:
            read_arguments["expected_generation"] = generation
        status_call = {"browser_id": browser_id, "action": "status", "operation_id": key}
        if tab_id:
            status_call["tab_id"] = tab_id
        read_call = ({"browser_id": browser_id, "tab_id": tab_id, "action": "read", "arguments": read_arguments}
                     if tab_id else {"browser_id": browser_id, "action": "list_tabs"})
        plan = {"blocking_operation_id": key, "action": row["action"], "state": row["state"],
            "browser_id": browser_id, "tab_id": tab_id, "expected_generation": current_generation,
            "original_outcome": "unknown", "automatic_retry_allowed": False, "repeat_write_allowed": False,
            "live_dispatch": live_dispatch, "next_steps": ["status", "read" if tab_id else "list_tabs", "reconcile"],
            "status_call": status_call, "read_call": read_call, "reconciliation_ready": False,
            "evidence_required": True, "permission_denial": False}
        if tab_id:
            plan["presence_read_call"] = {"browser_id": browser_id, "tab_id": tab_id, "action": "read",
                "arguments": {**read_arguments, "mode": "tab_presence"}}
            plan["if_target_absent"] = "Use presence_read_call to observe this exact tab's absence; do not guess a replacement tab."
        observation_id = value.get("observation_id")
        observation = self._observations.get(str(observation_id or ""))
        exact_observation = (observation and generation and
            all(observation[k] == v for k, v in {"browser_id": browser_id, "tab_id": tab_id, "generation": generation}.items())
            and time.monotonic() - observation["at"] <= 300)
        decision = None
        if exact_observation and not live_dispatch:
            if tab_id is None and row["action"] == "open_tab" and observation.get("inventory_observed") is True:
                decision = "cancel_tracking"
            elif observation.get("target_exists") is False:
                decision = "target_closed"
            elif observation.get("content_observed") is True:
                decision = "continue_from_observed_page"
            elif observation.get("metadata_observed") is True:
                decision = "cancel_tracking"
        if decision:
            reconcile_arguments = {"operation_id": key, "observation_id": observation_id, "decision": decision,
                "acknowledge_unknown": True, "evidence": "", "expected_generation": generation}
            call = {"browser_id": browser_id, "action": "reconcile", "confirm": True, "arguments": reconcile_arguments}
            if tab_id:
                call["tab_id"] = tab_id
            plan.update(reconciliation_ready=True, observation_id=observation_id, reconcile_call=call,
                evidence_instruction="Replace the empty evidence with facts from this fresh observation. The original outcome remains unknown; do not replay it or claim it succeeded.")
        elif live_dispatch:
            plan["next_steps"] = ["status"]
            plan["wait_for_dispatch_completion"] = True
        value.update(pending_operation={"operation_id": key, "action": row["action"], "state": row["state"],
                     "browser_id": browser_id, "tab_id": tab_id}, recovery=plan,
                     continuation_required=True, suggested_next_tool="browser_control", repeat_write_allowed=False)
        return value

    @staticmethod
    def _validate(action: str, arguments: dict[str, Any]):
        if action not in WIRE:
            raise BrowserControlError("browser_action_unsupported", "Action is outside the fixed browser allowlist")
        extras = set(arguments) - FIELDS[action] - COMMON
        if extras:
            raise BrowserControlError("browser_arguments_invalid", "Unsupported arguments for this action; arbitrary scripts and protocol commands are unavailable")
        execution_mode = arguments.get("execution_mode", "foreground")
        if not isinstance(execution_mode, str) or execution_mode not in {"foreground", "native_background", "extension_background", "extension_dom"}:
            raise BrowserControlError("browser_execution_mode_invalid", "execution_mode must be foreground, native_background, extension_background or extension_dom")
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
        if action == "upload_file" and (not isinstance(arguments.get("file_path"), str) or not 1 <= len(arguments["file_path"]) <= 4000 or not arguments.get("selector")):
            raise BrowserControlError("browser_arguments_invalid", "Upload requires an absolute allowed media file_path and a precise observed file_inputs selector")
        for key, lower, upper in (("frame_id", 0, 2147483647), ("max_chars", 100, 100000), ("settle_ms", 0, 5000), ("milliseconds", 0, 120000), ("limit", 1, 100), ("pixels", -100000, 100000), ("x", 0, 100000), ("y", 0, 100000), ("wait_timeout_ms", 0, 30000)):
            if key in arguments and (type(arguments[key]) is not int or not lower <= arguments[key] <= upper):
                raise BrowserControlError("browser_arguments_invalid", "Numeric action argument is outside its bounded range")
        for key in ("exact", "clear", "include_frames", "full_page"):
            if key in arguments and type(arguments[key]) is not bool:
                raise BrowserControlError("browser_arguments_invalid", "Boolean action argument must be a real boolean")
        if "shadow_path" in arguments and (not isinstance(arguments["shadow_path"], list) or len(arguments["shadow_path"]) > 8 or any(not isinstance(item, str) or len(item) > 1000 for item in arguments["shadow_path"])):
            raise BrowserControlError("browser_arguments_invalid", "Shadow path must contain up to eight bounded selectors")

    @staticmethod
    def _background_identity(browser: dict[str, Any]):
        # These are adapter-owned discovery facts, never an inference from an ID
        # prefix, requested engine, page contents or extension-provided metadata.
        if (browser.get("adapter") != "native_ipc" or browser.get("stable_identity") is not True
                or browser.get("transport") != "windows_named_pipe"
                or browser.get("browser_kind") != "xiaoruo_browser"
                or type(browser.get("native_pid")) is not int or browser["native_pid"] <= 0):
            raise BrowserControlError("native_background_identity_required", "Background execution requires a verified native IPC browser process; no adapter fallback was attempted")

    @staticmethod
    def _background_snapshot(value: Any, tab_id: str, phase: str) -> dict[str, Any]:
        items = value.get("tabs", []) if isinstance(value, dict) else []
        targets = [item for item in items if isinstance(item, dict) and str(item.get("tab_id", item.get("id", ""))) == tab_id]
        target = targets[0] if len(targets) == 1 else {}
        active = str(value.get("active_tab_id") or "") if isinstance(value, dict) else ""
        active_items = [item for item in items if isinstance(item, dict) and item.get("active") is True]
        return {"phase": phase, "observed_at": time.time(), "active_tab_id": active,
                "target_tab_id": tab_id, "target_present": len(targets) == 1,
                "target_active": target.get("active"), "target_engine": target.get("engine"),
                "verified": isinstance(value, dict) and value.get("verified") is True and value.get("adapter") == "native_ipc",
                "active_consistent": bool(active) and len(active_items) == 1 and str(active_items[0].get("tab_id", active_items[0].get("id", ""))) == active}

    @staticmethod
    def _background_target(snapshot: dict[str, Any], expected_active: str | None = None):
        if not snapshot["verified"] or not snapshot["active_consistent"] or not snapshot["target_present"]:
            raise BrowserControlError("native_background_state_unverified", "Background execution requires a verified exact target and unambiguous active tab evidence")
        if snapshot["target_engine"] != "chromium":
            raise BrowserControlError("native_background_cef_required", "Only an existing CEF tab supports this background policy; engine switching is unavailable")
        if snapshot["target_active"] is not False or snapshot["target_tab_id"] == snapshot["active_tab_id"]:
            raise BrowserControlError("foreground_required", "The selected tab is active; background execution was refused without changing focus")
        if expected_active is not None and snapshot["active_tab_id"] != expected_active:
            raise BrowserControlError("native_background_active_changed", "The user's active tab changed during background execution; focus was not restored and the action must not be replayed")

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
        reserved = False
        lock = None
        acquired = False
        generation = ""
        browser = None
        upload_target = None
        background = False
        edge_background = False
        browser_dom = False
        cooperative_tab = False
        edge_initial = None
        independent_target = False
        navigation_continuation = None
        download_confirmation = None
        download_baseline_ids = None
        click_dispatched_at = None
        background_execution = None
        background_active_id = None
        native_diagnostics = None
        def background_extra():
            return {"background_execution": background_execution} if background_execution is not None else {}
        def native_extra():
            return {"native_task_id": native_diagnostics["native_task_id"]} if native_diagnostics and "native_task_id" in native_diagnostics else {}
        try:
            if not isinstance(browser_id, str) or not ID.fullmatch(browser_id):
                raise BrowserControlError("browser_id_required", "Use an explicit browser_id returned by browser_instances")
            if not isinstance(arguments, dict):
                raise BrowserControlError("browser_arguments_invalid", "arguments must be an object")
            self._validate(action, arguments)
            background = arguments.get("execution_mode") == "native_background"
            edge_background = arguments.get("execution_mode") == "extension_background"
            browser_dom = arguments.get("execution_mode") == "extension_dom"
            if browser_dom and action not in edge_background_policy.DOM_ACTIONS:
                raise BrowserControlError("browser_dom_action_unsupported", "This action is unavailable in the browser-only DOM mode")
            if edge_background:
                background_execution = {"mode": "extension_background", "snapshots": [], "desktop_checks": []}
                if action not in edge_background_policy.ACTIONS:
                    raise BrowserControlError("extension_background_action_unsupported", "This action cannot run without activating Edge; no foreground fallback was attempted")
            if background:
                background_execution = {"mode": "native_background", "snapshots": [], "desktop_checks": []}
                if action not in BACKGROUND_ACTIONS or (action == "screenshot" and arguments.get("mode", "page") != "page"):
                    raise BrowserControlError("foreground_required", "This action requires the existing foreground policy; background execution cannot open, activate, close, navigate, debug or capture a window")
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
                current_browser = None
                try:
                    found = await asyncio.wait_for(self.discover(), min(1, remaining()))
                    candidates = [item for item in found.get("browsers", []) if item.get("browser_id") == browser_id and item.get("connected")]
                    if len(candidates) == 1:
                        current_browser = candidates[0]
                except Exception:
                    pass  # Historical receipt status remains readable while offline.
                return self._with_pending_recovery(BrowserReceipts.public(row), row,
                    generation=str((current_browser or {}).get("connection_generation") or ""), browser=current_browser)
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
                    return self._with_pending_recovery({**BrowserReceipts.public(old), "duplicate_call": True, "redispatched": False}, old,
                        execution_mode=arguments.get("execution_mode"))
            found = await asyncio.wait_for(self.discover(), remaining())
            matches = [item for item in found.get("browsers", []) if item.get("browser_id") == browser_id and item.get("connected")]
            if len(matches) != 1:
                raise BrowserControlError("browser_disconnected_or_ambiguous", "Browser instance is disconnected or ambiguous; discover it again", True)
            browser = matches[0]
            if browser_dom and (browser.get("adapter") != "authenticated_extension" or browser.get("browser_kind") != "edge"
                    or browser.get("stable_identity") is not True or browser.get("dom_protocol") != edge_background_policy.DOM_PROTOCOL
                    or action not in browser.get("dom_actions", [])):
                raise BrowserControlError("browser_dom_upgrade_required", "Browser-only page control requires Edge Bridge 1.9.4 or later")
            cooperative_tab = browser_dom and browser.get("dom_policy") == "cooperative_v2"
            if edge_background:
                edge_background_policy.identity(browser, action, BrowserControlError)
                independent_target = browser.get("background_policy") == "target_inactive_v2"
            if background:
                self._background_identity(browser)
            generation = str(browser.get("connection_generation", ""))
            if not generation:
                raise BrowserControlError("browser_identity_incomplete", "Extension must provide stable identity and a connection generation")
            expected = arguments.get("expected_generation")
            if write and not expected:
                raise BrowserControlError("browser_generation_required", "Pass arguments.expected_generation from current browser discovery before a write")
            if expected is not None and expected != generation:
                raise BrowserControlError("browser_generation_changed", "Browser reconnected; discover and observe its current tabs before writing", True)
            supported = browser.get("supported_actions", [])
            if action == "upload_file" and browser.get("adapter") != "native_ipc":
                raise BrowserControlError("unsupported", "Unified upload_file requires the advertised native CEF capability; the existing Edge upload entry point is unchanged")
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
            connected = [item for item in again.get("browsers", []) if item.get("browser_id") == browser_id and item.get("connection_generation") == generation and item.get("connected")]
            if len(connected) != 1:
                raise BrowserControlError("browser_generation_changed", "Browser connection changed while the operation was waiting", True)
            if background:
                self._background_identity(connected[0])
            if edge_background:
                edge_background_policy.identity(connected[0], action, BrowserControlError)
            current_tabs = None
            target = None
            presence_read = action == "read" and arguments.get("mode") == "tab_presence" and browser.get("adapter") == "authenticated_extension"
            if tab_id or action in {"list_tabs", "reconcile"} or edge_background:
                current_tabs = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                tabs = current_tabs.get("tabs", []) if isinstance(current_tabs, dict) else []
                if tab_id:
                    owners = [item for item in tabs if str(item.get("tab_id", item.get("id", ""))) == tab_id]
                    if len(owners) != 1 and not (presence_read and not owners) and not (action == "reconcile" and arguments.get("decision") == "target_closed" and not owners):
                        raise BrowserControlError("browser_tab_not_owned", "Explicit tab is absent or ambiguous in the selected browser; no fallback was attempted")
                    target = owners[0] if owners else None
            async def background_probe(phase: str):
                value = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                snapshot = self._background_snapshot(value, tab_id, phase)
                background_execution["snapshots"].append(snapshot)
                self._background_target(snapshot, background_active_id)

            async def check_background_desktop(phase: str):
                if self.background_check is None:
                    raise BrowserControlError("native_background_check_unavailable", "Background input requires the separate unlocked interactive desktop check")
                checked = await asyncio.wait_for(self.background_check(), remaining())
                if (edge_background or browser_dom) and (not isinstance(checked, dict) or checked.get("unlocked") is not True
                        or checked.get("secure_desktop") is not False or checked.get("session_active") is not True):
                    raise BrowserControlError("foreground_required", "Background Edge requires an unlocked active session and no secure desktop")
                # The bridge supplies only desktop facts. Keep a bounded allowlist
                # so user input history, tokens and window details cannot enter receipts.
                fields = {"unlocked", "secure_desktop", "session_active", "interactive", "desktop", "session_id", "source", "available", "reason"}
                if background_execution is not None:
                    background_execution["desktop_checks"].append({"phase": phase, **({key: _receipt_data(value) for key, value in checked.items() if key in fields} if isinstance(checked, dict) else {})})

            async def edge_probe(phase, target_id=tab_id, allow_missing=False):
                value = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                checked = edge_background_policy.snapshot(value, target_id, phase, BrowserControlError,
                    expected=edge_initial, allow_missing=allow_missing or presence_read, independent_target=independent_target)
                background_execution["snapshots"].append(checked)

            if edge_background:
                edge_initial = edge_background_policy.snapshot(current_tabs, tab_id, "initial", BrowserControlError,
                    allow_missing=presence_read, independent_target=independent_target)
                background_execution["snapshots"].append(edge_initial)

            if background:
                snapshot = self._background_snapshot(current_tabs, tab_id, "initial")
                background_execution["snapshots"].append(snapshot)
                self._background_target(snapshot)
                background_active_id = snapshot["active_tab_id"]
                background_execution["expected_active_tab_id"] = background_active_id
            if action == "status":
                return self._with_pending_recovery(
                    result(browser_id, tab_id, action, data=browser, verified=True, connection_generation=generation),
                    self._pending_row(browser_id, tab_id), generation=generation, browser=browser)
            if action == "list_tabs":
                if (not isinstance(current_tabs, dict) or not isinstance(current_tabs.get("tabs"), list)
                        or current_tabs.get("ok") is False or current_tabs.get("error")):
                    raise BrowserControlError("browser_inventory_unverified", "The browser did not return a verified tab inventory; no pending operation was changed")
                response = result(browser_id, None, action, data=current_tabs, executed=True, verified=True, connection_generation=generation)
                response["observation_id"] = self._observe(browser_id, None, generation, "", inventory_observed=True)
                return self._with_pending_recovery(response, self._pending_row(browser_id, None), generation=generation, browser=browser)
            if action == "reconcile":
                if confirm is not True:
                    raise BrowserControlError("browser_confirmation_required", "Explicit confirm=true is required to reconcile an uncertain action")
                key = str(arguments.get("operation_id") or operation_id or "")
                old = self.receipts.get(key)
                observation = self._observations.get(str(arguments.get("observation_id", "")))
                decision = arguments.get("decision")
                explanation = arguments.get("evidence")
                opened_target = None
                if old and old["action"] == "open_tab":
                    old_value = json.loads(old["result"]).get("result")
                    if isinstance(old_value, dict):
                        opened_target = str(old_value.get("tab_id") or "")
                if old and old["action"] == "click":
                    old_value = json.loads(old["result"]).get("result")
                    if isinstance(old_value, dict) and old_value.get("navigation_protocol") == "xiaoruo1-edge-navigation/1":
                        opened_target = str((old_value.get("navigation_target") or {}).get("tab_id") or "")
                if not old or old["browser_id"] != browser_id or (old["tab_id"] != tab_id and opened_target != tab_id):
                    raise BrowserControlError("browser_operation_not_found", "Operation does not belong to this browser and tab")
                if not observation or any(observation[k] != value for k, value in {"browser_id": browser_id, "tab_id": tab_id, "generation": generation}.items()) or time.monotonic() - observation["at"] > 300:
                    raise BrowserControlError("browser_reconciliation_observation_required", "Use a fresh successful read observation from this exact browser, tab and connection")
                if tab_id is None:
                    if (old["action"] != "open_tab" or old["tab_id"] is not None
                            or decision != "cancel_tracking" or arguments.get("acknowledge_unknown") is not True
                            or observation.get("inventory_observed") is not True
                            or old["state"] not in {"outcome_unknown", "dispatched_unverified", "dispatching"}
                            or (old["state"] == "dispatching" and old["generation"] == generation)
                            or not isinstance(explanation, str) or not 20 <= len(explanation) <= 2000):
                        raise BrowserControlError("browser_inventory_acknowledgement_required", "An unknown open_tab with no target can only cancel local tracking after a fresh exact-browser inventory and explicit acknowledgement; an active dispatch cannot be abandoned")
                    saved = result(browser_id, None, old["action"], data={"reconciliation": decision,
                        "original_outcome": "unknown", "operation_replayed": False, "repeat_original_write_allowed": False,
                        "goal_success_claimed": False, "created_tab_identified": False, "evidence": explanation},
                        operation_id=key, executed=None, verified=False, connection_generation=generation,
                        observation_id=arguments["observation_id"])
                    saved.update(status="reconciled_cancelled_tracking", summary="Only local open-tab tracking was cancelled after inspecting the current inventory; its original result remains unknown and is never replayed")
                    self.receipts.save(key, saved, expected_state=old["state"], expected_generation=old["generation"])
                    return saved
                if decision == "target_closed":
                    if (observation.get("target_exists") is not False or target is not None
                            or old["state"] not in {"outcome_unknown", "dispatched_unverified", "dispatching"}
                            or (old["state"] == "dispatching" and old["generation"] == generation)
                            or not isinstance(explanation, str) or not 10 <= len(explanation) <= 2000):
                        raise BrowserControlError("browser_closed_target_evidence_required", "Only a fresh exact-tab absence observation can retire an uncertain operation; an active dispatch cannot be abandoned")
                    saved = result(browser_id, tab_id, old["action"], data={"reconciliation": "target_closed",
                        "original_outcome": "unknown", "operation_replayed": False, "evidence": explanation},
                        operation_id=key, executed=None, verified=False, connection_generation=generation,
                        observation_id=arguments["observation_id"])
                    saved["status"] = "reconciled_target_closed"
                    self.receipts.save(key, saved, expected_state=old["state"], expected_generation=old["generation"])
                    return saved
                if observation.get("target_exists") is False:
                    raise BrowserControlError("browser_reconciliation_observation_required", "An absent tab cannot prove that the original action succeeded or was not applied")
                if decision == "cancel_tracking":
                    if (arguments.get("acknowledge_unknown") is not True
                            or not (observation.get("content_observed") is True or observation.get("metadata_observed") is True)
                            or target is None or old["state"] not in {"outcome_unknown", "dispatched_unverified", "dispatching"}
                            or (old["state"] == "dispatching" and old["generation"] == generation)
                            or not isinstance(explanation, str) or not 20 <= len(explanation) <= 2000):
                        raise BrowserControlError("browser_cancel_tracking_acknowledgement_required", "Cancelling local tracking requires a fresh exact-tab observation, explicit acknowledgement of the unknown result and no active dispatch")
                    saved = result(browser_id, tab_id, old["action"], data={"reconciliation": decision,
                        "original_outcome": "unknown", "operation_replayed": False, "repeat_original_write_allowed": False,
                        "goal_success_claimed": False, "evidence": explanation}, operation_id=key,
                        executed=None, verified=False, connection_generation=generation,
                        observation_id=arguments["observation_id"])
                    saved.update(status="reconciled_cancelled_tracking", summary="Only local tracking was cancelled; the original write remains unknown and is never replayed")
                    self.receipts.save(key, saved, expected_state=old["state"], expected_generation=old["generation"])
                    return saved
                if decision == "continue_from_observed_page":
                    if (arguments.get("acknowledge_unknown") is not True or observation.get("content_observed") is not True
                            or target is None or old["state"] not in {"outcome_unknown", "dispatched_unverified", "dispatching"}
                            or (old["state"] == "dispatching" and old["generation"] == generation)
                            or not isinstance(explanation, str) or not 20 <= len(explanation) <= 2000):
                        raise BrowserControlError("browser_observed_state_acknowledgement_required", "Continuing requires a fresh successful content read and explicit acknowledgement that the original action remains unknown; no click is repeated")
                    saved = result(browser_id, tab_id, old["action"], data={"reconciliation": decision,
                        "original_outcome": "unknown", "operation_replayed": False, "repeat_original_write_allowed": False,
                        "continuation_scope": "freshly_observed_page_only", "evidence": explanation}, operation_id=key,
                        executed=None, verified=False, connection_generation=generation,
                        observation_id=arguments["observation_id"])
                    saved.update(status="reconciled_observed_page", summary="Tracking resumed from the observed page; the original write remains unknown and must not be replayed")
                    self.receipts.save(key, saved, expected_state=old["state"], expected_generation=old["generation"])
                    return saved
                if (old["state"] not in {"outcome_unknown", "dispatched_unverified", "dispatching"}
                        or (old["state"] == "dispatching" and old["generation"] == generation)):
                    raise BrowserControlError("browser_receipt_not_reconcilable", "Only a pending completed dispatch can be reconciled; a live dispatch or finished receipt cannot be rejudged")
                if old["url"] and observation["url"] != old["url"] and old["action"] != "navigate":
                    receipt = json.loads(old["result"])
                    captured = receipt.get("result")
                    captured = captured if isinstance(captured, dict) else {}
                    task = captured.get("native_task")
                    task = task if isinstance(task, dict) else {}
                    before_url, after_url = urlsplit(old["url"]), urlsplit(observation["url"])
                    # A guarded background click can complete a same-tab route
                    # after its immediate DOM comparison. Permit only verified
                    # reconciliation of that recorded transition, never treating
                    # a changed page as proof that the action was not applied.
                    guarded_transition = (decision == "verified" and old["action"] == "click"
                        and old["generation"] == generation and browser.get("adapter") == "native_ipc"
                        and captured.get("helper") == "xiaoruo1-cef-background-interaction/1"
                        and captured.get("action") == "click" and captured.get("adapter") == "native_ipc"
                        and captured.get("execution_mode") == "native_background"
                        and captured.get("url_transition") is True and captured.get("action_may_have_run") is True
                        and captured.get("native_terminal") is True and captured.get("native_state") == "succeeded"
                        and task.get("state") == "succeeded" and not task.get("errorCode")
                        and captured.get("native_task_id") == (task.get("taskId") or task.get("id"))
                        and before_url.scheme in {"http", "https"} and before_url.scheme == after_url.scheme
                        and bool(before_url.netloc) and before_url.netloc.casefold() == after_url.netloc.casefold()
                        and not before_url.username and not after_url.username)
                    if (decision == "verified" and old["action"] == "click" and old["generation"] == generation
                            and browser.get("adapter") == "authenticated_extension" and browser.get("browser_kind") == "edge"):
                        guarded_transition = edge_background_policy.guarded_click_transition(captured, old["url"], observation["url"])
                        if not guarded_transition and current_tabs:
                            nav_target = verified_navigation_target(captured, source_tab_id=old["tab_id"],
                                source_url=old["url"], tabs=current_tabs.get("tabs", []),
                                frame_id=(captured.get("navigation_source") or {}).get("frame_id", 0))
                            guarded_transition = nav_target is not None and nav_target["tab_id"] == tab_id and nav_target["url"] == observation["url"]
                            captured_target = captured.get("navigation_target")
                            if (not guarded_transition and observation.get("content_observed") is True
                                    and captured.get("operation_no_replay") is True and captured.get("action_may_have_run") is True
                                    and isinstance(captured_target, dict) and captured_target.get("tab_id") == tab_id):
                                # A fresh exact-tab content read can complete a
                                # recorded navigation that was still loading.
                                # The original observed destination is retained
                                # and revalidated; no click is replayed.
                                resumed = {**captured, "navigation_target": {**captured_target,
                                    "url": observation["url"], "status": "complete", "navigation_verified": True}}
                                nav_target = verified_navigation_target(resumed, source_tab_id=old["tab_id"],
                                    source_url=old["url"], tabs=current_tabs.get("tabs", []),
                                    frame_id=(captured.get("navigation_source") or {}).get("frame_id", 0))
                                guarded_transition = nav_target is not None and nav_target["tab_id"] == tab_id
                    if not guarded_transition:
                        raise BrowserControlError("browser_reconciliation_page_changed", "Fresh observation is from a different page than the uncertain action")
                if decision not in {"verified", "not_applied"} or not isinstance(explanation, str) or not 10 <= len(explanation) <= 2000:
                    raise BrowserControlError("browser_reconciliation_evidence_required", "Provide verified/not_applied plus specific observed evidence")
                saved = result(browser_id, tab_id, old["action"], data={"reconciliation": decision, "evidence": explanation}, operation_id=key,
                               executed=decision == "verified", verified=True, connection_generation=generation, observation_id=arguments["observation_id"])
                old_upload_target = json.loads(old["result"]).get("upload_target")
                if old_upload_target:
                    saved["upload_target"] = old_upload_target
                saved["status"] = "reconciled_" + decision
                saved["ok"] = True
                self.receipts.save(key, saved, expected_state=old["state"], expected_generation=old["generation"])
                return saved
            resource = self.resource(browser_id, tab_id)
            if write:
                pending_id = self.receipts.pending(resource)
                if pending_id:
                    prior = self.receipts.get(pending_id)
                    prior_value = json.loads(prior["result"]) if prior else {}
                    prior_data = prior_value.get("result")
                    prior_data = prior_data if isinstance(prior_data, dict) else {}
                    return self._with_pending_recovery(result(browser_id, tab_id, action, status="failed", code="browser_target_fenced",
                        message="A previous operation has an unconfirmed result. Read its status, inspect the exact target and reconcile it before another write; do not replay it.",
                        recovery={"blocking_operation_id": pending_id, "action": prior["action"] if prior else None,
                            "browser_id": browser_id, "tab_id": prior.get("tab_id") or prior_data.get("tab_id") if prior else tab_id,
                            "expected_generation": generation, "automatic_retry_allowed": False,
                            "next_steps": ["status", "read", "reconcile"]}), prior,
                        generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                if action == "upload_file":
                    upload_target = hashlib.sha256(json.dumps([browser_id, tab_id, generation, (target or {}).get("url", ""), arguments["selector"]]).encode()).hexdigest()
                    prior = self.receipts.prior_upload(browser_id, tab_id, generation, upload_target)
                    if prior:
                        raise BrowserControlError("upload_duplicate_prevented", "This file input already has a selected or uncertain upload; inspect its operation receipt and webpage before any new selection")
                    if not background and self.desktop_check is None:
                        raise BrowserControlError("upload_desktop_control_unavailable", "Upload requires the existing active desktop and UserTakeover preflight")
                if background or edge_background or browser_dom:
                    await check_background_desktop("before_dispatch")
                elif self.desktop_check is not None:
                    await asyncio.wait_for(self.desktop_check(), remaining())
            elif action == "screenshot" and not background and not edge_background and not browser_dom and self.desktop_check is not None:
                # Legacy capture activates a tab/window. Read classification is
                # not authority to take the foreground under isolated_only.
                await asyncio.wait_for(self.desktop_check(), remaining())
            if background:
                # A user can select the target or any other tab while the desktop
                # check runs. Reject that race before reserving or dispatching.
                await background_probe("pre_dispatch")
            if edge_background:
                await edge_probe("pre_dispatch")
            if write:
                pending = result(browser_id, tab_id, action, status="dispatching", executed=None,
                                 operation_id=operation_id, connection_generation=generation, **background_extra(), **({"upload_target": upload_target} if upload_target else {}))
                self.receipts.reserve(operation_id, fingerprint, browser_id, tab_id, generation, action, resource, str((target or {}).get("url", "")), pending)
                reserved = True
            payload = {key: value for key, value in arguments.items() if key not in {"expected_generation", "execution_mode"}}
            if background:
                # Internal fields are never accepted from the public caller.
                # The adapter consumes them and guards each native task again.
                payload.update(background_only=True, expected_active_tab_id=background_active_id)
            if edge_background:
                payload.update(background_only=True, expected_active_tabs=edge_initial["active_tabs"],
                    expected_focused_window_id=edge_initial["focused_window_id"])
                if action == "open_tab":
                    payload["active"] = False
                if independent_target:
                    payload.update(independent_target_guard=True, expected_target_window_id=edge_initial.get("target_window_id"))
                    if write and target:
                        payload["expected_target_url"] = target.get("url")
            if browser_dom:
                payload.update(browser_dom_only=True, require_inactive=not cooperative_tab)
                if cooperative_tab:
                    payload["cooperative_tab"] = True
                    if write and target:
                        payload["expected_target_url"] = target.get("url")
                if action == "open_tab":
                    payload["activate"] = False
            if action in {"debug_attach", "debug_detach"}:
                payload["confirm"] = confirm
            if tab_id:
                payload["tab_id"] = tab_id
            payload["timeout"] = max(1, int(remaining() * 1000))
            version = tuple(int(part) for part in str(browser.get("version", "")).split(".") if part.isdigit())
            if action == "click" and (edge_background or browser_dom) and len(version) == 3 and version >= (1, 9, 7):
                try:
                    baseline = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_downloads", {"limit": 100}, min(1, remaining())), min(1, remaining()))
                    entries = baseline.get("downloads") if isinstance(baseline, dict) else None
                    if isinstance(entries, list):
                        download_baseline_ids = {item["id"] for item in entries if isinstance(item, dict) and type(item.get("id")) is int}
                except Exception:
                    pass  # A read-only download probe cannot change click authority.
                click_dispatched_at = time.time()
                payload["timeout"] = max(1, int(remaining() * 1000))
            dispatch_budget = remaining()
            dispatched = True
            data = await asyncio.wait_for(self.dispatch(browser_id, generation, WIRE[action], payload, dispatch_budget), dispatch_budget)
            if (edge_background or browser_dom) and isinstance(data, dict):
                # Preserve exact created targets before post-dispatch awaits can
                # fail or be cancelled. Never retain raw page/input contents.
                native_diagnostics = {key: _receipt_data(data[key]) for key in
                    ("tab_id", "target_tab_id", "background_protocol", "dom_protocol", "action_may_have_run", "ok", "verified", "error",
                     "navigation_protocol", "navigation_source", "navigation_target", "operation_no_replay") if key in data}
            if browser_dom:
                if not isinstance(data, dict) or data.get("dom_protocol") != edge_background_policy.DOM_PROTOCOL:
                    raise BrowserControlError("browser_dom_response_unverified", "The extension did not return browser-only execution evidence")
                if write:
                    await check_background_desktop("after_dispatch")
            if edge_background:
                # Retain only a bounded target receipt if later state probes
                # fail. In particular, an uncertain new tab must be discoverable
                # and reconcilable without creating a second tab.
                created_id = str(data.get("tab_id", "")) if isinstance(data, dict) and action == "open_tab" else tab_id
                await edge_probe("post_dispatch", created_id, allow_missing=action == "close_tab")
                if write:
                    await check_background_desktop("after_dispatch")
                    await edge_probe("post_desktop_check", created_id, allow_missing=action == "close_tab")
                protocol = (data.get("background_protocol") or (data.get("background_execution") or {}).get("protocol")) if isinstance(data, dict) else None
                if protocol != edge_background_policy.PROTOCOL:
                    raise BrowserControlError("extension_background_response_unverified", "The Edge extension did not return guarded background execution evidence")
            if background:
                # Later state/desktop probes may fail after the adapter has
                # already returned a task receipt. Keep only safe diagnostics;
                # they never turn that guard failure into verified execution.
                native_diagnostics = _native_diagnostic_summary(data)
                await background_probe("post_dispatch")
                if write:
                    await check_background_desktop("after_dispatch")
                    await background_probe("post_desktop_check")
            if not isinstance(data, dict):
                raise BrowserControlError("browser_response_invalid", "Adapter did not return structured evidence")
            if (action == "read" and (edge_background or browser_dom) and data.get("ok") is not False
                    and str(data.get("extraction", "")).startswith("dom-text") and isinstance(data.get("url"), str)):
                fresh = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                owners = [item for item in fresh.get("tabs", []) if str(item.get("tab_id", item.get("id", ""))) == tab_id]
                main_frames = [item for item in data.get("frames", []) if isinstance(item, dict) and item.get("frame_id") == 0]
                mismatched_frame = bool(main_frames and main_frames[0].get("url") and main_frames[0]["url"] != data["url"])
                if len(owners) != 1 or owners[0].get("url") != data["url"] or mismatched_frame:
                    return result(browser_id, tab_id, action, status="failed", code="browser_page_changed_during_read",
                        message="The same tab navigated while its content was being read. Retry only this exact-tab read; no click is required.",
                        retryable=True, executed=False, verified=False,
                        recovery={"browser_id": browser_id, "tab_id": tab_id, "next_steps": ["read"],
                            "retry_read_only": True, "repeat_write_allowed": False})
            if action == "click" and (edge_background or browser_dom) and data.get("navigation_protocol") is not None:
                nav_listing = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                navigation_continuation = verified_navigation_target(data, source_tab_id=tab_id,
                    source_url=str((target or {}).get("url", "")), tabs=nav_listing.get("tabs", []),
                    frame_id=arguments.get("frame_id", 0), allow_active=cooperative_tab)
                for observation_attempt in range(3):
                    if navigation_continuation is not None:
                        break
                    recovered = current_navigation(data, source_tab_id=tab_id,
                        source_url=str((target or {}).get("url", "")), tabs=nav_listing.get("tabs", []),
                        frame_id=arguments.get("frame_id", 0), allow_active=cooperative_tab)
                    if recovered:
                        data = {**recovered["receipt"], "recovered_after_navigation": True,
                            "original_script_error": data.get("error")}
                        navigation_continuation = recovered["continuation"]
                        break
                    if observation_attempt < 2 and remaining() > .5:
                        await asyncio.sleep(.15)
                        nav_listing = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                if navigation_continuation and data.get("error"):
                    data = {**data, "ok": True, "verified": True, "recovered_after_navigation": True,
                        "original_script_error": data.get("error")}
                    data.pop("error", None)
            if action == "click" and (edge_background or browser_dom) and data.get("download_protocol") is not None:
                download_listing = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_downloads", {"limit": 100}, remaining()), remaining())
                download_confirmation = verified_download_record(data, source_tab_id=tab_id,
                    source_url=str((target or {}).get("url", "")), downloads=download_listing.get("downloads", []))
                if download_confirmation is None and click_dispatched_at is not None:
                    download_confirmation = current_download(data, source_tab_id=tab_id,
                        source_url=str((target or {}).get("url", "")), downloads=download_listing.get("downloads", []),
                        baseline_ids=download_baseline_ids, dispatched_at=click_dispatched_at)
                if download_confirmation:
                    complete = download_confirmation.get("state") == "complete"
                    data = {**data, "ok": True, "verified": True, "download": download_confirmation,
                        "download_started": True, "verification": "matching_download_record",
                        "recovered_after_download": bool(data.get("error")), "original_script_error": data.get("error"),
                        "download_complete": complete, "goal_download_complete": complete and download_confirmation.get("exists") is True}
                    data.pop("error", None)
            if data.get("ok") is False or data.get("error"):
                if edge_background or browser_dom:
                    failure = data.get("error") if isinstance(data.get("error"), dict) else {}
                    failure = dict(failure)
                    diagnostics = data.get("read_diagnostics")
                    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                    stages = diagnostics.get("stages")
                    stages = stages if isinstance(stages, list) else []
                    read_not_ready = (action == "read" and diagnostics.get("frame_count") == 0
                        and any(isinstance(stage, dict) and stage.get("error") == "browser_frame_not_ready" for stage in stages))
                    read_execution_timeout = (action == "read" and diagnostics.get("frame_count") == 0 and not read_not_ready
                        and any(isinstance(stage, dict) and stage.get("error") == "browser_frame_read_timeout" for stage in stages))
                    if read_not_ready:
                        failure.update(code="browser_page_not_ready", message="The browser has not exposed a readable frame for this exact tab yet. Wait briefly and retry only the read on the same tab; do not replay clicks or switch to desktop input.")
                    if read_execution_timeout:
                        failure.update(code="browser_page_execution_timeout", message="This tab's page-script channel did not respond, although the browser is connected. Retry only the read; if still unavailable, inspect tab metadata and open the exact current URL in a new inactive tab. Preserve the original page and never replay an uncertain write.")
                    no_effect = data.get("action_may_have_run") is False
                    failure_code = str(failure.get("code") or "extension_background_refused")
                    failure_message = failure.get("message")
                    if not failure_message:
                        if "target_hidden" in failure_code:
                            failure_message = "The addressed control is inside a hidden page section. Open its form panel and read the visible controls before writing. No field content was changed."
                        elif "readonly" in failure_code:
                            failure_message = "The addressed control is read-only. Inspect the page's editable control before writing. No field content was changed."
                        elif "result_missing" in failure_code or "response_unavailable" in failure_code or "script_execution_error" in failure_code:
                            failure_message = "The browser did not return a usable page-script result. The write outcome is unknown; inspect the exact field before any further write. This result does not establish a browser permission denial."
                        elif no_effect:
                            failure_message = "The page control validation failed before a mutation was dispatched. Inspect the returned control error and current visible page."
                        else:
                            failure_message = "The browser action has an unconfirmed result. Inspect the exact page and the original operation receipt before continuing."
                    response = result(browser_id, tab_id, action, status="failed" if no_effect or not write else "outcome_unknown",
                        data=data, code=failure_code,
                        message=str(failure_message),
                        executed=False if no_effect else None, operation_id=operation_id, retryable=read_not_ready or read_execution_timeout,
                        connection_generation=generation, **background_extra(),
                        **({"recovery": {"browser_id": browser_id, "tab_id": tab_id,
                            "next_steps": ["wait", "read"], "retry_read_only": True,
                            "repeat_write_allowed": False,
                            **({"suggested_next_tool": "browser_control", "fallback_action": "open_tab",
                                "fallback_arguments": {"url": str((target or {}).get("url", "")), "expected_generation": generation},
                                "fallback_confirm_required": True, "original_tab_preserved": True,
                                "fallback_scope": "fresh_url_document_not_original_live_dom"} if read_execution_timeout else {})}}
                           if read_not_ready or read_execution_timeout else {}))
                    if read_not_ready or read_execution_timeout:
                        response.update(suggested_next_tool="browser_control", continuation_required=True, repeat_write_allowed=False)
                    if no_effect and write:
                        response["recovery"] = {"browser_id": browser_id, "tab_id": tab_id,
                            "next_steps": ["read"], "write_dispatched": False,
                            "inspect_visible_control": True, "frame_id": arguments.get("frame_id", 0)}
                    elif write:
                        response["recovery"] = {"browser_id": browser_id, "tab_id": tab_id,
                            "blocking_operation_id": operation_id, "next_steps": ["status", "read", "reconcile"],
                            "repeat_write_allowed": False}
                    if write:
                        response = self._save_response(operation_id, response)
                    return self._with_pending_recovery(response, self.receipts.get(operation_id) if write else None,
                        generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                if browser.get("adapter") == "native_ipc":
                    failure = data.get("error") if isinstance(data.get("error"), dict) else {}
                    no_effect = data.get("native_dispatched") is False or data.get("action_may_have_run") is False
                    known_failure = action != "upload_file" and data.get("native_terminal") is True and data.get("native_state") in {"failed", "timed_out", "cancelled"}
                    known = no_effect or known_failure or not write
                    if background and write and data.get("action_may_have_run") is True:
                        # An adapter can observe a focus race that has already
                        # ended by this facade's next snapshot. Its uncertainty
                        # must survive even when the native task is terminal.
                        known, no_effect = False, False
                    response = result(browser_id, tab_id, action,
                        status="failed" if known else "outcome_unknown",
                        data=data, code=str(failure.get("code") or "native_operation_failed"),
                        message=str(failure.get("message") or "Native browser operation did not succeed"),
                        executed=False if no_effect else None,
                        verified=False, operation_id=operation_id,
                        native_task_id=data.get("native_task_id"), connection_generation=generation,
                        **background_extra(),
                        **({"upload_target": upload_target} if upload_target else {}))
                    if write:
                        response = self._save_response(operation_id, response)
                    return self._with_pending_recovery(response, self.receipts.get(operation_id) if write else None,
                        generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                raise BrowserControlError("browser_adapter_refused", "Adapter returned a real failure; inspect the current page before any new write", not write)
            verified = not write
            if write:
                verified = data.get("verified") is True and action != "press"
                if action == "click" and data.get("navigation_protocol") is not None and (edge_background or browser_dom):
                    verified = navigation_continuation is not None
                if action == "click" and data.get("download_protocol") is not None and (edge_background or browser_dom):
                    verified = download_confirmation is not None
                if (action == "press" and (edge_background or browser_dom) and data.get("verified") is True
                        and data.get("input_semantics") == "synthetic_dom_events_untrusted"
                        and data.get("action") == "press_key" and data.get("verification") == "observed_page_or_control_change"):
                    verified = True
                if action == "press" and background and browser.get("adapter") == "native_ipc":
                    # Only the fixed background DOM helper can prove a synthetic
                    # press effect. A native terminal task or bare verified flag
                    # cannot verify a physical keypress or the foreground path.
                    before, after = data.get("before"), data.get("after")
                    verified = (data.get("verified") is True
                                and data.get("helper") == "xiaoruo1-cef-background-interaction/1"
                                and data.get("input_semantics") == "synthetic_dom_events_untrusted"
                                and isinstance(before, dict) and isinstance(after, dict)
                                and before != after)
                if action in {"navigate", "open_tab", "close_tab", "activate_tab"}:
                    check = await asyncio.wait_for(self.dispatch(browser_id, generation, "list_tabs", {}, remaining()), remaining())
                    items = check.get("tabs", [])
                    actual_id = str(data.get("tab_id", tab_id or ""))
                    actual = [item for item in items if str(item.get("tab_id", item.get("id", ""))) == actual_id]
                    verified = not actual if action == "close_tab" else len(actual) == 1
                    if verified and action == "navigate":
                        verified = actual[0].get("url") == arguments["url"]
                        if (edge_background or browser_dom) and data.get("navigation_verified") is True:
                            verified = edge_background_policy.navigation_verified(data, arguments["url"], actual[0].get("url"), action=action,
                                protocol=edge_background_policy.DOM_PROTOCOL if browser_dom else edge_background_policy.PROTOCOL)
                    if verified and action == "open_tab" and arguments.get("url"):
                        verified = actual[0].get("url") == arguments["url"]
                        if (edge_background or browser_dom) and data.get("navigation_verified") is True:
                            verified = edge_background_policy.navigation_verified(data, arguments["url"], actual[0].get("url"), action=action,
                                protocol=edge_background_policy.DOM_PROTOCOL if browser_dom else edge_background_policy.PROTOCOL)
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
                              connection_generation=generation, retryable=False,
                              **background_extra(),
                              **({"upload_target": upload_target} if upload_target else {}))
            if navigation_continuation is not None:
                response["next_target"] = {"browser_id": browser_id,
                    "tab_id": navigation_continuation["tab_id"], "expected_generation": generation,
                    "read_required": True, "navigation_status": navigation_continuation["navigation_status"]}
            if download_confirmation is not None:
                response["download_verification"] = {"download_id": download_confirmation["id"],
                    "started": True, "complete": download_confirmation.get("state") == "complete",
                    "file_exists": download_confirmation.get("exists") is True,
                    "next_tool": "browser_list_downloads"}
            recovered_capture = (action == "task_status" and browser.get("adapter") == "native_ipc"
                                 and _recovered_native_screenshot(data, task_id=arguments.get("task_id")))
            if verified and ((action == "screenshot" and isinstance(data.get("path"), str)) or recovered_capture):
                response["artifacts"] = [{"path": data["path"], "mime_type": data.get("mime_type", "image/png"),
                    "size_bytes": data.get("size_bytes"), "private": True}]
            if action in {"read", "debug_snapshot"} and tab_id:
                target_exists = (data.get("exists") if presence_read and data.get("extraction") == "browser_tab_presence"
                    and data.get("dom_read") is False and data.get("tab_id") == tab_id else None)
                response["observation_id"] = self._observe(browser_id, tab_id, generation,
                    str(data.get("url") or (target or {}).get("url") or ""), target_exists=target_exists,
                    content_observed=action == "read" and data.get("dom_read") is not False
                        and bool(str(data.get("text") or "").strip() or data.get("interactive_elements")),
                    metadata_observed=action == "read" and data.get("extraction") == "browser_tab_metadata"
                        and data.get("dom_read") is False and data.get("tab_id") == tab_id)
            if not verified:
                response["error"] = {"code": "browser_verification_required", "message": "Action returned, but its effect is not confirmed; read this page and reconcile before another write"}
            if write:
                response = self._save_response(operation_id, response)
            return self._with_pending_recovery(response, self.receipts.get(operation_id) if write else self._pending_row(browser_id, tab_id) if action == "read" else None,
                generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
        except asyncio.CancelledError:
            if reserved and action in WRITE_ACTIONS and operation_id:
                try:
                    self.receipts.save(operation_id, result(browser_id, tab_id, action,
                        status="outcome_unknown" if dispatched else "failed", data=native_diagnostics,
                        code="browser_cancelled_after_dispatch" if dispatched else "browser_cancelled_before_dispatch",
                        message="Caller cancelled after dispatch; inspect the page rather than replaying" if dispatched else "Caller cancelled before the write was dispatched; no page action was sent",
                        executed=None if dispatched else False, operation_id=operation_id, connection_generation=generation,
                        **native_extra(), **background_extra(), **({"upload_target": upload_target} if upload_target else {})))
                except sqlite3.Error:
                    pass
            raise
        except Exception as exc:
            write_dispatched = dispatched and action in WRITE_ACTIONS
            uncertain = write_dispatched or ((background or edge_background) and dispatched)
            if isinstance(exc, BrowserControlError):
                code, message, retryable = exc.code, str(exc), exc.retryable
            elif isinstance(exc, UploadError):
                code, message, retryable = exc.code, str(exc), exc.code.startswith("upload_desktop")
            elif isinstance(exc, sqlite3.Error):
                code, message, retryable = "browser_receipt_database_busy", "Local operation receipts are unavailable; no automatic retry was attempted", not write_dispatched
            elif isinstance(exc, TimeoutError):
                code, message, retryable = "browser_timeout", "Browser operation exceeded its bounded timeout", not write_dispatched
            else:
                code, message, retryable = "browser_transport_unavailable", "Browser adapter disconnected or rejected the operation; inspect the current browser state", not write_dispatched
            value = result(browser_id, tab_id, action, status="outcome_unknown" if uncertain else "failed", data=native_diagnostics, code=code, message=message,
                           retryable=retryable and not uncertain, executed=None if uncertain else False,
                           operation_id=operation_id, connection_generation=generation,
                           **native_extra(), **background_extra(),
                           **({"upload_target": upload_target} if upload_target else {}))
            if reserved and operation_id:
                try:
                    value = self._save_response(operation_id, value)
                except sqlite3.Error:
                    pass
            if write_dispatched and operation_id:
                try:
                    return self._with_pending_recovery(value, self.receipts.get(operation_id),
                        generation=generation, execution_mode=arguments.get("execution_mode"))
                except sqlite3.Error:
                    pass
            if code == "browser_target_fenced" and generation:
                try:
                    duplicate = self.receipts.get(operation_id) if operation_id else None
                    if duplicate and duplicate["browser_id"] == browser_id:
                        if duplicate["fingerprint"] != fingerprint:
                            value["error"] = {"code": "browser_operation_conflict", "message": "This operation_id belongs to a different action or target"}
                        else:
                            return self._with_pending_recovery({**BrowserReceipts.public(duplicate), "duplicate_call": True, "redispatched": False}, duplicate,
                                generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                    return self._with_pending_recovery(value, self._pending_row(browser_id, tab_id),
                        generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                except sqlite3.Error:
                    pass
            if action == "reconcile" and generation and code in {"browser_reconciliation_observation_required", "browser_observed_state_acknowledgement_required", "browser_cancel_tracking_acknowledgement_required", "browser_inventory_acknowledgement_required", "browser_receipt_changed", "browser_generation_changed"}:
                try:
                    key = str(arguments.get("operation_id") or operation_id or "")
                    old = self.receipts.get(key)
                    if old and old["browser_id"] == browser_id:
                        return self._with_pending_recovery(value, old, generation=generation, browser=browser,
                            execution_mode=arguments.get("execution_mode"))
                except sqlite3.Error:
                    pass
            if (code == "browser_tab_not_owned" and tab_id and generation):
                try:
                    return self._with_pending_recovery(value, self._pending_row(browser_id, tab_id),
                        generation=generation, browser=browser, execution_mode=arguments.get("execution_mode"))
                except sqlite3.Error:
                    pass
            return value
        finally:
            if lock is not None and acquired:
                lock.release()
