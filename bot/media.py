"""Outbound media: files, GIFs and stickers the bot can send back to Discord.

The agent loop runs in a worker thread (see llm.run_agent), so the media tools
here only *queue* sends onto ``MediaContext.sends``. After the agent returns,
BotClient flushes the queue to the channel (files as attachments, stickers via
the Discord sticker API).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .web import Web

log = logging.getLogger("bot.media")

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
KLIPY_SEARCH_URL = "https://api.klipy.com/api/v1/{key}/gifs/search"
GIF_KEY_HELP = (
    "GIF search is not configured: set GIPHY_API_KEY (free: https://developers.giphy.com/) "
    "and/or KLIPY_API_KEY (free: https://klipy.com/developers) in .env. Do not web-search "
    "for GIFs (found URLs are unreliable) — either ask the user to send the .gif directly, "
    "or say GIF search needs an API key configured."
)

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/html": ".html",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}


@dataclass
class MediaSend:
    """One queued outbound item, flushed by BotClient after the agent returns."""
    kind: str                      # "file" | "sticker"
    caption: str | None = None
    path: Path | None = None       # kind == "file"
    filename: str | None = None    # kind == "file" (display name)
    sticker_id: str | None = None  # kind == "sticker" (snowflake as string)


class StickerIndex:
    """Cached index of Discord's official sticker packs.

    Fetched once via ``client.fetch_premium_sticker_packs()`` (works for bots,
    no Nitro needed) and cached on disk so searches stay fast and survive
    API hiccups. Server stickers are layered in per-search.
    """

    REFRESH_SECONDS = 6 * 3600  # repull pack list at most every 6 h

    def __init__(self, client, loop: asyncio.AbstractEventLoop, cache_path: Path | None):
        self._client = client
        self._loop = loop
        self._cache_path = cache_path
        self._entries: list[dict] = []
        self._fetched_at = 0.0
        if cache_path and cache_path.exists():
            try:
                self._entries = json.loads(cache_path.read_text())
                self._fetched_at = time.time()
            except (OSError, ValueError) as e:
                log.warning("sticker cache unreadable, will refetch: %s", e)

    def ensure(self) -> list[dict]:
        """Return entries, refreshing from the API when stale (falls back to stale)."""
        if self._entries and time.time() - self._fetched_at < self.REFRESH_SECONDS:
            return self._entries
        try:
            self._refresh()
        except Exception as e:  # network/loop failure — stale beats nothing
            if self._entries:
                log.warning("sticker pack refresh failed, using stale cache: %s", e)
            else:
                raise
        return self._entries

    def _refresh(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            self._client.fetch_premium_sticker_packs(), self._loop
        )
        packs = fut.result(timeout=15)
        entries = [
            {"id": str(s.id), "name": s.name, "pack": p.name}
            for p in packs
            for s in p.stickers
        ]
        if entries:
            self._entries = entries
            self._fetched_at = time.time()
            if self._cache_path:
                try:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_text(json.dumps(entries))
                except OSError as e:
                    log.warning("sticker cache write failed: %s", e)

    def search(
        self,
        query: str,
        guild_stickers: list | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Search official packs + the current server's stickers.

        Returns [{"id", "name", "pack"}] ranked: name match first, then pack.
        """
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        scored: list[tuple[int, int, dict]] = []
        for e in self.ensure():
            name_hay, full_hay = e["name"].lower(), f"{e['name']} {e['pack']}".lower()
            if all(t in name_hay for t in terms):
                scored.append((0, len(e["name"]), e))
            elif all(t in full_hay for t in terms):
                scored.append((1, len(e["name"]), e))
        for gs in guild_stickers or []:
            name = gs.name.lower()
            if all(t in name for t in terms):
                scored.append((0, len(gs.name), {"id": str(gs.id), "name": gs.name, "pack": "server sticker"}))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [e for _, _, e in scored[:limit]]


class MediaContext:
    """Per-message state shared by the media tools. Built by BotClient."""

    def __init__(
        self,
        web: Web,
        downloads_dir: Path,
        max_bytes: int,
        client,
        loop: asyncio.AbstractEventLoop,
        giphy_api_key: str | None = None,
        klipy_api_key: str | None = None,
        gif_provider: str = "auto",
        guild_stickers: list | None = None,
        sticker_cache_path: Path | None = None,
    ):
        self.web = web
        self.downloads_dir = downloads_dir
        self.max_bytes = max_bytes
        self.client = client
        self.loop = loop
        self.giphy_api_key = giphy_api_key
        self.klipy_api_key = klipy_api_key
        self.gif_provider = (gif_provider or "auto").strip().lower()
        self.guild_stickers = guild_stickers or []
        self.stickers = StickerIndex(client, loop, sticker_cache_path)
        self.sends: list[MediaSend] = []

    def _gif_providers(self) -> list[tuple[str, str]]:
        """Ordered [(name, api_key)] GIF providers, per GIF_PROVIDER config."""
        if self.gif_provider == "giphy":
            return [("giphy", self.giphy_api_key)] if self.giphy_api_key else []
        if self.gif_provider == "klipy":
            return [("klipy", self.klipy_api_key)] if self.klipy_api_key else []
        # auto: Giphy first (better search), Klipy as backup
        out: list[tuple[str, str]] = []
        if self.giphy_api_key:
            out.append(("giphy", self.giphy_api_key))
        if self.klipy_api_key:
            out.append(("klipy", self.klipy_api_key))
        return out

    # ---- tools ----------------------------------------------------------

    def tool_search_gifs(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "[search_gifs] missing query"
        providers = self._gif_providers()
        if not providers:
            return f"[search_gifs] {GIF_KEY_HELP}"
        errors: list[str] = []
        for name, key in providers:
            try:
                gifs = self._search_giphy(key, query) if name == "giphy" else self._search_klipy(key, query)
            except httpx.HTTPError as e:
                errors.append(f"{name}: {e.__class__.__name__}")
                continue
            if gifs:
                lines = [
                    f"[search_gifs] GIFs for: {query} (via {name}; send one with send_file)"
                ]
                for i, g in enumerate(gifs, 1):
                    lines.append(f"{i}. {g['title']} — {g['url']}")
                return "\n".join(lines)
            errors.append(f"{name}: no results")
        return f"[search_gifs] no GIFs found for: {query} ({'; '.join(errors)})"

    def _search_giphy(self, key: str, query: str) -> list[dict]:
        r = self.web.get(
            GIPHY_SEARCH_URL,
            params={"api_key": key, "q": query, "limit": 6, "rating": "pg-13"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        gifs = []
        for g in (data.get("data") or [])[:6]:
            imgs = g.get("images") or {}
            url = (imgs.get("fixed_width") or imgs.get("original") or {}).get("url")
            if url:
                gifs.append({"title": (g.get("title") or query).strip(), "url": url})
        return gifs

    def _search_klipy(self, key: str, query: str) -> list[dict]:
        r = self.web.get(
            KLIPY_SEARCH_URL.format(key=key),
            params={"q": query, "per_page": 6, "content_filter": "low"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        items = (data.get("data") or {}).get("data") or []
        if isinstance(items, dict):
            items = [items]
        gifs = []
        for it in items[:6]:
            url = _klipy_url(it)
            if url:
                gifs.append({"title": (it.get("title") or it.get("name") or query).strip(), "url": url})
        return gifs

    def tool_send_file(self, source: str, caption: str | None = None) -> str:
        source = (source or "").strip()
        if not source:
            return "[send_file] missing source (a URL or a local file path)"

        if source.startswith(("http://", "https://")):
            path = self._download(source)
            if isinstance(path, str):
                return path
        else:
            p = Path(source).expanduser()
            if not p.is_absolute():
                p = self.downloads_dir / p
            if not p.is_file():
                return f"[send_file] local file not found: {source}"
            path = p

        try:
            size = path.stat().st_size
        except OSError as e:
            return f"[send_file] cannot stat {path}: {e}"
        if size == 0:
            return f"[send_file] {path.name} is empty — not sent"
        if size > self.max_bytes:
            return f"[send_file] {path.name} is {size} bytes, over the {self.max_bytes} limit — not sent"

        self.sends.append(MediaSend(kind="file", caption=caption, path=path, filename=path.name))
        return f"[send_file] queued: {path.name} ({size} bytes). It will be sent with your reply."

    def tool_search_stickers(self, query: str, limit: int = 8) -> str:
        query = (query or "").strip()
        if not query:
            return "[search_stickers] missing query"
        try:
            results = self.stickers.search(query, self.guild_stickers, limit=limit)
        except Exception as e:
            return f"[search_stickers] sticker search failed: {e}"
        if not results:
            return f"[search_stickers] no stickers found for: {query}"
        lines = [f"[search_stickers] stickers for: {query} (send one with send_sticker)"]
        for e in results:
            lines.append(f"{e['id']} — {e['name']} ({e['pack']})")
        return "\n".join(lines)

    def tool_send_sticker(self, sticker_id: str, caption: str | None = None) -> str:
        sticker_id = str(sticker_id or "").strip()
        if not sticker_id.isdigit():
            return f"[send_sticker] sticker_id must be the number from search_stickers, got: {sticker_id!r}"
        self.sends.append(MediaSend(kind="sticker", caption=caption, sticker_id=sticker_id))
        return f"[send_sticker] queued sticker {sticker_id}. It will be sent with your reply."

    # ---- helpers ---------------------------------------------------------

    def _download(self, url: str) -> Path | str:
        try:
            r = self.web.get(url, timeout=60)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return f"[send_file] download failed for {url}: {e}"
        name = _filename_for(url, r.headers.get("content-type", ""))
        path = self.downloads_dir / name
        i = 1
        while path.exists():
            path = self.downloads_dir / f"{path.stem}-{i}{path.suffix}"
            i += 1
        try:
            path.write_bytes(r.content)
        except OSError as e:
            return f"[send_file] could not save {name}: {e}"
        return path


def _klipy_url(item: dict) -> str | None:
    """Extract a downloadable GIF URL from a Klipy search item.

    Real API shape (verified 2026-10): ``item["file"]`` maps size tier
    (``hd``/``md``/``sm``/``xs``) → format (``gif``/``webp``/``jpg``/...) →
    ``{url, width, height, size}``. Prefer the ``md`` tier (up to ~512 px —
    crisp in Discord without huge downloads), then smaller tiers. Also tolerates
    a flat ``{name: {url}}`` shape or a direct url field as a safety net.
    """
    data = item.get("file") or item.get("files")
    if isinstance(data, dict):
        for tier in ("md", "sm", "xs", "hd"):
            variants = data.get(tier)
            if isinstance(variants, dict):
                for fmt in ("gif", "webp"):
                    f = variants.get(fmt)
                    if isinstance(f, dict) and f.get("url"):
                        return f["url"]
                for f in variants.values():
                    if isinstance(f, dict) and f.get("url"):
                        return f["url"]
        for v in data.values():
            if isinstance(v, dict) and v.get("url"):
                return v["url"]
    elif isinstance(data, list):
        for f in data:
            if isinstance(f, dict) and f.get("url"):
                return f["url"]
    for key in ("url", "gif_url", "media_url"):
        if item.get(key):
            return item[key]
    return None


def _filename_for(url: str, content_type: str) -> str:
    raw = unquote(urlparse(url).path.split("/")[-1]) or "download"
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:80] or "download"
    if "." not in raw:
        ext = CONTENT_TYPE_EXTENSIONS.get(content_type.split(";")[0].strip().lower())
        if ext:
            raw += ext
    return raw
