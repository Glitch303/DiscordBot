# DiscordBot

A **standalone** general-chat Discord bot in pure Python. No Hermes Agent, no frameworks beyond `discord.py` — just a small, readable codebase you own completely.

## What it does

| Feature | How |
|---|---|
| 💬 General chatting | LLM via any OpenAI-compatible endpoint, per-user persistent memory |
| 🌐 Web search | Self-hosted [SearXNG](https://docs.searxng.org/) — no API key needed |
| 📄 Web fetch | Fetches any URL and turns it into readable text |
| 👁️ Sees images | Uploads images → base64 → multimodal model actually looks at them |
| 📎 Reads files | Text/code files are inlined into the prompt; PDFs/media are described |
| 🔗 Link understanding | Bare links in a message are auto-fetched and given to the model as context |
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
   ├── auto-fetches links in messages (httpx + BeautifulSoup)
   ├── builds OpenAI-format conversation (store.py keeps per-user history)
   ▼
bot/llm.py  (agent loop, max 4 tool rounds)
   ├── tool: web_search  → bot/web.py → SearXNG @ localhost:8080 (JSON)
   ├── tool: fetch_url   → bot/web.py → httpx + BeautifulSoup
   ├── tool: search_gifs / send_file → bot/media.py → Giphy/Klipy API + download to downloads/
   ├── tool: search_stickers / send_sticker → bot/media.py → Discord sticker packs (cached)
   └── chat completions  → bot/llm.py → MODEL_BASE_URL (OpenAI-compatible)
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
  attachments.py  image/file/link/sticker → model message parts
  media.py        outbound media: GIF search, file downloads, sticker index, send queue
  store.py        SQLite session ledger: sessions, messages, /resume (Hermes-style)
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
| `OWNER_ID` | empty | the user ID treated as the **owner** (bot references them as the main user; everyone else is "other people") |
| `DISCORD_ALLOWED_USERS` | empty = allow all | comma-separated user IDs allowed to talk to the bot |
| `REQUIRE_MENTION` | `1` | `0` = respond to every channel message (no @mention needed) |
| `MODEL_BASE_URL` | `http://localhost:27906/v1` | any OpenAI-compatible endpoint |
| `MODEL_NAME` | `Qwen3.8-Orca-27B-Instruct` | must support tool calling + image input |
| `MODEL_API_KEY` | unset | needed only if the endpoint requires auth |
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG with `json` format enabled |
| `MAX_HISTORY_MESSAGES` | `30` | per-user stored history window |
| `MAX_ATTACHMENT_BYTES` | `25000000` | skip bigger uploads (also caps what the bot downloads/sends) |
| `GIPHY_API_KEY` | unset | optional; enables `search_gifs` via Giphy. Free key: https://developers.giphy.com/ — the free tier is **~30 requests/min and 50,000 impressions/month** (enough for one bot; the rate limit resets at the top of each hour) |
| `KLIPY_API_KEY` | unset | optional; alternative GIF provider (https://partner.klipy.com/, free — 100 req/hr in testing mode, unlimited once production is approved). With both keys set the bot falls back to Klipy when Giphy fails |
| `GIF_PROVIDER` | `auto` | `auto` = Giphy then Klipy; force `giphy` or `klipy` to use only one |

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
- Sessions are **per-chat** (per-user in DMs and channels) — `/sessions` only ever lists the
  current chat's history, same as Hermes' chat-scoped session listing.
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
