from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from web_config.settings import Settings


class BrowserBridgeError(RuntimeError):
    """Raised when the local user-session browser bridge cannot complete a call."""


class BrowserBridgeClient:
    """Small authenticated client used by the MCP process to reach the user session."""

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

    async def screenshot(self, tab_id: str, full_page: bool = False) -> dict[str, Any]:
        return await self.call("/v1/tabs/screenshot", {"tab_id": tab_id, "full_page": full_page})

    async def close_tab(self, tab_id: str) -> dict[str, Any]:
        return await self.call("/v1/tabs/close", {"tab_id": tab_id})