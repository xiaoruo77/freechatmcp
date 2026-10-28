from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from web_config.settings import Settings


class BrowserBridgeError(RuntimeError):
    """Raised when the local user-session browser bridge cannot complete a call."""


class BrowserBridgeClient:
    """Authenticated facade from MCP to the existing interactive user session.

    Native Named Pipe discovery lives in that session's bridge, where LOCALAPPDATA
    and the protected discovery files belong to the logged-in user. The MCP
    Windows service must not guess another user's profile or copy IPC tokens.
    Edge continues to use the same extension transport behind this facade.
    """

    def __init__(self, settings: Settings):
        self.base_url = settings.browser_bridge_url.rstrip("/")
        self.token_file: Path = settings.browser_bridge_token_file

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BrowserBridgeError("Browser bridge token is not available") from exc
        if not token:
            raise BrowserBridgeError("Browser bridge token is empty")
        return token

    async def call(self, endpoint: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={"X-Xiaoruo1-Browser-Token": self._token()},
                timeout=httpx.Timeout(timeout),
                trust_env=False,
            ) as client:
                response = await client.post(endpoint, json=payload or {})
        except (httpx.HTTPError, BrowserBridgeError) as exc:
            if isinstance(exc, BrowserBridgeError):
                raise
            raise BrowserBridgeError(
                "本机 Edge 扩展桥接未启动或未连接。请先启动 Xiaoruo1 Browser Bridge，并在日常 Edge 中启用扩展。"
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:1000]}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise BrowserBridgeError(str(detail or f"Browser bridge HTTP {response.status_code}"))
        return body

    async def browser_instances(self, timeout: int = 10_000) -> dict[str, Any]:
        """Discover existing instances, with verified v72 native IPC preferred."""
        return await self.call("/v1/browsers", {"timeout": timeout}, timeout / 1000 + 5)

    async def browser_control(self, browser_id: str, action: str, tab_id: str | None = None,
                              arguments: dict[str, Any] | None = None, operation_id: str | None = None,
                              confirm: bool = False, timeout: int = 30_000) -> dict[str, Any]:
        """Dispatch to an explicit browser; task_status recovers a native task ID.

        The bridge waits for native asynchronous tasks and never falls back to
        an extension after an IPC operation may have entered the browser queue.
        """
        return await self.call("/v1/browser/dispatch", {"browser_id": browser_id,
            "action": action, "tab_id": tab_id, "arguments": arguments,
            "operation_id": operation_id, "confirm": confirm, "timeout": timeout}, timeout / 1000 + 5)

    async def list_tabs(self) -> dict[str, Any]:
        return await self.call("/v1/tabs")

    async def list_downloads(self, limit: int = 20) -> dict[str, Any]:
        return await self.call("/v1/downloads", {"limit": limit})

    async def open_tab(self, url: str | None = None) -> dict[str, Any]:
        return await self.call("/v1/tabs/open", {"url": url})

    async def navigate(self, tab_id: str, url: str, timeout: int = 30_000) -> dict[str, Any]:
        return await self.call("/v1/tabs/navigate", {"tab_id": tab_id, "url": url, "timeout": timeout}, timeout / 1000 + 5)

    async def read_page(
        self,
        tab_id: str | None = None,
        max_chars: int = 20_000,
        settle_ms: int = 750,
        include_frames: bool = False,
        selector: str | None = None,
        mode: str = "auto",
    ) -> dict[str, Any]:
        return await self.call(
            "/v1/tabs/read",
            {
                "tab_id": tab_id,
                "max_chars": max_chars,
                "settle_ms": settle_ms,
                "include_frames": include_frames,
                "selector": selector,
                "mode": mode,
            },
            timeout=50,
        )

    async def click(
        self,
        tab_id: str,
        by: str | None,
        selector: str | None,
        name: str | None,
        exact: bool,
        timeout: int,
    ) -> dict[str, Any]:
        return await self.call(
            "/v1/tabs/click",
            {"tab_id": tab_id, "by": by, "selector": selector, "name": name, "exact": exact, "timeout": timeout},
            timeout / 1000 + 10,
        )

    async def type_text(
        self,
        tab_id: str,
        text: str,
        by: str | None,
        selector: str | None,
        name: str | None,
        exact: bool,
        clear: bool,
        timeout: int,
    ) -> dict[str, Any]:
        return await self.call(
            "/v1/tabs/type",
            {
                "tab_id": tab_id,
                "text": text,
                "by": by,
                "selector": selector,
                "name": name,
                "exact": exact,
                "clear": clear,
                "timeout": timeout,
            },
            timeout / 1000 + 5,
        )

    async def press_key(
        self,
        tab_id: str,
        key: str,
        by: str | None,
        selector: str | None,
        name: str | None,
        exact: bool,
        timeout: int,
    ) -> dict[str, Any]:
        return await self.call(
            "/v1/tabs/press",
            {
                "tab_id": tab_id,
                "key": key,
                "by": by,
                "selector": selector,
                "name": name,
                "exact": exact,
                "timeout": timeout,
            },
            timeout / 1000 + 5,
        )

    async def select_option(
        self,
        tab_id: str,
        option: str,
        option_by: str,
        by: str | None,
        selector: str | None,
        name: str | None,
        exact: bool,
        timeout: int,
    ) -> dict[str, Any]:
        return await self.call(
            "/v1/tabs/select",
            {
                "tab_id": tab_id,
                "option": option,
                "option_by": option_by,
                "by": by,
                "selector": selector,
                "name": name,
                "exact": exact,
                "timeout": timeout,
            },
            timeout / 1000 + 5,
        )

    async def wait(self, tab_id: str, milliseconds: int = 1000) -> dict[str, Any]:
        return await self.call("/v1/tabs/wait", {"tab_id": tab_id, "milliseconds": milliseconds}, milliseconds / 1000 + 5)

    async def upload_file(self, tab_id: str, selector: str, file_path: str, confirm: bool = False,
                          timeout: int = 30_000, idempotency_key: str = "") -> dict[str, Any]:
        """Select validated media in an observed Edge input; not a platform receipt."""
        return await self.call("/v1/tabs/upload_file", {"tab_id": tab_id, "selector": selector,
            "file_path": file_path, "confirm": confirm, "timeout": timeout,
            "idempotency_key": idempotency_key}, timeout / 1000 + 20)

    async def upload_status(self, idempotency_key: str) -> dict[str, Any]:
        """Read the durable receipt without selecting or resending any file."""
        return await self.call("/v1/tabs/upload_status", {"idempotency_key": idempotency_key})

    async def screenshot(self, tab_id: str, full_page: bool = False) -> dict[str, Any]:
        return await self.call("/v1/tabs/screenshot", {"tab_id": tab_id, "full_page": full_page})

    async def close_tab(self, tab_id: str) -> dict[str, Any]:
        return await self.call("/v1/tabs/close", {"tab_id": tab_id})