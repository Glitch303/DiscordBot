"""YouTube video transcription: captions via youtube-transcript-api + oEmbed metadata.

Two entry points, one function (``transcribe``):
- bot.py auto-fetched YouTube links the user sends (instead of the useless
  JS-heavy watch page that fetch_url would return), and
- the model's ``transcribe_youtube`` tool (llm.py) when the user asks about a
  video by name/link and the model wants its spoken content.

No API key needed: transcripts come from YouTube's own caption tracks, and the
title/author come from the keyless oEmbed endpoint.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("bot.youtube")

TRANSCRIPT_MAX_CHARS = 16_000
LANGUAGE_PRIORITY = ("en", "id")  # the bot's two conversation languages, in priority order
BLOCK_SECONDS = 15                # group caption segments into ~15s blocks (one line per block)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID_RE = re.compile(r"^/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})")


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith(("www.", "m.")):
        host = host[4:]
    return host


def is_youtube_url(url: str) -> bool:
    """True for watch / youtu.be / shorts / live / embed links."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return False
    host = _host(url)
    if host == "youtu.be":
        return bool(p.path and p.path.strip("/"))
    if host == "youtube.com":
        if p.path.startswith("/watch") and "v=" in (p.query or ""):
            return True
        return _PATH_ID_RE.match(p.path or "") is not None
    return False


def video_id(url_or_id: str) -> str | None:
    """Extract the 11-char video ID from a YouTube URL, or pass a bare ID through."""
    s = (url_or_id or "").strip()
    if _VIDEO_ID_RE.match(s):
        return s
    try:
        p = urlparse(s)
    except ValueError:
        return None
    host = _host(s)
    if host == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if host == "youtube.com":
        v = parse_qs(p.query).get("v", [""])
        if v and v[0]:
            return v[0]
        m = _PATH_ID_RE.match(p.path or "")
        if m:
            return m.group(1)
    return None


def _metadata(web, vid: str) -> tuple[str, str]:
    """(title, author) via YouTube oEmbed — keyless. ('', '') on any failure."""
    try:
        r = web.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        return (data.get("title") or "").strip(), (data.get("author_name") or "").strip()
    except Exception as e:
        log.debug("oembed failed for %s: %s", vid, e)
        return "", ""


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _format_blocks(segments: list[dict], max_chars: int) -> tuple[str, bool]:
    """Group caption segments into one [mm:ss]-stamped line per ~15s block.

    Returns (text, truncated).
    """
    out: list[str] = []
    total = 0
    start: float | None = None
    parts: list[str] = []
    truncated = False

    def flush() -> None:
        nonlocal total, start, parts, truncated
        if not parts:
            return
        line = f"[{_fmt_ts(start or 0)}] " + " ".join(parts)
        room = max_chars - total
        if len(line) > room:
            line = line[: max(room - 1, 0)].rstrip() + "…"
            truncated = True
        out.append(line)
        total += len(line)
        start, parts = None, []

    for seg in segments:
        if start is None or seg["start"] - start >= BLOCK_SECONDS:
            flush()
            if truncated:
                break
            start = seg["start"]
        parts.append(seg["text"])
    flush()
    if truncated:
        out.append("…[transcript truncated — the video continues]")
    return "\n".join(out), truncated


def transcribe(url_or_id: str, web) -> str:
    """Fetch the caption transcript for a YouTube URL or video ID.

    Returns a model-ready block: ``[youtube] Title — author``, the watch URL,
    then the transcript as [mm:ss]-stamped blocks (capped at
    TRANSCRIPT_MAX_CHARS). Failures come back as a ``[youtube] ...`` note,
    same contract as ``Web.fetch`` — this never raises.
    """
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        YouTubeTranscriptApiException,
    )

    vid = video_id(url_or_id)
    if not vid:
        return f"[youtube] couldn't find a video id in: {url_or_id!r}"
    watch = f"https://www.youtube.com/watch?v={vid}"

    try:
        tlist = YouTubeTranscriptApi().list(vid)
        try:
            transcript = tlist.find_transcript(LANGUAGE_PRIORITY)
        except Exception:
            # Preferred languages (en/id) missing — take whatever track exists.
            fallback = list(tlist)
            if not fallback:
                raise
            transcript = fallback[0]
        fetched = transcript.fetch(preserve_formatting=False)
    except YouTubeTranscriptApiException as e:
        return f"[youtube] {watch}: {_failure_reason(e)}"

    title, author = _metadata(web, vid)
    header = f"[youtube] {title or 'YouTube video'}"
    if author:
        header += f" — {author}"
    header += f"\n{watch}\n\nTranscript:"

    body, _ = _format_blocks(
        [{"start": s.start, "text": s.text} for s in fetched],
        TRANSCRIPT_MAX_CHARS,
    )
    if not body:
        return header + "\n(empty transcript)"
    return header + "\n" + body


def _failure_reason(e: Exception) -> str:
    name = type(e).__name__
    friendly = {
        "TranscriptsDisabled": "captions are disabled for this video",
        "NoTranscriptFound": "no captions/subtitles are available for this video",
        "VideoUnavailable": "the video is unavailable (private, removed, or region-locked)",
        "AgeRestricted": "the video is age-restricted — captions can't be fetched",
        "IpBlocked": "YouTube blocked the request from this IP",
    }.get(name)
    if friendly:
        return friendly
    msg = str(e)
    return f"couldn't fetch the transcript ({msg[:200]})"
