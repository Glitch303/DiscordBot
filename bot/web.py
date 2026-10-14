"""Web search via SearXNG and URL content extraction."""
from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("bot.web")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SEARCH_RESULTS_PER_QUERY = 5
SEARCH_SNIPPET_CHARS = 400
EXTRACT_MAX_CHARS = 12_000


class Web:
    def __init__(self, searxng_url: str, timeout: float = 15.0):
        self.searxng_url = searxng_url
        self.timeout = timeout
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> httpx.Response:
        """Raw GET (for binary/JSON consumers like the media tools). Caller raises for status."""
        return self._client.get(url, params=params, timeout=timeout)

    def search(self, query: str, max_results: int = SEARCH_RESULTS_PER_QUERY) -> str:
        """Search the web; return a formatted result list for the model."""
        try:
            r = self._client.get(
                self.searxng_url,
                params={"q": query, "format": "json", "safesearch": 1},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return f"[web_search] search failed: {e}"

        results = (data.get("results") or [])[:max_results]
        if not results:
            return f"[web_search] no results for: {query}"

        lines = [f"[web_search] results for: {query}"]
        for i, res in enumerate(results, 1):
            title = (res.get("title") or "").strip()
            url = (res.get("url") or "").strip()
            snippet = re.sub(r"\s+", " ", (res.get("content") or "")).strip()[:SEARCH_SNIPPET_CHARS]
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
        lines.append("(use fetch_url to read the full page of any result)")
        return "\n".join(lines)

    def fetch(self, url: str, max_chars: int = EXTRACT_MAX_CHARS) -> str:
        """Fetch a URL and return readable text (title + body, stripped)."""
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return f"[fetch_url] failed to fetch {url}: {e}"

        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return f"[fetch_url] {url} is not text/html (content-type: {ctype})"

        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()

        title = (soup.title.get_text(strip=True) if soup.title else "") or url
        text = re.sub(r"[ \t]+", " ", soup.get_text("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[truncated]"
        return f"[fetch_url] {title}\n{url}\n\n{text}"
