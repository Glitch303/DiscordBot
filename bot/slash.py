"""Native Discord slash commands (Hermes-style).

Like Hermes Agent's gateway, the in-chat commands are registered as real
Discord **application commands**: they show up in the app's command menu with
autocomplete and typed options, and each interaction is routed back into the
same command pipeline as the text-typed version (the handlers in bot.py).
The text commands keep working exactly as before — the slash surface is
additive.

    /new              start a fresh conversation (old one stays resumable)
    /title name:…     name the current conversation (or show its name)
    /sessions query:… list / search this chat's conversations
    /resume session:… resume an old conversation (autocomplete picker)
    /delete session:… delete a selected conversation
    /status           service state + bot login + backend health
    /start /stop /restart   control the systemd user service
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

import discord
from discord import app_commands

log = logging.getLogger("bot.slash")

SERVICE = "discordbot.service"
MSG_LIMIT = 2000


# ---- service control (same shape as bot/cli.py) ------------------------------

def _env() -> dict:
    """systemd --user needs XDG_RUNTIME_DIR + DBUS; headless contexts often lack them."""
    uid = os.getuid()
    xrd = f"/run/user/{uid}"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", xrd)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={xrd}/bus")
    return env


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args, SERVICE],
        capture_output=True, text=True, env=_env(), timeout=20,
    )


def service_active() -> str:
    try:
        return _systemctl("is-active").stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def service_installed() -> bool:
    try:
        return _systemctl("cat").returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


async def _deferred(fn, *args, delay: float = 1.5) -> None:
    """Let the interaction response reach Discord before the service (and with
    it this process) goes down — an instant systemctl call would kill us
    mid-response."""
    await asyncio.sleep(delay)
    await asyncio.to_thread(fn, *args)


def _fit(text: str) -> str:
    return text if len(text) <= MSG_LIMIT - 10 else text[: MSG_LIMIT - 10] + "…"


async def status_text(client) -> str:
    """/status body: service state, bot login, backend health (reuses cli.py)."""
    from .cli import _bot_login_line, _health_lines

    lines = ["**service:**"]
    if not await asyncio.to_thread(service_installed):
        lines.append("  not installed — run `bash scripts/install-service.sh`")
    else:
        lines.append(f"  {await asyncio.to_thread(service_active) or 'not running'}")
    lines.append(f"**bot:**  {await asyncio.to_thread(_bot_login_line) or 'no login recorded yet'}")
    lines.append("")
    lines += await asyncio.to_thread(_health_lines)
    return _fit("\n".join(lines))


# ---- session picker helpers ----------------------------------------------------

def _picker_choices(client, chat_key: str) -> list[app_commands.Choice[str]]:
    """Autocomplete suggestions: `N. Title (current)` → session id (value)."""
    out: list[app_commands.Choice[str]] = []
    for i, r in enumerate(client.store.list_sessions(chat_key), 1):
        label = r["title"] or r["preview"] or r["id"]
        name = f"{i}. {label}" + (" (current)" if r["is_current"] else "")
        out.append(app_commands.Choice(name=name[:100], value=r["id"]))
    return out


def _resolve_ref(client, chat_key: str, ref: str) -> tuple[str | None, str]:
    """(session_id, display_name) from a picker id, a menu number, a title, or an id prefix."""
    ref = (ref or "").strip()
    if not ref:
        return None, ""
    if ref.isdigit():
        rows = client.store.list_sessions(chat_key)
        n = int(ref)
        if 1 <= n <= len(rows):
            r = rows[n - 1]
            return r["id"], r["title"] or r["id"]
    sid = client.store.resolve(chat_key, ref)
    if sid:
        return sid, client.store.session_title(sid) or ref
    return None, ref


# ---- registration ----------------------------------------------------------------

def register(client):
    """Build the app-command tree for this bot (closures capture the client)."""
    # discord.py 2.7 renamed app_commands.Tree → app_commands.CommandTree
    tree_cls = getattr(app_commands, "CommandTree", None) or app_commands.Tree
    tree = tree_cls(client=client)
    store = client.store

    async def _deny(interaction: app_commands.Interaction) -> None:
        try:
            if interaction.response.pending:
                await interaction.response(content="🔒 This bot is private.", ephemeral=True)
            else:
                await interaction.followup(content="🔒 This bot is private.", ephemeral=True)
        except discord.DiscordException:
            pass

    def _key(interaction: app_commands.Interaction) -> str:
        return client._chat_key_for(interaction)

    # ---- sessions -------------------------------------------------------------

    @tree.command(name="new", description="Start a fresh conversation (the old one stays resumable)")
    async def new_cmd(interaction: app_commands.Interaction) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        store.end_current(_key(interaction), reason="slash_new")
        await interaction.response(content="Fresh conversation started — `/resume` gets the old one back.")

    @tree.command(name="title", description="Name the current conversation (or show its name)")
    @app_commands.describe(name="New name; omit to show the current name")
    async def title_cmd(interaction: app_commands.Interaction, name: str | None = None) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        reply = client._title_command(_key(interaction), name or "", interaction.user.id)
        await interaction.response(content=_fit(reply))

    @tree.command(name="sessions", description="List this chat's conversations, or search them")
    @app_commands.describe(query="Optional search text — matches titles and message content")
    async def sessions_cmd(interaction: app_commands.Interaction, query: str | None = None) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        arg = f"search {query}" if query else ""
        reply = client._sessions_command(_key(interaction), arg, interaction.user.id)
        await interaction.response(content=_fit(reply))

    async def _session_autocomplete(interaction: app_commands.Interaction, current: str):
        return _picker_choices(client, _key(interaction))

    def _attach_autocomplete(cmd, param: str, cb) -> None:
        # discord.py >= 2.7: Command.autocomplete(name) returns a decorator;
        # older 2.x: Command.autocomplete(name, callback).
        try:
            cmd.autocomplete(param)(cb)
        except TypeError:
            cmd.autocomplete(param, cb)

    @tree.command(name="resume", description="Jump back to an old conversation in this chat")
    @app_commands.describe(session="Pick from the list, or type its number / title / id (omit for the menu)")
    async def resume_cmd(interaction: app_commands.Interaction, session: str | None = None) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        chat_key = _key(interaction)
        if session:
            # Same pipeline as the text command: handles number, title, id,
            # out-of-range and already-current cases with the same wording.
            reply = client._resume_command(chat_key, session, interaction.user.id)
        else:
            reply = client._resume_command(chat_key, "", interaction.user.id)
        await interaction.response(content=_fit(reply))

    _attach_autocomplete(resume_cmd, "session", _session_autocomplete)

    @tree.command(name="delete", description="Delete a conversation from this chat (and all its messages)")
    @app_commands.describe(session="Pick the session to delete")
    async def delete_cmd(interaction: app_commands.Interaction, session: str) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        sid, name = _resolve_ref(client, _key(interaction), session)
        if not sid:
            return await interaction.response(
                content=f"No session found matching **{session[:60]}**.", ephemeral=True)
        deleted = await asyncio.to_thread(store.delete_session, sid)
        if not deleted:
            return await interaction.response(content="That session is already gone.", ephemeral=True)
        extra = "\nIt was the current conversation — a fresh one has been started." if deleted["new_session"] else ""
        await interaction.response(content=_fit(f"🗑️ Deleted **{name}**." + extra), ephemeral=True)

    _attach_autocomplete(delete_cmd, "session", _session_autocomplete)

    # ---- service control ----------------------------------------------------------

    @tree.command(name="status", description="Service state + bot login + backend health")
    async def status_cmd(interaction: app_commands.Interaction) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        await interaction.response(type=app_commands.ResponseType.defer_ephemeral)
        await interaction.edit(content=await status_text(client))

    @tree.command(name="start", description="Start the bot's systemd user service")
    async def start_cmd(interaction: app_commands.Interaction) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        if not await asyncio.to_thread(service_installed):
            return await interaction.response(
                content="The service isn't installed — run `bash scripts/install-service.sh` (terminal) first.",
                ephemeral=True)
        if await asyncio.to_thread(service_active) == "active":
            return await interaction.response(content="▶️ Already running.", ephemeral=True)
        await interaction.response(content="▶️ Starting the service…", ephemeral=True)
        await asyncio.to_thread(_systemctl, "start")
        await asyncio.sleep(3)
        state = await asyncio.to_thread(service_active)
        if state == "active":
            await interaction.followup(content="✅ Service is running again.", ephemeral=True)
        else:
            await interaction.followup(
                content=f"⚠️ Service state after start: **{state or 'unknown'}** — check `discordbot logs`.",
                ephemeral=True)

    @tree.command(name="stop", description="Stop the bot's service (I go offline)")
    async def stop_cmd(interaction: app_commands.Interaction) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        await interaction.response(content="🛑 Stopping the service — I'll be offline in a moment.", ephemeral=True)
        asyncio.create_task(_deferred(_systemctl, "stop"))

    @tree.command(name="restart", description="Restart the bot's service (brief offline gap)")
    async def restart_cmd(interaction: app_commands.Interaction) -> None:
        if not client._is_allowed(interaction.user):
            return await _deny(interaction)
        await interaction.response(content="🔄 Restarting the service — be right back.", ephemeral=True)
        asyncio.create_task(_deferred(_systemctl, "restart"))

    return tree
