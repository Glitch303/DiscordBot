"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEFAULT_PERSONA = """\
You are a friendly, knowledgeable Discord chat bot. Be concise, warm, and helpful. \
Use web_search for current/verifiable info and fetch_url to read pages. You can see \
images and read text files the user sends you. No terminal, no code execution. \
If the system prompt names an owner, be a little warmer and more casual with \
them; with everyone else stay friendly and polite.
"""


@dataclass(frozen=True)
class Config:
    # Discord
    discord_token: str
    allowed_users: frozenset[int]
    require_mention: bool
    owner_id: int | None

    # LLM (OpenAI-compatible)
    model_base_url: str
    model_name: str
    model_api_key: str | None

    # Web
    searxng_url: str

    # Outbound media (GIF search; stickers use Discord's own API)
    giphy_api_key: str | None
    klipy_api_key: str | None
    gif_provider: str  # "auto" | "giphy" | "klipy" — order for search_gifs

    # Behavior
    max_history_messages: int
    max_attachment_bytes: int
    log_level: str

    # Paths
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    downloads_dir: Path = field(default_factory=lambda: ROOT / "downloads")
    soul_path: Path = field(default_factory=lambda: ROOT / "SOUL.md")
    sticker_cache_path: Path = field(
        default_factory=lambda: ROOT / "data" / "sticker_packs.json"
    )

    @property
    def system_prompt(self) -> str:
        if self.soul_path.exists():
            return self.soul_path.read_text().strip()
        return DEFAULT_PERSONA


def load() -> Config:
    allowed = frozenset(
        int(x)
        for x in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",")
        if x.strip().isdigit()
    )
    owner_raw = os.environ.get("OWNER_ID", "").strip()
    return Config(
        discord_token=os.environ["DISCORD_BOT_TOKEN"],
        allowed_users=allowed,
        owner_id=int(owner_raw) if owner_raw.isdigit() else None,
        require_mention=os.environ.get("REQUIRE_MENTION", "1") not in ("0", "false", ""),
        model_base_url=os.environ.get("MODEL_BASE_URL", "http://localhost:27906/v1").rstrip("/"),
        model_name=os.environ.get("MODEL_NAME", "Qwen3.8-Orca-27B-Instruct"),
        model_api_key=os.environ.get("MODEL_API_KEY") or None,
        searxng_url=os.environ.get("SEARXNG_URL", "http://localhost:8080").rstrip("/"),
        giphy_api_key=os.environ.get("GIPHY_API_KEY") or None,
        klipy_api_key=os.environ.get("KLIPY_API_KEY") or None,
        gif_provider=(os.environ.get("GIF_PROVIDER", "auto") or "auto").strip().lower(),
        max_history_messages=_int("MAX_HISTORY_MESSAGES", 30),
        max_attachment_bytes=_int("MAX_ATTACHMENT_BYTES", 25_000_000),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
