"""Convert Discord attachments (images / files) into model-understandable text."""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("bot.attachments")

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".csv", ".html", ".css", ".xml", ".log", ".sh", ".ini", ".cfg",
}
TEXT_MAX_CHARS = 15_000


def describe_attachments(
    files: list[tuple[Path, str]],
    model: "ModelClient",  # type: ignore[name-defined]
) -> list[dict]:
    """Turn downloaded attachments into OpenAI message content parts.

    ``files``: list of (path, mime_type) tuples.
    Returns a list of parts: text notes and/or image_url parts.
    Returns [] when there is nothing to add.
    """
    parts: list[dict] = []
    for path, mime in files:
        note = _describe_file(path, mime)
        if note:
            parts.append({"type": "text", "text": note})
        if mime.startswith("image/"):
            data_url = _to_data_url(path, mime)
            if data_url:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return parts


def _describe_file(path: Path, mime: str) -> str:
    if mime == "image/gif" or path.suffix.lower() == ".gif":
        return f"Attached GIF: {path.name} ({mime}). It's animated — I can see its first frame. You can send it back with send_file(\"{path}\") or search for another GIF with search_gifs."
    if mime.startswith("image/"):
        return f"Attached image: {path.name} ({mime}). I can see it. You can send it back with send_file(\"{path}\")."
    if mime.startswith("video/") or mime.startswith("audio/"):
        return f"Attached media: {path.name} ({mime}). I cannot watch or listen to it directly, but I can discuss it based on its name/metadata or any context you give."
    if mime in ("application/pdf",):
        return f"Attached file: {path.name} (PDF). I can read it if you paste its contents, or ask me to fetch the URL if it's online."
    if path.suffix.lower() in TEXT_EXTENSIONS or mime.startswith("text/"):
        try:
            content = path.read_text(errors="replace")[:TEXT_MAX_CHARS]
            return f"Attached file: {path.name}\n```\n{content}\n```"
        except OSError as e:
            return f"Attached file: {path.name} (could not read: {e})"
    return f"Attached file: {path.name} ({mime}, {path.stat().st_size} bytes). You can send it back with send_file(\"{path}\")."


def _to_data_url(path: Path, mime: str) -> str | None:
    try:
        import base64
        data = path.read_bytes()
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except OSError as e:
        log.warning("failed to read image %s: %s", path, e)
        return None


def describe_stickers(stickers) -> list[dict]:
    """Inbound Discord stickers -> text parts so the model knows about them.

    Stickers arrive as message.stickers (not as attachments), so without this a
    sticker-only message would be silently ignored. The model can bounce a
    sticker back with send_sticker(sticker_id=...).
    """
    parts: list[dict] = []
    for st in stickers:
        pack = getattr(st, "pack", None)
        pack_name = pack.name if pack else "server sticker"
        parts.append({
            "type": "text",
            "text": (
                f"The user sent the sticker “{st.name}” ({pack_name}, id {st.id}). "
                f"You can send it back with send_sticker(sticker_id=\"{st.id}\"), "
                f"or find and send another with search_stickers."
            ),
        })
    return parts


def extract_urls(text: str) -> list[str]:
    """Find bare URLs in message text (ones the user didn't explicitly ask about)."""
    found = re.findall(r"https?://[^\s<>\"')\]]+", text or "")
    # dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
