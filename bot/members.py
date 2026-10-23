"""Member lookup for the ``search_members`` model tool (hermes-agent style).

The bot runs with the Server Members intent, so ``guild.members`` is the full
live roster of the guild — Discord delivers every member in the READY payload
and keeps it current via GUILD_MEMBER_ADD/REMOVE events. This is a plain
synchronous prefix scan of that cache (no REST call, no event loop), which
matters because the agent loop runs in a worker thread.
"""
from __future__ import annotations

import discord

MAX_RESULTS = 10


def search_members(
    guild: discord.Guild | None,
    name: str,
    owner_id: int | None = None,
) -> str:
    """Prefix-search the guild's member cache.

    Matches display name OR account username, case-insensitive. Returns
    ``display name (username) — <@id>`` lines so the model can tag the person
    by writing the exact ``<@id>`` into its reply.
    """
    if guild is None:
        return "This is a DM — there is no server member list to search here."
    q = (name or "").strip().lower()
    if not q:
        return "No name to search for — pass (the start of) the member's name."

    matches: list[discord.Member] = []
    for m in guild.members:
        if m.display_name.lower().startswith(q) or m.name.lower().startswith(q):
            matches.append(m)
        if len(matches) >= MAX_RESULTS:
            break

    if not matches:
        return f"Nobody in {guild.name} has a name starting with {name.strip()!r}."

    lines = []
    for m in matches:
        owner_mark = " [YOUR OWNER]" if owner_id and m.id == owner_id else ""
        bot_mark = " (bot)" if m.bot else ""
        extra = f" (username: {m.name})" if m.name != m.display_name else ""
        lines.append(f"  {m.display_name}{owner_mark}{bot_mark}{extra} — <@{m.id}>")
    return (
        f"Members of {guild.name} whose name starts with {name.strip()!r} — "
        f"tag one by writing its exact <@id> in your reply:\n" + "\n".join(lines)
    )
