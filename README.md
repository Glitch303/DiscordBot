# DiscordBot

A **standalone** general-chat Discord bot in pure Python. No Hermes Agent, no frameworks beyond `discord.py` — just a small, readable codebase you own completely.

## What it does

| Feature | How |
|---|---|
| 💬 General chatting | LLM via any OpenAI-compatible endpoint, persistent memory (private per DM, shared per channel) |
| 👥 Group chat awareness | In channels the bot reads the **whole** conversation (every user, mentioned or not) and knows who said what; persona switches for the owner vs. everyone else |
| 🌐 Web search | Self-hosted [SearXNG](https://docs.searxng.org/) — no API key needed |
| 📄 Web fetch | Fetches any URL and turns it into readable text |
| 👁️ Sees images | Uploads images → base64 → multimodal model actually looks at them |
| 📎 Reads files | Text/code files are inlined into the prompt; PDFs/media are described |
| 🔗 Link understanding | Bare links in a message are auto-fetched and given to the model as context |
| 📺 YouTube transcripts | YouTube links get their **captions** auto-fetched (title + author + `[mm:ss]` transcript) instead of the JS-heavy watch page; the model can also call `transcribe_youtube` on demand. No API key |
| 🎬 Sends GIFs | `search_gifs` (Giphy and/or Klipy keys — see below) → `send_file` posts the .gif as an attachment |
| 🏷️ Sends stickers | `search_stickers` (Discord's official 14 sticker packs + this server's stickers) → `send_sticker` |
| 📤 Sends files | `send_file` sends any file back — a URL (auto-downloaded) or a file the user sent |
| 😄 Sticker chat | Stickers you send are read (name + pack + id) and can be bounced back |

**Deliberately not included:** terminal access, code execution, file-system tools, browser automation. It's a chat bot — it reads the web, it sees what you send, it talks, and it sends GIFs, stickers, and files back.

## Architecture

```
Discord (DM / channels)
   │  discord.py  (message_content + members intents)
   ▼
bot/main.py → bot/bot.py  (BotClient)
   ├── mention gating, allowlist, /new /help, typing indicator
   ├── downloads attachments → temp files
   ├── auto-fetches links in messages (httpx + BeautifulSoup; YouTube links → bot/youtube.py captions)
   ├── builds OpenAI-format conversation (store.py keeps per-session history per mode: shared channel sessions with [Name] sender labels, or isolated per-user sessions)
   ▼
bot/llm.py  (agent loop, max 4 tool rounds)
   ├── tool: web_search  → bot/web.py → SearXNG @ localhost:8080 (JSON)
   ├── tool: fetch_url   → bot/web.py → httpx + BeautifulSoup
   ├── tool: transcribe_youtube → bot/youtube.py → youtube-transcript-api + keyless oEmbed (title/author)
   ├── tool: search_gifs / send_file → bot/media.py → Giphy/Klipy API + download to downloads/
   ├── tool: search_stickers / send_sticker → bot/media.py → Discord sticker packs (cached)
   └── chat completions  → bot/llm.py → MODEL_BASE_URL (OpenAI-compatible)

bot/slash.py  native Discord slash commands (Hermes-style app commands):
              /new /title /sessions /resume /delete /status /start /stop /restart
              — routed into the same handlers as the text commands above
```

Everything is plain Python you can read top to bottom in ~500 lines.

## Repository layout

```
SOUL.md             the bot's personality (system prompt) — edit freely
bot/
  main.py         entry point (python -m bot.main)
  config.py       loads .env into a Config dataclass
  bot.py          discord.Client subclass: message handling, sessions
  llm.py          OpenAI-compatible client + tool-calling agent loop
  web.py          SearXNG search + URL text extraction
  youtube.py      YouTube captions: URL/ID parsing, oEmbed metadata, transcript formatting
  attachments.py  image/file/link/sticker → model message parts
  media.py        outbound media: GIF search, file downloads, sticker index, send queue
  store.py        SQLite session ledger: sessions, messages, /resume (Hermes-style)
  slash.py        native Discord slash commands (app commands + session picker, Hermes-style)
scripts/
  setup.sh              create venv + install deps + seed .env
  start.sh              run in foreground
  install-service.sh    systemd user service (runs at boot, auto-restart)
  health-check.sh       model / searxng / discord / service status
docs/
  behavior.md       how it behaves in DMs vs channels
  troubleshooting.md
```

## Quick start

```bash
bash scripts/setup.sh        # venv + deps + .env (copy .env.example)
# edit .env: put your DISCORD_BOT_TOKEN in
bash scripts/start.sh        # run it (Ctrl-C to stop)
```

Make it a service:

```bash
bash scripts/install-service.sh
sudo loginctl enable-linger $USER   # one-time: keep it alive after logout
journalctl --user -u discordbot -f  # live logs
```

Check everything:

```bash
bash scripts/health-check.sh
```

## Terminal CLI (`discordbot`)

Like Hermes' `hermes` command, the bot is controllable from the terminal. The wrapper lives in
`bin/discordbot` — put it on your PATH once:

```bash
ln -sf "$PWD/bin/discordbot" ~/.local/bin/discordbot
```

| Command | What it does |
|---|---|
| `discordbot run` | run in the foreground (Ctrl-C to stop) |
| `discordbot start` / `stop` / `restart` | control the systemd user service (restart auto-prints status) |
| `discordbot status` | service state + logged-in bot + model/SearXNG/token health |
| `discordbot logs [-f] [-n N]` | show logs, or follow live |
| `discordbot health` | detailed backend checks + current config |
| `discordbot sessions` | list recent sessions (all chats) |
| `discordbot sessions status` | session overview: bindings, active/ended state, counts |
| `discordbot sessions [search <q>]` | search sessions by title or message content |
| `discordbot session-mode [shared\|per_user]` | show or switch session mode (shared = one session per channel, per_user = one per user); switching writes `.env` + restarts |
| `discordbot config [KEY] [VALUE]` | list, read, or set `.env` values (tokens masked) |

## Creating the Discord bot

1. [Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** tab → copy the **token** → `DISCORD_BOT_TOKEN`
3. **Privileged Gateway Intents**: enable **Message Content Intent** (required) and **Server Members Intent** (for allowlist by username; user IDs work without it)
4. Invite URL (paste `YOUR_APP_ID`):
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=277025473544`
   (permissions: view channels, send messages, embed links, attach files, read history, add reactions)
   ⚠️ If you invited the bot with an older URL, re-invite it with this one — it adds
   **Attach Files**, which the bot needs to send GIFs/files in server channels.
5. Find your user ID (Settings → Advanced → Developer Mode → right-click yourself → Copy ID) → `DISCORD_ALLOWED_USERS`

## Configuration (.env)

| Var | Default | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | required |
| `OWNER_ID` | empty | the user ID treated as the **owner** — the bot wears the private persona defined in `SOUL.md` for them and its public persona for everyone else (see SOUL.md → Group chat) |
| `DISCORD_ALLOWED_USERS` | empty = allow all | comma-separated user IDs allowed to talk to the bot |
| `REQUIRE_MENTION` | `1` | `0` = respond to every channel message (no @mention needed) |
| `MODEL_BASE_URL` | `http://localhost:27906/v1` | any OpenAI-compatible endpoint |
| `MODEL_NAME` | `Qwen3.8-Orca-27B-Instruct` | must support tool calling + image input |
| `MODEL_API_KEY` | unset | needed only if the endpoint requires auth |
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG with `json` format enabled |
| `MAX_HISTORY_MESSAGES` | `30` | stored history window per chat (DM or channel session) |
| `MAX_ATTACHMENT_BYTES` | `25000000` | skip bigger uploads (also caps what the bot downloads/sends) |
| `GIPHY_API_KEY` | unset | optional; enables `search_gifs` via Giphy. Free key: https://developers.giphy.com/ — the free tier is **~30 requests/min and 50,000 impressions/month** (enough for one bot; the rate limit resets at the top of each hour) |
| `KLIPY_API_KEY` | unset | optional; alternative GIF provider (https://partner.klipy.com/, free — 100 req/hr in testing mode, unlimited once production is approved). With both keys set the bot falls back to Klipy when Giphy fails |
| `YOUTUBE_HTTP_PROXY` | unset | optional; proxy (`http://user:pass@host:port`) for the YouTube transcript API — escape hatch if YouTube persistently blocks your IP (transient rate-limits are retried automatically) |
| `GIF_PROVIDER` | `auto` | `auto` = Giphy then Klipy; force `giphy` or `klipy` to use only one |
| `SESSION_MODE` | `shared` | `shared` = one shared session per channel (whole-chat context, `[Name]` labels); `per_user` = one isolated session per user per channel. Each mode has its own database (`data/sessions_shared.db` / `data/sessions_per_user.db`). Switch with `discordbot session-mode <mode>` or `/session-mode` (both restart the bot) |

## Personality (SOUL.md)

The bot's voice and behavior come from [`SOUL.md`](SOUL.md) at the project root — it's
injected as the system prompt on every message. Edit it however you like (name, tone,
quirks, rules) and restart:

```bash
# stop the current run
pkill -f "bot\.main"
# start again (or use your service manager)
bash scripts/start.sh
```

If `SOUL.md` is missing, a minimal built-in persona is used as a fallback.

## In-Discord commands

### Text commands (typed as a normal message)

| Command | Effect |
|---|---|
| `/new` or `/reset` | start a fresh conversation (the old one is kept, resumable) |
| `/title <name>` | name the current conversation (`/title` alone shows it) |
| `/resume` | numbered menu of this chat's conversations |
| `/resume <number\|name\|id>` | jump back to an old conversation (number, title, or session id) |
| `/sessions` | list this chat's conversations (current one marked) |
| `/sessions search <q>` | find conversations by title or message content |
| `/sessions <name\|id>` | same as `/resume <name\|id>` |
| `/help` | capability summary |

Everything else is natural language. Mention the bot in a channel (`@Bot what's the weather in Tokyo`) or just DM it.

### Slash commands (type `/` in Discord — Hermes-style app commands)

Like Hermes Agent, the commands are also registered as **native Discord application
commands** (`bot/slash.py`): they appear in the app's command menu with typed
options and an autocomplete session picker, and route into the same handlers as
the text commands (which keep working). Global sync powers DMs; each server gets
instant guild-scoped copies on startup.

| Command | Effect |
|---|---|
| `/new` | start a fresh conversation (old one stays resumable) |
| `/title name:…` | name the current conversation (omit `name` to show it) |
| `/sessions query:…` | list / search this chat's conversations |
| `/resume session:…` | resume a conversation — pick from the autocomplete list or type number/title/id; omit for the menu |
| `/delete session:…` | delete the picked conversation (and all its messages); if it was the current one, a fresh one is started |
| `/session-mode mode:…` | switch session mode (`shared` vs `per_user`); omit to show current. I restart myself to apply it |
| `/status` | service state + bot login + model/SearXNG/token health (ephemeral) |
| `/start` | start the systemd user service |
| `/stop` | stop the service (the bot goes offline after replying) |
| `/restart` | restart the service (brief offline gap) |

## Sessions & history (Hermes-style)

Like Hermes Agent, the bot keeps a full session ledger in SQLite (`data/sessions.db`) instead of
one pruned transcript per chat:

- Every conversation is a **session** with a stable id, title, and lineage (`/new` ends the
  current session and starts a new one that points back to the old — nothing is deleted).
- **All messages are retained forever.** Only a sliding window (`MAX_HISTORY_MESSAGES`) is
  loaded into the model's context per reply, so old sessions can be fully resumed later.
- **Tool calls are part of the history too.** Assistant tool-call rows and tool results are
  stored with their turn and re-injected into the model's context — that's what stops the
  model from "remembering" it sent a GIF it actually never sent (a plain-text
  "there's your gif" used to happen when only the final reply was kept).
- Sessions are **per-chat**, in one of two designs (`SESSION_MODE`):
  - `shared` (default): private per DM user, and **shared per channel** — in a server
    channel everyone's messages land in one session, each user row stamped with the
    sender's Discord id + display name (names are re-applied at read time, so stored
    content stays clean and the model always knows who said what). Old per-user channel
    sessions are auto-merged into the shared channel session on first start.
  - `per_user`: every (channel, user) pair gets its own isolated session; unmentioned
    messages are ignored; the system prompt adapts to the sender.
- **Each mode has its own database** — `data/sessions_shared.db` vs
  `data/sessions_per_user.db` — so switching never mixes histories (the pre-split
  `data/sessions.db` is auto-renamed to the shared one on first start). Switch with
  `discordbot session-mode <mode>` or `/session-mode` in Discord; both restart the bot.
- `/sessions` only ever lists the current chat's history, same as Hermes' chat-scoped session listing.
- Existing JSON sessions were auto-migrated to SQLite on first start.

## Requirements

- Python ≥ 3.11
- `uv` (for venv setup)
- A model endpoint (local or hosted) supporting chat completions + tool calling + image input
- SearXNG (or any search JSON API you adapt in `bot/web.py`)

## License

MIT — see [LICENSE](LICENSE).

The session ledger design (`/new`, `/title`, `/sessions`, `/resume`) is adapted
from [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous
Research (MIT License) — concepts and schema ideas, not copied code.
