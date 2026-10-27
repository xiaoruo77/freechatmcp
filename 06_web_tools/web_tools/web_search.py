"""Small, bounded public-web search helper for Xiaoruo1 self-checks.

The MCP server cannot invoke ChatGPT's internal web-search UI directly.  This
module therefore provides a narrow public search tool that the ChatGPT app can
call after a recoverable local error.  Queries are sanitized before they leave
the machine and the response contains only result metadata, never raw HTML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx


class WebSearchError(RuntimeError):
    """Raised when the bounded public search request cannot be completed."""


def _clean(value: Any, limit: int = 2_000) -> str:
    text = unescape(str(value or "")).replace("\ufffd", " ")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def sanitize_search_text(value: Any, limit: int = 240) -> str:
    """Remove local paths, credentials and opaque identifiers from a query."""

    text = str(value or "")
    text = re.sub(
        r"(?i)(?:[a-z]:[\\/]|\\\\|/mnt/[a-z]/|/home/|/users/)[^\s,;|]+",
        " ",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:token|secret|password|cookie|authorization|api[_ -]?key)\b\s*[=:]\s*[^\s,;|]+",
        " ",
        text,
    )
    text = re.sub(r"(?i)\b[0-9a-f]{24,}\b", " ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def build_search_query(
    operation: str = "",
    error_class: str = "",
    error_text: str = "",
) -> str:
    """Build a short public-safe query from a local failure checkpoint."""

    parts = [
        sanitize_search_text(operation, 80),
        sanitize_search_text(error_class, 80),
        sanitize_search_text(error_text, 140),
    ]
    query = " ".join(part for part in parts if part).strip()
    return query[:240] or "Xiaoruo1 runtime error troubleshooting"


class _BingResultsParser(HTMLParser):
    """Parse only Bing's result cards; no page text is returned to callers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._result_depth = 0
        self._title_depth = 0
        self._snippet_depth = 0
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._url = ""
        self.results: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            if self._result_depth:
                self._finish_result()
            self._result_depth = 1
            self._title = []
            self._snippet = []
            self._url = ""
            return
        if not self._result_depth:
            return
        self._result_depth += 1
        if tag == "h2":
            self._title_depth = self._result_depth
        elif tag == "p":
            self._snippet_depth = self._result_depth
        elif tag == "a" and self._title_depth:
            self._url = str(attributes.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if not self._result_depth:
            return
        if tag == "h2":
            self._title_depth = 0
        elif tag == "p":
            self._snippet_depth = 0
        self._result_depth -= 1
        if self._result_depth <= 0:
            self._finish_result()

    def handle_data(self, data: str) -> None:
        if not self._result_depth:
            return
        if self._title_depth:
            self._title.append(data)
        if self._snippet_depth:
            self._snippet.append(data)

    def _finish_result(self) -> None:
        title = _clean("".join(self._title), 500)
        snippet = _clean("".join(self._snippet), 1_200)
        url = _clean(self._url, 2_000)
        if title and url.startswith(("http://", "https://")):
            self.results.append({"title": title, "url": url, "snippet": snippet})
        self._result_depth = 0
        self._title_depth = 0
        self._snippet_depth = 0
        self._title = []
        self._snippet = []
        self._url = ""


def _domain_allowed(url: str, domains: list[str]) -> bool:
    if not domains:
        return True
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    for domain in domains:
        normalized = str(domain or "").strip().lower()
        normalized = normalized.removeprefix("https://").removeprefix("http://").split("/", 1)[0].rstrip(".")
        if normalized and (hostname == normalized or hostname.endswith("." + normalized)):
            return True
    return False


async def search_public_web(
    query: str,
    max_results: int = 5,
    domains: list[str] | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Search Bing and return a bounded, structured list of public results."""

    safe_query = sanitize_search_text(query, 240)
    if not safe_query:
        raise ValueError("query must contain public-safe search terms")
    limit = max(1, min(int(max_results), 8))
    timeout = max(5, min(int(timeout_seconds), 30))
    params = urlencode(
        {
            "q": safe_query,
            "count": str(limit),
            "setlang": "zh-CN",
            "cc": "CN",
            "form": "QBLH",
        }
    )
    url = "https://www.bing.com/search?" + params
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            trust_env=False,
            headers={
                "User-Agent": "Xiaoruo1/1.0 public-error-self-check",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise WebSearchError("公开网页搜索请求失败") from exc
    if response.status_code >= 400:
        raise WebSearchError(f"公开网页搜索返回 HTTP {response.status_code}")

    parser = _BingResultsParser()
    parser.feed(response.text)
    normalized_domains = [str(item) for item in (domains or []) if str(item).strip()]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parser.results:
        item_url = item["url"]
        if item_url in seen or not _domain_allowed(item_url, normalized_domains):
            continue
        seen.add(item_url)
        results.append(
            {
                **item,
                "domain": (urlparse(item_url).hostname or "").lower(),
            }
        )
        if len(results) >= limit:
            break

    return {
        "provider": "bing",
        "query": safe_query,
        "results": results,
        "result_count": len(results),
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "source_policy": "public result metadata only; local paths and secrets were redacted from the query",
    }