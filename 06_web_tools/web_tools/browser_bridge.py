from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.getenv("WEB_AGENT_DATA", str(PROJECT_ROOT / "08_web_data"))).expanduser().resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
SCREENSHOT_ROOT = DATA_ROOT / "browser_screenshots"
SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = Path(os.getenv("WEB_AGENT_BROWSER_TOKEN_FILE", str(DATA_ROOT / "browser_bridge.token"))).expanduser().resolve()
HOST = os.getenv("WEB_AGENT_BROWSER_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_AGENT_BROWSER_BRIDGE_PORT", "48272"))
LOG_FILE = Path(
    os.getenv("WEB_AGENT_BROWSER_LOG", str(DATA_ROOT.parent / "09_web_logs" / "browser_bridge.log"))
).expanduser().resolve()
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

_DOWNLOAD_NAME_PATTERN = re.compile(
    r"download|export|save|pdb|zip|mol2|gro|leap|structure|下载|导出|保存",
    re.IGNORECASE,
)
_DOWNLOAD_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_DOWNLOAD_FALLBACK_WAIT_SECONDS = 4.0


def _is_placeholder_duplicate_read(result: Any) -> bool:
    """Reject a duplicate-tab read that contains only the host app shell.

    ChatGPT can keep a streaming document alive while a temporary recovery tab
    returns only inline bootstrap JavaScript. Treating that shell as page text
    makes the web model believe that the page was successfully observed.
    """
    if not isinstance(result, dict) or result.get("recovery") != "duplicate_tab":
        return False
    text = str(result.get("text") or "").strip()
    if not text:
        return True
    shell_markers = (
        "document.currentScript",
        "__oai_logHTML",
        "requestAnimationFrame",
        "composer.first-prompt-input",
    )
    return len(text) < 1_000 and sum(marker in text for marker in shell_markers) >= 2


def _is_readable_browser_url(url: Any) -> bool:
    return bool(re.match(r"^(https?:|file:|about:blank$)", str(url or ""), re.IGNORECASE))


def _is_transient_browser_url(url: Any) -> bool:
    return bool(
        re.match(
            r"^(?:about:blank$|(?:chrome|edge)://newtab/?$|(?:chrome|edge)://new-tab-page/?$)",
            str(url or ""),
            re.IGNORECASE,
        )
    )


def _ensure_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


BRIDGE_TOKEN = _ensure_token()
logger = logging.getLogger("xiaoruo1.browser_bridge")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


class BrowserBridgeError(RuntimeError):
    """Raised when the Edge extension bridge cannot complete a call."""


class TabRequest(BaseModel):
    tab_id: str


class DownloadListRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class OpenTabRequest(BaseModel):
    url: str | None = None


class NavigateRequest(TabRequest):
    url: str = Field(min_length=1, max_length=4_000)
    timeout: int = Field(default=30_000, ge=1_000, le=120_000)


class ReadRequest(BaseModel):
    # When omitted, the extension reads the active tab in the last-focused
    # Edge window. Keeping this optional makes the MCP tool usable even when
    # a SPA changes its tab list while the model is composing a request.
    tab_id: str | None = None
    max_chars: int = Field(default=20_000, ge=100, le=100_000)
    settle_ms: int = Field(default=750, ge=0, le=5_000)
    include_frames: bool = False
    selector: str | None = Field(default=None, max_length=1_000)
    mode: str = Field(default="auto", pattern="^(auto|visible|fast)$")


class LocatorRequest(TabRequest):
    # Leave the strategy unspecified when callers provide only selector/name;
    # the Edge extension can then infer CSS or text without defaulting to role.
    by: str | None = Field(default=None, pattern="^(role|text|label|placeholder|css)$")
    selector: str | None = Field(default=None, max_length=2_000)
    name: str | None = Field(default=None, max_length=2_000)
    exact: bool = False
    timeout: int = Field(default=15_000, ge=500, le=120_000)


class TypeRequest(LocatorRequest):
    text: str = Field(max_length=100_000)
    clear: bool = True


class PressRequest(LocatorRequest):
    key: str = Field(min_length=1, max_length=100)


class SelectRequest(LocatorRequest):
    option: str = Field(min_length=1, max_length=2_000)
    option_by: str = Field(default="label", pattern="^(label|value|text|index)$")


_CSS_LOCATOR_MARKERS = ("#", ".", "[", "]", ">", "+", "~", "*", ":", "=", ",")
_KNOWN_ROLES = {
    "button",
    "link",
    "textbox",
    "checkbox",
    "radio",
    "combobox",
    "listbox",
    "option",
    "tab",
    "menuitem",
    "heading",
}


def _locator_payload(request: LocatorRequest) -> dict[str, Any]:
    """Normalize legacy selector-only calls before they reach the extension."""
    selector = (request.selector or "").strip()
    name = (request.name or "").strip()
    by = (request.by or "").strip().lower()
    if not by:
        if selector and any(marker in selector for marker in _CSS_LOCATOR_MARKERS):
            by = "css"
        elif name:
            by = "text"
        elif selector and selector.lower() in _KNOWN_ROLES:
            by = "role"
        elif selector:
            by = "text"
        else:
            by = "role"
    if by in {"text", "label", "placeholder"} and not selector and name:
        selector = name
    return {
        "tab_id": request.tab_id,
        "by": by,
        "selector": selector or None,
        "name": name or None,
        "exact": request.exact,
        "timeout": request.timeout,
    }


class WaitRequest(TabRequest):
    milliseconds: int = Field(default=1_000, ge=0, le=120_000)


class ScreenshotRequest(TabRequest):
    full_page: bool = False


def _validate_url(url: str | None) -> str | None:
    if url is None or not url.strip():
        return None
    value = url.strip()
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("Only http:// and https:// browser URLs are allowed")
    return value


def _edge_download_directories() -> list[Path]:
    """Resolve the active Edge download directory without assuming its path.

    Edge stores the user's chosen download directory in each profile's
    Preferences file. Reading that small metadata file avoids hard-coding
    ``Downloads`` and also works when the user keeps browser data on another
    drive. No cookies, history, or page data are read here.
    """
    candidates: list[Path] = []

    explicit = os.getenv("XIAORUO1_EDGE_DOWNLOAD_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        user_data = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    else:
        user_data = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"

    profile_dirs: list[Path] = []
    default_profile = user_data / "Default"
    if default_profile.is_dir():
        profile_dirs.append(default_profile)
    try:
        profile_dirs.extend(
            sorted(
                item
                for item in user_data.iterdir()
                if item.is_dir() and item.name.startswith("Profile ")
            )
        )
    except OSError:
        pass

    for profile in profile_dirs:
        preferences = profile / "Preferences"
        try:
            if not preferences.is_file() or preferences.stat().st_size > 4 * 1024 * 1024:
                continue
            data = json.loads(preferences.read_text(encoding="utf-8"))
            configured = ((data.get("download") or {}).get("default_directory") or "").strip()
            if configured:
                candidates.append(Path(configured).expanduser())
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            continue

    # This is only a fallback for a fresh profile before Edge has created its
    # Preferences file. It is not used in preference to the configured path.
    candidates.append(Path.home() / "Downloads")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = candidate.resolve(strict=False)
        except OSError:
            normalized = candidate
        key = os.path.normcase(str(normalized))
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return unique


def _download_record(path: Path, state: str = "complete") -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    digest = hashlib.sha1(str(path).encode("utf-8", errors="replace")).hexdigest()[:16]
    return {
        "id": f"local-{digest}",
        "state": state,
        "filename": str(path),
        "file_path": str(path),
        "name": path.name,
        "url": "",
        "final_url": "",
        "mime": "",
        "danger": "safe",
        "exists": True,
        "bytes_received": int(stat.st_size),
        "total_bytes": int(stat.st_size),
        "last_modified": float(stat.st_mtime),
        "source": "edge_download_directory",
    }


def _local_download_snapshot() -> dict[str, tuple[int, int]]:
    """Return path -> (size, mtime_ns) for recent Edge download files."""
    now = time.time()
    snapshot: dict[str, tuple[int, int]] = {}
    for root in _edge_download_directories():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.is_file() or item.name.startswith("."):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            if now - stat.st_mtime > _DOWNLOAD_MAX_AGE_SECONDS:
                continue
            snapshot[str(item.resolve(strict=False))] = (int(stat.st_size), int(stat.st_mtime_ns))
    return snapshot


def _local_downloads(limit: int = 20) -> list[dict[str, Any]]:
    """List recent completed/active files in Edge's configured download dir."""
    now = time.time()
    records: list[dict[str, Any]] = []
    for root in _edge_download_directories():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.is_file() or item.name.startswith("."):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            if now - stat.st_mtime > _DOWNLOAD_MAX_AGE_SECONDS:
                continue
            state = "in_progress" if item.name.lower().endswith(".crdownload") else "complete"
            record = _download_record(item, state)
            if record:
                records.append(record)
    records.sort(key=lambda item: float(item.get("last_modified") or 0), reverse=True)
    return records[: max(1, min(int(limit), 100))]


def _new_local_downloads(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[dict[str, Any]]:
    changed = [
        path
        for path, signature in after.items()
        if path not in before or before[path] != signature
    ]
    records: list[dict[str, Any]] = []
    for path in changed:
        item = Path(path)
        state = "in_progress" if item.name.lower().endswith(".crdownload") else "complete"
        record = _download_record(item, state)
        if record:
            records.append(record)
    records.sort(key=lambda item: float(item.get("last_modified") or 0), reverse=True)
    return records


def _wait_for_new_local_downloads(
    before: dict[str, tuple[int, int]],
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    latest: list[dict[str, Any]] = []
    while time.monotonic() <= deadline:
        latest = _new_local_downloads(before, _local_download_snapshot())
        if any(item.get("state") == "complete" for item in latest):
            return latest
        time.sleep(0.2)
    return latest


def _download_locator_likely(payload: dict[str, Any], result: Any) -> bool:
    values = [
        payload.get("selector"),
        payload.get("name"),
        result.get("verification") if isinstance(result, dict) else None,
        result.get("target", {}).get("text") if isinstance(result, dict) and isinstance(result.get("target"), dict) else None,
    ]
    return bool(_DOWNLOAD_NAME_PATTERN.search(" ".join(str(value) for value in values if value)))


def _attach_local_download(result: Any, downloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose a filesystem-observed download while preserving click output."""
    primary = downloads[0]
    if isinstance(result, dict):
        enriched = dict(result)
        existing = enriched.get("download")
        if not isinstance(existing, dict) or not Path(str(existing.get("file_path") or "")).exists():
            enriched["download"] = primary
        enriched["downloads"] = downloads
        enriched.setdefault("verification", "download_observed_in_edge_download_directory")
        return enriched
    return {
        "result": result,
        "download": primary,
        "downloads": downloads,
        "verification": "download_observed_in_edge_download_directory",
    }


class EdgeExtensionBridge:
    """Request/response bridge between MCP and the user's Edge extension.

    The extension owns the browser session and therefore reuses the user's
    normal Edge profile and login state. This process never reads browser
    cookies, passwords, or profile databases.
    """

    def __init__(self) -> None:
        self._websocket: WebSocket | None = None
        self._connection_id: str | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._connected_at: str | None = None

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    async def attach(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection_id = uuid.uuid4().hex
        async with self._state_lock:
            old = self._websocket
            self._websocket = websocket
            self._connection_id = connection_id
            self._connected_at = str(asyncio.get_running_loop().time())
        if old is not None:
            try:
                await old.close(code=4001, reason="replaced by a newer Edge extension connection")
            except Exception:
                pass
        logger.info("edge_extension_connected connection_id=%s", connection_id)
        await self._send({"type": "hello", "server": "xiaoruo1", "version": "2.0.0"})
        try:
            while True:
                message = await websocket.receive_json()
                await self._receive(message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("edge_extension_receive_error connection_id=%s error=%s", connection_id, type(exc).__name__)
        finally:
            await self.detach(websocket, connection_id)

    async def detach(self, websocket: WebSocket, connection_id: str | None = None) -> None:
        async with self._state_lock:
            if websocket is not self._websocket:
                return
            if connection_id is not None and connection_id != self._connection_id:
                return
            self._websocket = None
            self._connection_id = None
            self._connected_at = None
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(BrowserBridgeError("Edge 扩展桥接已断开"))
        logger.info("edge_extension_disconnected")

    async def _send(self, message: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None:
            raise BrowserBridgeError("Edge 扩展未连接。请先在日常 Edge 中加载并启用 Xiaoruo1 Edge Bridge 扩展")
        async with self._send_lock:
            try:
                await websocket.send_json(message)
            except Exception as exc:
                raise BrowserBridgeError("无法向 Edge 扩展发送指令") from exc

    async def _receive(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") == "hello_ack":
            logger.info("edge_extension_ready version=%s", message.get("version", "unknown"))
            return
        request_id = str(message.get("id", ""))
        future = self._pending.get(request_id)
        if future is None:
            return
        if not future.done():
            future.set_result(message)

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if self._websocket is None:
            raise BrowserBridgeError("Edge 扩展未连接。请先在日常 Edge 中加载并启用 Xiaoruo1 Edge Bridge 扩展")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            response = await asyncio.wait_for(future, timeout=max(1.0, timeout))
        except asyncio.TimeoutError as exc:
            raise BrowserBridgeError(f"Edge 扩展执行超时: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        if not response.get("ok", False):
            raise BrowserBridgeError(str(response.get("error") or f"Edge 扩展执行失败: {method}"))
        return response.get("result")

    async def list_tabs(self) -> dict[str, Any]:
        result = await self.call("list_tabs")
        return {"browser": "edge-extension", "tabs": result.get("tabs", []) if isinstance(result, dict) else result}

    async def list_downloads(self, request: DownloadListRequest) -> dict[str, Any]:
        try:
            result = await self.call("list_downloads", {"limit": request.limit})
            if isinstance(result, dict) and isinstance(result.get("downloads"), list):
                # A new extension returns the authoritative Chromium download
                # records. Keep that result when it is available.
                return result
        except Exception as exc:
            # Older unpacked extensions do not know list_downloads yet. The
            # bridge can still read the configured Edge download directory and
            # return the real file path, so a stale extension does not make a
            # completed download look missing to the web model.
            logger.info("edge_download_api_fallback error=%s", type(exc).__name__)
        return {
            "browser": "edge-extension-filesystem-fallback",
            "downloads": await asyncio.to_thread(_local_downloads, request.limit),
        }

    async def open_tab(self, url: str | None) -> dict[str, Any]:
        return await self.call("open_tab", {"url": _validate_url(url)})

    async def navigate(self, request: NavigateRequest) -> dict[str, Any]:
        target = _validate_url(request.url)
        assert target is not None
        return await self.call(
            "navigate",
            {"tab_id": request.tab_id, "url": target, "timeout": request.timeout},
            request.timeout / 1000 + 5,
        )

    async def read(self, request: ReadRequest) -> dict[str, Any]:
        if request.settle_ms:
            await asyncio.sleep(request.settle_ms / 1000)
        last_error: BrowserBridgeError | None = None
        # A Manifest V3 service worker can be woken up or reconnected between
        # two MCP calls. A short retry here avoids turning that normal browser
        # lifecycle event into a user-visible MCP failure.
        result: Any = None
        for attempt in range(4):
            try:
                result = await self.call(
                    "read_page",
                    {
                        "tab_id": request.tab_id,
                        "max_chars": request.max_chars,
                        "settle_ms": request.settle_ms,
                        "include_frames": request.include_frames,
                        "selector": request.selector,
                        "mode": request.mode,
                    },
                    timeout=20,
                )
                if isinstance(result, dict):
                    result.setdefault("bridge_attempts", attempt + 1)
                    self._add_known_interactive_metadata(result)
                    if _is_placeholder_duplicate_read(result):
                        last_error = BrowserBridgeError(
                            "页面仍在加载；临时恢复标签只返回了页面脚本壳，未返回可读正文"
                        )
                        if attempt < 1:
                            await asyncio.sleep(1.0)
                            continue
                        raise last_error
                if result and (not isinstance(result, dict) or result.get("text")):
                    return result
            except BrowserBridgeError as exc:
                last_error = exc
                # Page-level failures (for example a protected edge:// page
                # or a script injection error) are deterministic and should
                # be returned immediately. Only retry bridge lifecycle errors
                # where the extension may be reconnecting between calls.
                message = str(exc)
                retryable = any(
                    marker in message
                    for marker in (
                        "桥接已断开",
                        "无法向 Edge 扩展发送指令",
                        "Edge 扩展未连接",
                    )
                )
                if "安全边界禁止读取此页面类型" in message and attempt < 3:
                    # A newly-created tab can report chrome://newtab for one
                    # bridge turn even though the visible tab has already
                    # committed its requested HTTPS navigation. Re-query the
                    # authoritative tab list before deciding that this is a
                    # protected page. This remains safe because an actual
                    # edge://extensions or other internal URL is retried
                    # neither here nor in the extension.
                    try:
                        listed = await self.list_tabs()
                        tabs = listed.get("tabs", []) if isinstance(listed, dict) else []
                        current = next(
                            (
                                item
                                for item in tabs
                                if str(item.get("tab_id") or item.get("id") or "") == str(request.tab_id)
                            ),
                            None,
                        )
                        current_url = str((current or {}).get("url") or "")
                        if _is_readable_browser_url(current_url) or _is_transient_browser_url(current_url):
                            await asyncio.sleep(0.4 * (attempt + 1))
                            continue
                    except Exception:
                        pass
                if not retryable or attempt >= 1:
                    if "安全边界禁止读取此页面类型" not in message or attempt >= 3:
                        raise
            if attempt < 1:
                await asyncio.sleep(0.75 * (attempt + 1))
        if last_error is not None:
            raise last_error
        return result if isinstance(result, dict) else {"text": "", "truncated": False}

    @staticmethod
    def _add_known_interactive_metadata(result: dict[str, Any]) -> None:
        """Keep tool discovery useful while an already-loaded unpacked extension is stale.

        The GLYCAM text page has stable public control ids. An unpacked Edge
        extension may keep its old content script in an existing tab until the
        extension is manually reloaded, so expose the same safe, non-value
        metadata at the bridge boundary during that transition.
        """
        url = str(result.get("url") or "").lower()
        if "glycam.org/txt" not in url:
            return
        controls = result.get("interactive_elements")
        if not isinstance(controls, list):
            controls = []
        known = {str(item.get("id") or "") for item in controls if isinstance(item, dict)}
        if "sequence_input" not in known:
            controls.insert(
                0,
                {
                    "index": 0,
                    "tag": "input",
                    "id": "sequence_input",
                    "name": "sequence",
                    "type": "text",
                    "role": "textbox",
                    "aria_label": "",
                    "placeholder": "",
                    "text": "",
                    "selector": "#sequence_input",
                    "disabled": False,
                },
            )
        if "txt_submit" not in known:
            controls.append(
                {
                    "index": len(controls),
                    "tag": "button",
                    "id": "txt_submit",
                    "name": "",
                    "type": "submit",
                    "role": "button",
                    "aria_label": "",
                    "placeholder": "",
                    "text": "Submit",
                    "selector": "#txt_submit",
                    "disabled": False,
                },
            )
        result["interactive_elements"] = controls[:100]

    async def _rewrite_legacy_generic_locator(self, request: LocatorRequest, payload: dict[str, Any]) -> dict[str, Any]:
        """Map the old model-generated generic GLYCAM locators to stable ids.

        Older ChatGPT tool calls often tried `textarea`, `input`, or the
        `textbox` role after reading only page text. On the GLYCAM text page the
        actual visible control is an input with id `sequence_input`; hidden CSRF
        and login fields make a raw `input` CSS query unreliable.
        """
        selector = str(payload.get("selector") or "").strip().lower()
        by = str(payload.get("by") or "").strip().lower()
        name = str(payload.get("name") or "").strip().lower()
        generic = selector in {"input", "textarea", "textbox", "[role='textbox']", "[role=\"textbox\"]"} or (
            by == "role" and selector == "textbox"
        ) or (name == "textbox")
        if not generic:
            return payload
        try:
            tabs = await self.list_tabs()
        except Exception:
            return payload
        tab_id = str(request.tab_id)
        tab = next((item for item in tabs.get("tabs", []) if str(item.get("tab_id")) == tab_id), None)
        url = str((tab or {}).get("url") or "").lower()
        if "glycam.org/txt" not in url:
            return payload
        rewritten = dict(payload)
        rewritten.update({"by": "css", "selector": "#sequence_input", "name": None})
        return rewritten

    async def click(self, request: LocatorRequest) -> dict[str, Any]:
        payload = _locator_payload(request)
        before = await asyncio.to_thread(_local_download_snapshot)
        try:
            result = await self.call(
                "click",
                payload,
                request.timeout / 1000 + 10,
            )
        except BrowserBridgeError as exc:
            # A stale extension can report a page-script timeout after the
            # click has already started an asynchronous Blob download. Give
            # the configured Edge download directory a chance to prove that
            # the user-visible action succeeded before surfacing the timeout.
            if _download_locator_likely(payload, None):
                local_downloads = await asyncio.to_thread(
                    _wait_for_new_local_downloads,
                    before,
                    _DOWNLOAD_FALLBACK_WAIT_SECONDS,
                )
                if local_downloads:
                    return _attach_local_download(
                        {
                            "verification": "download_observed_after_extension_error",
                            "bridge_warning": str(exc),
                        },
                        local_downloads,
                    )
            message = str(exc)
            # A locator/DOM execution error means the click was not dispatched
            # at all.  Do not downgrade it to the legacy "click sent" retry
            # checkpoint: that made callers wait/retry forever against a
            # missing or stale element and hid the real failure cause.
            if "执行层错误：" in message or "找不到可点击目标" in message or "找不到可操作的可见目标" in message:
                raise
            if "点击已发送，但" in message:
                # The page action was dispatched, but a slow SPA did not
                # expose a changed URL/text/target within the extension's
                # short verification window. Take one bounded observation
                # here before returning. This keeps the old ChatGPT app
                # snapshot useful even though it does not know the newer
                # browser_wait/browser_read_page continuation rule.
                response: dict[str, Any] = {
                    "verification": "click_sent_no_immediate_state_change",
                    "state_change_observed": False,
                    "follow_up_required": True,
                    "bridge_warning": message,
                }
                try:
                    after_click = await asyncio.wait_for(
                        self.read(
                            ReadRequest(
                                tab_id=request.tab_id,
                                max_chars=8_000,
                                settle_ms=0,
                                include_frames=False,
                                mode="auto",
                            )
                        ),
                        timeout=6,
                    )
                    response["after_click"] = after_click
                except Exception as read_exc:
                    response["after_click_read_error"] = f"{type(read_exc).__name__}: {read_exc}"
                return response
            raise
        extension_download = result.get("download") if isinstance(result, dict) else None
        extension_path = str(extension_download.get("file_path") or "") if isinstance(extension_download, dict) else ""
        extension_path_exists = bool(extension_path and Path(extension_path).exists())
        likely_download = _download_locator_likely(payload, result)
        local_downloads: list[dict[str, Any]] = []
        if likely_download and not extension_path_exists:
            local_downloads = await asyncio.to_thread(
                _wait_for_new_local_downloads,
                before,
                _DOWNLOAD_FALLBACK_WAIT_SECONDS,
            )
        elif not extension_path_exists:
            local_downloads = _new_local_downloads(before, _local_download_snapshot())
        if local_downloads:
            return _attach_local_download(result, local_downloads)
        return result

    async def type_text(self, request: TypeRequest) -> dict[str, Any]:
        payload = _locator_payload(request)
        payload = await self._rewrite_legacy_generic_locator(request, payload)
        payload.update({"text": request.text, "clear": request.clear})
        return await self.call(
            "type_text",
            payload,
            request.timeout / 1000 + 5,
        )

    async def press_key(self, request: PressRequest) -> dict[str, Any]:
        payload = _locator_payload(request)
        payload["key"] = request.key
        try:
            return await self.call(
                "press_key",
                payload,
                request.timeout / 1000 + 5,
            )
        except BrowserBridgeError as exc:
            # An already-running unpacked Edge extension can keep its old
            # service worker alive after the bridge source is upgraded. Use
            # the content-script reader as a compatibility path so keyboard
            # controls remain available without restarting Edge or losing
            # the user's tabs. A page reload refreshes reader.js from disk.
            if "Unknown Edge bridge command: press_key" not in str(exc):
                raise
            fallback = await self.call(
                "read_page",
                {
                    "tab_id": request.tab_id,
                    "max_chars": 100,
                    "settle_ms": 0,
                    "selector": "__xiaoruo1_press__:" + request.key,
                    "mode": "visible",
                },
                request.timeout / 1000 + 5,
            )
            if not isinstance(fallback, dict) or fallback.get("pressed") is not True:
                raise BrowserBridgeError("键盘按键已发送，但桥接回退路径未返回成功校验")
            return {"tab_id": request.tab_id, "pressed": True, "verified": True, "fallback": "reader_content_script", "target": fallback}

    async def select_option(self, request: SelectRequest) -> dict[str, Any]:
        payload = _locator_payload(request)
        payload.update({"option": request.option, "option_by": request.option_by})
        return await self.call(
            "select_option",
            payload,
            request.timeout / 1000 + 5,
        )

    async def wait(self, request: WaitRequest) -> dict[str, Any]:
        return await self.call("wait", {"tab_id": request.tab_id, "milliseconds": request.milliseconds}, request.milliseconds / 1000 + 5)

    async def screenshot(self, request: ScreenshotRequest) -> dict[str, Any]:
        result = await self.call("screenshot", {"tab_id": request.tab_id, "full_page": request.full_page}, 30)
        if not isinstance(result, dict) or not result.get("data_url"):
            raise BrowserBridgeError("Edge 扩展没有返回截图")
        data_url = str(result["data_url"])
        try:
            _, encoded = data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
        except (ValueError, binascii.Error) as exc:
            raise BrowserBridgeError("Edge 扩展返回了无效截图") from exc
        file_path = SCREENSHOT_ROOT / f"{request.tab_id}-{uuid.uuid4().hex}.png"
        file_path.write_bytes(image_bytes)
        return {
            "tab_id": request.tab_id,
            "path": str(file_path),
            "url": result.get("url", ""),
            "mime_type": "image/png",
            "full_page": False,
        }

    async def close_tab(self, request: TabRequest) -> dict[str, Any]:
        return await self.call("close_tab", {"tab_id": request.tab_id})


bridge = EdgeExtensionBridge()


def require_bridge_token(value: str | None = Header(default=None, alias="X-Xiaoruo1-Browser-Token")) -> None:
    if not value or not hmac.compare_digest(value, BRIDGE_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Valid local browser bridge token required")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if bridge._websocket is not None:
        try:
            await bridge._websocket.close(code=1001, reason="server shutdown")
        except Exception:
            pass


app = FastAPI(title="Xiaoruo1 Edge Extension Bridge", version="2.0.0", lifespan=lifespan)


@app.websocket("/ws")
async def extension_websocket(websocket: WebSocket) -> None:
    # The WebSocket is loopback-only at the process level and also requires
    # the same random token used by the MCP-side HTTP client. No page cookies
    # or browser profile data cross this socket.
    token = websocket.query_params.get("token", "")
    origin = websocket.headers.get("origin", "")
    if not token or not hmac.compare_digest(token, BRIDGE_TOKEN) or not origin.startswith("chrome-extension://"):
        await websocket.close(code=1008, reason="extension authentication required")
        return
    await bridge.attach(websocket)


@app.get("/health", dependencies=[Depends(require_bridge_token)])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "xiaoruo1_edge_extension_bridge",
        "host": HOST,
        "port": PORT,
        "extension_connected": bridge.connected,
    }


@app.post("/v1/tabs", dependencies=[Depends(require_bridge_token)])
async def list_tabs() -> dict[str, Any]:
    return await bridge.list_tabs()


@app.post("/v1/downloads", dependencies=[Depends(require_bridge_token)])
async def list_downloads(body: DownloadListRequest) -> dict[str, Any]:
    try:
        return await bridge.list_downloads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/open", dependencies=[Depends(require_bridge_token)])
async def open_tab(body: OpenTabRequest) -> dict[str, Any]:
    try:
        return await bridge.open_tab(body.url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/navigate", dependencies=[Depends(require_bridge_token)])
async def navigate(body: NavigateRequest) -> dict[str, Any]:
    try:
        return await bridge.navigate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/read", dependencies=[Depends(require_bridge_token)])
async def read_page(body: ReadRequest) -> dict[str, Any]:
    try:
        return await bridge.read(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/click", dependencies=[Depends(require_bridge_token)])
async def click(body: LocatorRequest) -> dict[str, Any]:
    try:
        return await bridge.click(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/type", dependencies=[Depends(require_bridge_token)])
async def type_text(body: TypeRequest) -> dict[str, Any]:
    try:
        return await bridge.type_text(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/press", dependencies=[Depends(require_bridge_token)])
async def press_key(body: PressRequest) -> dict[str, Any]:
    try:
        return await bridge.press_key(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/select", dependencies=[Depends(require_bridge_token)])
async def select_option(body: SelectRequest) -> dict[str, Any]:
    try:
        return await bridge.select_option(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/wait", dependencies=[Depends(require_bridge_token)])
async def wait(body: WaitRequest) -> dict[str, Any]:
    try:
        return await bridge.wait(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/screenshot", dependencies=[Depends(require_bridge_token)])
async def screenshot(body: ScreenshotRequest) -> dict[str, Any]:
    try:
        return await bridge.screenshot(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/tabs/close", dependencies=[Depends(require_bridge_token)])
async def close_tab(body: TabRequest) -> dict[str, Any]:
    try:
        return await bridge.close_tab(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def main() -> None:
    # The scheduled task uses pythonw.exe, whose stdout/stderr are None. The
    # default Uvicorn formatter calls isatty() on stdout, so disable console
    # logging and keep bridge events in LOG_FILE.
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()