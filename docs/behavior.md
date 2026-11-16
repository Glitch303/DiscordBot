# Behavior

## DMs
The bot answers **every** DM message from an allowed user — no mention needed. Each DM is its own private session.

## Server channels
By default (`REQUIRE_MENTION=1`) the bot only answers when **@mentioned**. It ignores everything else so it stays out of the way.

Set `REQUIRE_MENTION=0` to have it answer every message in every channel it can see (be careful in busy servers — token costs add up).

**Messages from other bots.** Discord delivers other bots' messages to your bot, but they are dropped by default to prevent bot-to-bot ping-pong loops. Control this with `DISCORD_ALLOWED_BOT_IDS`: empty/unset = ignore all bots (default), `all` = accept every bot, or a comma-separated list of bot IDs to allow only those. Allowed bot messages are treated like any other: unmentioned ones are recorded as context (shared mode), mentioned ones get a reply. Note: with `REQUIRE_MENTION=0`, two bots that both answer everything can talk to each other forever.

**Group chat awareness** (shared mode only). A channel is one shared conversation, not a bag of per-user chats:
- Every user message in the channel — *mentioned or not* — is recorded in the channel session, so the bot reads the whole chat and can refer to things said minutes or hours ago.
- In the model prompt each user message is prefixed with the speaker's name (`[Alice]`). The channel system prompt injects only the owner's identity (name + Discord id from `OWNER_ID`); which persona to wear for the owner vs. everyone else is defined in `SOUL.md` ("Group chat"), not in code.
- Replies are still only triggered by a mention (or always, with `REQUIRE_MENTION=0`), and are addressed to whoever mentioned the bot.
- `/new` resets the channel for **everyone** (shared session), so use it with that in mind.

## Sessions / memory
- **Session modes** (`SESSION_MODE`, default `shared`) — two designs, switchable at runtime:
  - `shared` — **DMs**: each DM user has an **isolated private conversation**. **Server channels**: one **shared session per channel** — Alice and Bob in the same channel share one conversation (see "Group chat awareness" above). Each stored user row carries the sender's Discord id + display name; names are re-applied at read time, so stored content stays clean. (Old per-user channel sessions are automatically merged into the shared channel session on first run — this already happened for the existing history.)
  - `per_user` — the classic design: **every (channel, user) pair has its own isolated session**. Unmentioned messages are ignored, and the system prompt adapts to the sender. DMs stay per-user.
- **Each mode has its own database**: `shared` → `data/sessions_shared.db`, `per_user` → `data/sessions_per_user.db`. The pre-split `data/sessions.db` is auto-renamed to the shared one on first start. Switching modes changes which DB is read and written — nothing is mixed or lost. Switch with `discordbot session-mode <mode>` (terminal) or `/session-mode` (Discord); both restart the bot to apply.
- Conversations are **sessions** in SQLite, Hermes-style: stable id, title, lineage.
- **All messages are kept forever**; only the last `MAX_HISTORY_MESSAGES` turns are loaded into the model context per reply.
- `/new` (or `/reset`) ends the current session and starts a fresh one — the old one stays resumable.
- `/title <name>` names the current session (used by `/resume`); `/title` shows the current name.
- **Menus (Hermes-style, adapted for a single-user bot):**
  - `/resume` with no args → numbered menu of this chat's sessions (named and unnamed) with a one-line preview each.
  - `/resume <number>` → pick from that menu (1 = most recent); `/resume <name|id>` → direct switch (name = title, or session id).
    Success replies with `↻ Resumed session **title** (N messages). Conversation restored.`
  - `/sessions` → this chat's sessions, current one marked `(current)` (Hermes' named-only default is relaxed here — an untitled conversation still shows up).
  - `/sessions search <q>` → matches titles and message content.
  - `/sessions <name|id>` → same as `/resume <name|id>`.
  - Naming a session (`/title <name>`) is still the clean way to make it findable by name.
- **Discord slash commands (Hermes-style app commands, `bot/slash.py`):** the same commands are registered as native application commands (type `/` in the app) with typed options and an **autocomplete session picker** on `/resume` and `/delete`. Interactions route into the same handlers as the text commands, which keep working unchanged. `/delete session:…` is slash-only: it removes the picked session (and all its messages); if it was the chat's current session, a fresh one is started so the chat keeps working. `/status` shows service + bot + backend health; `/start` `/stop` `/restart` control the systemd user service (stop/restart reply first, then act, so the message lands before the process goes down). Global sync covers DMs (can take up to an hour to propagate); each server gets instant guild-scoped copies on startup.

## Memory (WhatsBot-style)
On top of sessions, the bot keeps two tiers of memory (ported from WhatsBot):

- **Rolling summary** — when a session has more than `VERBATIM_TURNS` user turns (default 10), the turns that fall *outside* the most-recent-10 window are compressed into a compact summary (≤200 words, same language as the conversation). The summary is merged incrementally — each update folds new messages into the previous summary — and injected into the system prompt as `[CONVERSATION SUMMARY]`. The recent tail stays verbatim in context, so nothing is lost at the cost of tokens.
- **Durable facts** — every user message (replied *or* recorded as context) is scanned in the background for specific, durable facts about the user or people they mention (names, preferences, plans, relationships, habits — not opinions or small talk). Facts are stored per user in `data/facts.db` with an entity and a confidence, deduplicated on exact text (re-stated facts bump `last_confirmed` and keep the higher confidence), and injected into the prompt as `[FACTS about this user]` (up to `MAX_FACTS_IN_CONTEXT`, most recently confirmed first). In shared channels the facts ride on the turn (the channel system prompt is shared); in DMs/per-user mode they go in the per-user system prompt.

Both run **after** the reply, in background threads — they never add to reply latency, and a model failure in either just gets logged. A cleanup thread prunes every 6 hours: stored messages older than `MEMORY_CUTOFF_DAYS` (default 30) and facts not re-confirmed within `FACT_CUTOFF_DAYS` (default 60).

**Commands** (text and slash):
- `/memory` — what the bot remembers about this conversation and you (session stats, summary size, fact count)
- `/facts` — list the durable facts stored about you, with ids
- `/forget <id>` — delete one fact (id from `/facts`)
- `/stats` — session and message counts for this chat + your fact count

## Links
Bare URLs in a message (up to 3) are **auto-fetched** and handed to the model as context, so "what's this about?" + a link works without extra tool calls. The model can also fetch more via `fetch_url` itself.

**YouTube links** (watch / youtu.be / shorts / live / embed) are handled specially: instead of fetching the JS-heavy watch page, the bot pulls the video's **captions** (`bot/youtube.py`, via `youtube-transcript-api`) and hands the model the title, author, and the transcript in `[mm:ss]`-stamped blocks (capped at ~16 KB). Language preference is English → Indonesian, falling back to whatever caption track exists. Videos with no captions / captions disabled / that are private or region-locked come back as a short failure note the model can relay.

## Attachments
| Type | What happens |
|---|---|
| Images (png/jpg/webp) | Sent to the model as an image — it can see and describe them |
| GIFs | Described as animated (first frame visible); the bot can send one back or search for another |
| Text/code/config files | Contents inlined into the prompt (up to ~15 KB) |
| PDF / audio / video / other | Described by name + size (no content parsing) |
| Stickers | Read by name + pack + id — the bot can bounce one back or pick another |
| > `MAX_ATTACHMENT_BYTES` | Skipped |

Attachment notes include the local file path, so "send that back to me" works via `send_file`.

## Outbound media (model-decided)
The agent can send things back to you, not just talk:
- `search_gifs(query)` — GIF search via Giphy (needs `GIPHY_API_KEY`) and/or Klipy (`KLIPY_API_KEY`); `GIF_PROVIDER=auto|giphy|klipy` picks the order (default: Giphy first, Klipy as fallback). Without any key the tool says so and the bot can still send a direct `.gif` URL
- `send_file(source, caption?)` — sends a file as a Discord attachment; `source` is a URL (auto-downloaded to `downloads/`) or a local path (e.g. a file you sent). GIFs sent this way play as animated GIFs in Discord.
- `search_stickers(query)` — searches Discord's official sticker packs (14 packs, ~360 stickers, fetched once and cached in `data/sticker_packs.json`) plus this server's stickers
- `send_sticker(sticker_id, caption?)` — sends a Discord sticker (works in DMs and servers; bots don't need Nitro for stickers)
- Queued items are sent with the reply: a single uncaptioned item is merged into the reply message, otherwise each item goes out as its own message with its caption.

## Tagging (@mentions, model-decided)
The bot can @mention (tag) people in its replies by writing `<@ID>` — Discord pings them. Each turn the prompt gets a short "Tagging" note with a **roster of real Discord ids**: everyone who has spoken in the current session (name + id, most recent first, max 20 — user rows are stamped with the sender's id + display name) plus anyone tagged in the triggering message (with their username when it differs from the display name). **The bot itself is kept out of the roster** and labelled separately ("You are … (id …) — tag yourself only if asked") — otherwise, when it's the only id around, the model tags itself instead of the person actually requested. The note tells the model to use only roster ids (never invent them), to match names loosely (nickname/username/display name may differ), to tag sparingly (answering someone directly, calling their attention, handing the conversation over), and to use `search_members` — or refer by name — when the person isn't in the roster. The roster is rebuilt per turn and never persisted.

Two hermes-agent-style extras around tagging:
- `search_members(name)` — model tool (server channels only) that prefix-searches the guild's **full** member cache (the bot runs with the Server Members intent, so `guild.members` is every member, live). This is how the bot tags people it has never seen in the session: the model looks the name up, gets back `display name (username) — <@id>` lines, and writes the exact `<@id>` into its reply. Synchronous cache scan, no REST call — `bot/members.py`.
- Outbound mention guard: the client is constructed with `AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)`, so `@everyone`/`@here`/`@role` in the model's output (or echoed user text) render but **never ping**; `<@id>` user tags and the reply chip still do.

## Web tools (model-decided)
- `web_search(query)` — SearXNG, returns 5 results (title + URL + snippet)
- `fetch_url(url)` — readable text of the page (scripts/styles stripped, ~12 KB cap)
- `transcribe_youtube(url_or_id)` — captions of a YouTube video (title, author, `[mm:ss]` transcript, ~16 KB cap); used when the user names/asks about a video that wasn't linked in the message
- The agent loop allows up to **4 tool rounds** per message before it must answer.

## Errors
Model or search failures produce a short "sorry, something went wrong" reply and log the full traceback. The bot never crashes on one bad message.

## Busy handling
One message at a time per user/session; extra messages get a "still working" nudge.
