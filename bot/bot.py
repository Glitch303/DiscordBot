"""Discord bot: message handling, attachment download, session management."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path

import discord

from .attachments import describe_attachments, describe_stickers, extract_urls
from .config import Config
from .llm import ModelClient, run_agent
from .media import MediaContext, MediaSend
from . import slash
from .store import SessionStore
from .web import Web

log = logging.getLogger("bot")

class BotClient(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.model = ModelClient(cfg.model_base_url, cfg.model_name, cfg.model_api_key)
        self.web = Web(cfg.searxng_url)
        self.store = SessionStore(cfg.data_dir)
        self._busy: set[str] = set()
        self.tree = slash.register(self)  # Discord app commands (Hermes-style)

    # ---- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        self._fetch_reactions = True
        try:
            await self.tree.sync()  # global commands (these power DMs)
        except discord.DiscordException:
            log.exception("slash command sync failed")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("logged in as %s (id=%s) | allowed users: %s | require_mention: %s",
                 self.user, self.user.id,
                 sorted(self.cfg.allowed_users) or "ALL", self.cfg.require_mention)
        # NOTE: commands are registered GLOBALLY ONLY (like hermes-agent). Adding
        # guild-scoped copies as well makes Discord show each command TWICE in the
        # picker (global + guild entry). Trade-off: definition changes take up to
        # an hour to propagate to servers (old definitions keep working meanwhile).

    async def close(self) -> None:
        self.model.close()
        self.web.close()
        self.store.close()
        await super().close()

    # ---- message handling -------------------------------------------------

    def _is_allowed(self, user: discord.abc.User) -> bool:
        return not self.cfg.allowed_users or user.id in self.cfg.allowed_users

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return
        if not self._is_allowed(message.author):
            log.debug("ignoring message from non-allowed user %s", message.author.id)
            return

        channel = message.channel
        is_dm = isinstance(channel, discord.DMChannel)
        content = message.content or ""

        # Mention gating in server channels
        if not is_dm and self.cfg.require_mention:
            mentioned = (self.user in message.mentions) or (
                self.user.id in (m.id for m in message.mentions)
            )
            if not mentioned:
                return

        stripped = content.strip()
        reply = self._handle_command(message, stripped, is_dm)
        if reply is not None:
            await message.reply(reply)
            return

        chat_key = self._chat_key(message)
        if chat_key in self._busy:
            await message.reply("⏳ Still working on your last message — one at a time please.")
            return
        self._busy.add(chat_key)

        try:
            # Download attachments
            files: list[tuple[Path, str]] = []
            for att in message.attachments:
                if att.size > self.cfg.max_attachment_bytes:
                    continue
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=Path(att.filename).suffix or ".bin",
                        dir=self.cfg.downloads_dir, delete=False
                    ) as f:
                        await att.save(f)
                        files.append((Path(f.name), att.content_type))
                except Exception as e:
                    log.warning("attachment download failed: %s", e)

            # Build user turn content
            parts: list[dict] = []
            extra = ""
            urls = extract_urls(stripped)
            if urls:
                fetches = []
                for u in urls[:3]:
                    try:
                        fetches.append(self.web.fetch(u))
                    except Exception as e:
                        fetches.append(f"[fetch_url] failed: {e}")
                if fetches:
                    extra += "\n\nThe user sent these link(s), already fetched for you:\n" + "\n\n---\n\n".join(fetches)
            if stripped and stripped.lower() not in (" ",):
                parts.append({"type": "text", "text": stripped + extra})
            attach_parts = describe_attachments(files, self.model) if files else []
            if attach_parts:
                if not parts:
                    parts.append({"type": "text", "text": "The user sent an attachment (see below)." + extra})
                parts.extend(attach_parts)
            if message.stickers:
                parts.extend(describe_stickers(message.stickers))
            if not parts:
                return  # reaction-only or empty message

            # Outbound media context: the agent can queue GIFs/files/stickers
            guild = message.guild
            media_ctx = MediaContext(
                web=self.web,
                downloads_dir=self.cfg.downloads_dir,
                max_bytes=self.cfg.max_attachment_bytes,
                client=self,
                loop=asyncio.get_running_loop(),
                giphy_api_key=self.cfg.giphy_api_key,
                klipy_api_key=self.cfg.klipy_api_key,
                gif_provider=self.cfg.gif_provider,
                guild_stickers=list(guild.stickers) if guild else [],
                sticker_cache_path=self.cfg.sticker_cache_path,
            )

            # Assemble conversation
            session_id = self.store.ensure_session(chat_key, message.author.id)
            history = self.store.history(session_id, self.cfg.max_history_messages)
            convo: list[dict] = [{"role": "system", "content": self._system_prompt_for(message.author.id)}]
            convo.extend(history)
            convo.append({"role": "user", "content": parts})

            async with channel.typing():
                new_msgs = await _to_thread(run_agent, self.model, self.web, convo, media_ctx)

            # Persist the whole turn — tool calls included. Without the tool rows
            # the model can't see that it sends media by CALLING tools, and it
            # starts writing "there's your gif" in plain text instead.
            self.store.append(session_id, ("user", parts), *[(m["role"], m) for m in new_msgs])
            reply = next(
                (m["text"] for m in reversed(new_msgs) if m["role"] == "assistant" and not m.get("tool_calls")),
                None,
            ) or "(no response)"
            await self._reply_with_media(message, reply, media_ctx.sends)
        except Exception:
            log.exception("error handling message %s", message.id)
            try:
                await message.reply("Sorry, something went wrong — try again.")
            except discord.DiscordException:
                pass
        finally:
            self._busy.discard(chat_key)

    # ---- outbound media ---------------------------------------------------

    async def _reply_with_media(self, message: discord.Message, reply: str, sends: list[MediaSend]) -> None:
        """Send the reply, flushing queued files/stickers.

        A single uncaptioned item is merged into the reply message; anything
        else goes out as follow-up messages (file with caption, sticker with
        caption) so each item keeps its own text.
        """
        if not sends:
            await message.reply(reply)
            return

        items: list[dict] = []
        for s in sends:
            if s.kind == "file":
                if s.path is not None and s.path.is_file():
                    items.append({"caption": s.caption, "file": discord.File(s.path, filename=s.filename or s.path.name)})
                else:
                    log.warning("queued file missing at send time: %s", s.path)
            elif s.kind == "sticker" and s.sticker_id:
                try:
                    items.append({"caption": s.caption, "sticker": await self.fetch_sticker(int(s.sticker_id))})
                except discord.DiscordException as e:
                    log.warning("sticker %s fetch failed: %s", s.sticker_id, e)

        if not items:
            await message.reply(reply)
            return

        channel = message.channel
        if len(items) == 1 and not items[0]["caption"]:
            it = items[0]
            await message.reply(
                reply,
                file=it.get("file"),
                stickers=[it["sticker"]] if it.get("sticker") else None,
            )
            log.info("sent media with reply: %s", _describe_media(it))
            return

        try:
            await message.reply(reply)
        except discord.DiscordException:
            log.exception("failed to send reply text (media may still follow)")
        for it in items:
            try:
                await channel.send(
                    content=it["caption"],
                    file=it.get("file"),
                    stickers=[it["sticker"]] if it.get("sticker") else None,
                )
                log.info("sent media: %s", _describe_media(it))
            except discord.DiscordException:
                log.exception("failed to send media item")

    def _handle_command(self, message: discord.Message, text: str, is_dm: bool) -> str | None:
        """Process /commands. Returns the reply text, or None if not a command."""
        m = re.match(r"^/([a-zA-Z]+)(?:\s+(.*?))?\s*$", text, re.DOTALL)
        if not m:
            return None
        cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()
        chat_key = self._chat_key(message)

        if cmd in ("new", "reset"):
            self.store.end_current(chat_key, reason="user_reset")
            return "Fresh conversation started."

        if cmd in ("help", "status"):
            return (
                "**What I can do:** chat, web search, read links you send, look at images, read text files, send GIFs/stickers/files back.\n"
                "`/new` — start a fresh conversation\n"
                "`/title <name>` — name this conversation\n"
                "`/sessions [search <q> | <name>]` — list this chat's conversations\n"
                "`/resume [number | name]` — menu, or jump back to an old conversation\n"
                "`/help` — this message\n"
                "Also in Discord's slash menu (type `/`): `/new` `/title` `/sessions` `/resume` `/delete` with an autocomplete session picker, plus `/status` `/start` `/stop` `/restart` for service control."
                + ("" if is_dm else "\nMention me (@me) to get my attention in this channel.")
            )

        if cmd == "title":
            return self._title_command(chat_key, arg, message.author.id)

        if cmd == "sessions":
            return self._sessions_command(chat_key, arg, message.author.id)

        if cmd == "resume":
            return self._resume_command(chat_key, arg, message.author.id)

        return None

    def _title_command(self, chat_key: str, arg: str, user_id: int) -> str:
        session_id = self.store.ensure_session(chat_key, user_id)
        if not arg:
            t = self.store.session_title(session_id)
            return f"Current title: {t if t else '(untitled)'} — set one with `/title <name>`"
        self.store.set_title(session_id, arg[:80])
        return f"Title set to: {arg[:80]}"

    # ---- /resume & /sessions (Hermes-style menus) ---------------------------

    def _sessions_command(self, chat_key: str, arg: str, user_id: int) -> str:
        parts = arg.split()
        target_parts: list[str] = []
        for i, part in enumerate(parts):
            low = part.lower()
            if not target_parts:
                if low in ("list", "ls", "browse", "full", "--full", "all", "--all"):
                    continue  # default already lists everything; flags kept as harmless aliases
                if low in ("search", "find"):
                    query = " ".join(parts[i + 1:]).strip()
                    if not query:
                        return "Usage: `/sessions search <query>`"
                    rows = self.store.search_sessions(chat_key, query)
                    if not rows:
                        return f"No sessions matching “{query[:40]}”."
                    return self._format_session_listing(rows, f'Sessions matching “{query[:40]}”')
            target_parts.append(part)
        target = " ".join(target_parts).strip()
        if target:
            # /sessions <name|id> == /resume <name|id>
            return self._resume_command(chat_key, target, user_id)
        rows = self.store.list_sessions(chat_key)
        if not rows:
            return "No sessions here yet — just start talking."
        return self._format_session_listing(rows, "Sessions")

    def _resume_command(self, chat_key: str, arg: str, user_id: int) -> str:
        ref = _strip_resume_name(arg)
        if not ref:
            rows = self.store.list_sessions(chat_key)
            if not rows:
                return (
                    "No sessions here yet.\n"
                    "Use `/title <name>` to name your current session so it's easy to find later."
                )
            lines = ["📋 **Sessions**", ""]
            for i, s in enumerate(rows, 1):
                preview_part = f"  — _{s['preview']}_" if s["preview"] else ""
                lines.append(f"{i}. **{s['title'] or '—'}**{preview_part}")
            lines += [
                "",
                "Usage: `/resume <number>` (e.g. `/resume 1` for the most recent) or `/resume <name>` for named sessions.",
            ]
            return "\n".join(lines)

        if ref.isdigit():
            rows = self.store.list_sessions(chat_key)
            n = int(ref)
            if not (1 <= n <= len(rows)):
                return f"Resume index {n} is out of range.\nUse `/resume` with no arguments to see available sessions."
            row = rows[n - 1]
            sid, name = row["id"], row["title"] or row["preview"] or row["id"]
        else:
            sid = self.store.resolve(chat_key, ref)
            if not sid:
                return f"No session found matching '**{ref}**'.\nUse `/resume` with no arguments to see available sessions."
            name = self.store.session_title(sid) or ref

        if self.store.current_session_id(chat_key) == sid:
            return f"📌 Already on session **{name}**."
        if not self.store.resume(chat_key, sid, user_id):
            return "Failed to switch session."
        count = self.store.user_message_count(sid)
        if count == 0:
            return f"↻ Resumed session **{name}**. Conversation restored."
        unit = "message" if count == 1 else "messages"
        return f"↻ Resumed session **{name}** ({count} {unit}). Conversation restored."

    def _format_session_listing(self, rows: list[dict], title: str) -> str:
        lines = [f"📋 **{title}**", ""]
        for i, r in enumerate(rows, 1):
            current_part = " (current)" if r["is_current"] else ""
            preview_part = f"  — _{r['preview']}_" if r["preview"] else ""
            lines.append(f"{i}. **{r['title'] or '—'}**{current_part} — `{r['id']}`{preview_part}")
        lines += [
            "",
            "Resume: `/resume <session id>` or `/resume <number>` from `/resume`.",
            "More: `/sessions search <query>`, `/title <name>` to name a session.",
        ]
        return "\n".join(lines)

    def _system_prompt_for(self, user_id: int) -> str:
        """Per-user system prompt: base persona + who this user is (facts only).

        The persona itself — including which face the owner gets and which face
        everyone else gets — lives in SOUL.md, so nothing persona-specific is
        injected here.
        """
        base = self.cfg.system_prompt
        if self.cfg.owner_id and user_id == self.cfg.owner_id:
            return base + "\n\nThe person messaging you right now is your owner. Wear your private face, per your persona."
        if self.cfg.owner_id:
            return base + (
                f"\n\nThe person messaging you right now is NOT your owner "
                f"(your owner is Discord id {self.cfg.owner_id}). Wear your public face, per your persona."
            )
        return base

    def _chat_key(self, message: discord.Message) -> str:
        """Session key: per-user in DMs and server channels (isolated contexts)."""
        channel = message.channel
        if isinstance(channel, discord.DMChannel):
            return f"dm-{message.author.id}"
        return f"ch-{channel.id}-{message.author.id}"

    def _chat_key_for(self, interaction) -> str:
        """Same keying as ``_chat_key``, for app-command interactions (slash menu)."""
        channel = interaction.channel
        if channel is None or isinstance(channel, discord.DMChannel):
            return f"dm-{interaction.user.id}"
        return f"ch-{channel.id}-{interaction.user.id}"


def _describe_media(it: dict) -> str:
    if it.get("file") is not None:
        f = it["file"]
        return f"file {getattr(f, 'filename', f)}"
    s = it.get("sticker")
    if s is not None:
        return f"sticker {getattr(s, 'name', s)} ({s.id})"
    return "item"


def _to_thread(fn, *args):
    import asyncio
    return asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args))


def _strip_resume_name(raw: str) -> str:
    """Strip literal <...> / [...] / quotes typed straight from the help text."""
    name = raw.strip()
    for a, b in (('"', '"'), ("'", "'"), ("<", ">"), ("[", "]")):
        if len(name) >= 2 and name.startswith(a) and name.endswith(b):
            name = name[1:-1].strip()
    return name
