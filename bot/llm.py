"""Minimal OpenAI-compatible chat client with tool calling."""
from __future__ import annotations

import json
import logging
import time

import httpx
from typing import TYPE_CHECKING

from . import media as media_mod
from . import members as members_mod
from . import youtube

if TYPE_CHECKING:  # pragma: no cover
    import discord

log = logging.getLogger("bot.model")

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information. Use for news, facts you're "
                "unsure about, anything that may have changed, or when the user asks "
                "you to look something up online. Returns titles, URLs, and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch a web page and return its readable text content. Use when you "
                "have a URL (from web_search or the user) and need the full page text "
                "to answer accurately or to summarize it. Do NOT use it for YouTube "
                "URLs — the watch page is just JavaScript; use transcribe_youtube "
                "instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transcribe_youtube",
            "description": (
                "Get the transcript (captions) of a YouTube video. Use when the user "
                "shares or names a YouTube video and you need what's actually said in it "
                "— to summarize, quote, or discuss it. Accepts a full YouTube URL "
                "(watch/youtu.be/shorts/live) or a bare video ID. Returns the video "
                "title, author, and the caption text in [mm:ss]-stamped blocks. If the "
                "video has no captions (or they're disabled), the tool says so — tell "
                "the user, don't guess the content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "YouTube URL or video ID"},
                },
                "required": ["url"],
            },
        },
    },
]

MEDIA_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_gifs",
            "description": (
                "Search GIFs (Giphy or Klipy, depending on the configured provider). "
                "Returns a numbered list of titles with direct .gif URLs. Use when the "
                "user asks for a GIF ('send me a cat gif'), INCLUDING when they ask for "
                "another one after you already sent one — always search again and pass "
                "the new URL to send_file. A text reply alone never sends a GIF."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "GIF search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "Send a file to the user as a Discord attachment (GIF, image, PDF, "
                "audio — anything). This is the ONLY way a file/GIF actually gets "
                "delivered — writing 'here you go' or 'there's your gif' in your text "
                "reply sends nothing, so ALWAYS call this tool when the user asks for a "
                "file or GIF. `source` is either a URL (downloaded automatically, e.g. "
                "a .gif URL from search_gifs) or a local file path (e.g. a file the user "
                "sent you, whose path is in the attachment note). Sent together with "
                "your reply. `caption` is an optional short text line shown with the file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "URL or local file path"},
                    "caption": {"type": "string", "description": "Optional caption"},
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_stickers",
            "description": (
                "Search Discord stickers: the official sticker packs plus this "
                "server's stickers. Returns a list of `id — name (pack)`. Use when the "
                "user asks for a sticker. To send one, pass its id to send_sticker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Sticker search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_sticker",
            "description": (
                "Send a Discord sticker to the user. The ONLY way a sticker gets "
                "delivered — a text reply alone sends nothing. `sticker_id` is the "
                "numeric id from search_stickers, or the id of a sticker the user just "
                "sent you (it is listed with its id in the message). Sent together with "
                "your reply. `caption` is an optional short text line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sticker_id": {"type": "string", "description": "Sticker id (number)"},
                    "caption": {"type": "string", "description": "Optional caption"},
                },
                "required": ["sticker_id"],
            },
        },
    },
]


GUILD_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_members",
            "description": (
                "Look up a member of this Discord server by (the start of) their name, "
                "to get their exact mention tag. Use when you need to @mention (tag) "
                "someone who is NOT in your tagging roster — e.g. the user asks you to "
                "ping, call over, or hand the conversation to a server member. Matches "
                "display names and usernames by prefix, case-insensitive. Returns lines "
                "'display name (username) — <@id>' — tag the person by writing their "
                "<@id> in your reply. Server channels only, not DMs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The (start of the) member's name — display name or username",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


def tools_for(media: media_mod.MediaContext | None,
              guild: "discord.Guild | None" = None) -> list[dict]:
    """Base tools, plus the media tools (media context) and the member-lookup
    tool (only in server channels, where a member list exists)."""
    tools = list(TOOL_SCHEMAS)
    if media is not None:
        tools += MEDIA_TOOL_SCHEMAS
    if guild is not None:
        tools += GUILD_TOOL_SCHEMAS
    return tools


class ModelClient:
    # Retries for transient server-side hiccups (e.g. llama-swap (re)loading a
    # model returns 500s for a while, or the connection is briefly refused).
    RETRY_BACKOFFS = (5.0, 15.0, 45.0)  # ~65s total wait before giving up

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 max_tokens: int = 1024, temperature: float = 0.7):
        headers = {"User-Agent": "discordbot/0.1"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = httpx.Client(timeout=180.0, headers=headers)

    def close(self) -> None:
        self._client.close()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One API call (with retries on transient server errors).

        Returns the raw response message dict.
        """
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        while True:
            try:
                r = self._client.post(url, json=body)
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ConnectTimeout,
                    json.JSONDecodeError) as e:
                transient = (
                    isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500
                ) or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, json.JSONDecodeError))
                if not transient or attempt >= len(self.RETRY_BACKOFFS):
                    if isinstance(e, httpx.HTTPStatusError):
                        # Surface the server's own error body — a bare "500 Internal
                        # Server Error" from an OpenAI-compatible server is useless.
                        try:
                            detail = e.response.text[:500]
                        except Exception:
                            detail = ""
                        log.error("model server error: %s — response: %s", e, detail)
                    raise
                delay = self.RETRY_BACKOFFS[attempt]
                attempt += 1
                log.warning(
                    "transient model server error (%s) — retry %d/%d in %.0fs",
                    e, attempt, len(self.RETRY_BACKOFFS), delay,
                )
                time.sleep(delay)


def run_agent(
    model: ModelClient,
    web: "Web",  # type: ignore[name-defined]
    messages: list[dict],
    media: "media_mod.MediaContext | None" = None,
    guild: "discord.Guild | None" = None,
    owner_id: int | None = None,
    max_tool_rounds: int = 4,
) -> list[dict]:
    """Agent loop: send messages, execute tool calls, repeat until a final text answer.

    ``messages`` is the full conversation (system + history + current user turn).
    When ``media`` is given, the agent may also queue outbound GIFs/files/stickers
    (they land on ``media.sends``; BotClient sends them after the reply).

    Returns the NEW conversation rows added this turn, oldest first:
    assistant tool-call rows (``{"role","text","tool_calls"}``), tool-result rows
    (``{"role","tool_call_id","text"}``) and the final assistant text row
    (``{"role","text"}``). The caller persists them so the model sees its own
    past tool usage on the next turn.
    """
    convo = list(messages)
    added: list[dict] = []
    for _ in range(max_tool_rounds + 1):
        msg = model.chat(convo, tools=tools_for(media, guild))
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            text = (msg.get("content") or "").strip() or "(no response)"
            convo.append({"role": "assistant", "content": text})
            added.append({"role": "assistant", "text": text})
            return added

        convo.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })
        added.append({
            "role": "assistant",
            "text": msg.get("content") or None,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            name = tc["function"]["name"]
            args = _parse_args(tc["function"]["arguments"])
            log.info("tool call: %s(%s)", name, args)
            if name == "web_search":
                result = web.search(args.get("query", ""))
            elif name == "fetch_url":
                result = web.fetch(args.get("url", ""))
            elif name == "transcribe_youtube":
                result = youtube.transcribe(args.get("url", ""), web)
            elif name == "search_members":
                result = members_mod.search_members(guild, args.get("name", ""), owner_id=owner_id)
            elif media is not None and name == "search_gifs":
                result = media.tool_search_gifs(args.get("query", ""))
            elif media is not None and name == "send_file":
                result = media.tool_send_file(args.get("source", ""), args.get("caption"))
            elif media is not None and name == "search_stickers":
                result = media.tool_search_stickers(args.get("query", ""))
            elif media is not None and name == "send_sticker":
                result = media.tool_send_sticker(args.get("sticker_id", ""), args.get("caption"))
            else:
                result = f"unknown tool: {name}"
            convo.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            added.append({"role": "tool", "tool_call_id": tc["id"], "text": result})
    added.append({"role": "assistant", "text": "(stopped: too many tool rounds)"})
    return added


def _parse_args(raw: str) -> dict:
    import json
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
