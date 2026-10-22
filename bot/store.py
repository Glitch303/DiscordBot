"""Session store: SQLite-backed sessions + messages (Hermes-style, standalone).

Two session designs (``SESSION_MODE``) are supported, and **each gets its own
database** so switching never mixes histories:

- ``shared`` → ``sessions_shared.db``: one session per server channel; every
  message (mentioned or not) is stored with its sender, labelled ``[Name]``
  at read time.
- ``per_user`` → ``sessions_per_user.db``: one isolated session per
  (channel, user); unmentioned messages are ignored; the system prompt
  adapts to the sender.

``sessions.db`` is the pre-split name (it always held shared-design
sessions); it is renamed to ``sessions_shared.db`` automatically on first
start.

Design adapted from Hermes Agent's state layer (hermes_state_*.py), simplified:

- ``sessions`` table: every conversation is a row with a stable id, title,
  lineage (``parent_session_id``), and activity timestamps. Sessions are never
  deleted on ``/new`` — they're ended and kept, so they can be resumed.
- ``messages`` table: ALL messages are retained forever (Hermes keeps full
  history for search). Only a sliding window (``max_messages``) is loaded
  into the model context at read time.
- ``chat_sessions`` table: binds a chat key to its active session (Hermes
  calls this gateway routing). ``/resume`` simply re-points this binding.
- Server-channel sessions are SHARED per channel (key ``ch-{channel}``): the
  whole conversation, from every user, lives in one session. Each user row
  carries ``user_id`` + ``sender`` so the model knows who said what; history
  can be read back with ``[Name]`` labels. DMs stay per-user (``dm-{user}``).

This is what powers /new, /title, /sessions, and /resume.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("bot.store")

# One database per session mode (see module docstring).
SHARED_DB_NAME = "sessions_shared.db"
PER_USER_DB_NAME = "sessions_per_user.db"
LEGACY_DB_NAME = "sessions.db"  # pre-split name, always shared-design data


def db_name_for_mode(mode: str) -> str:
    return SHARED_DB_NAME if mode == "shared" else PER_USER_DB_NAME


def db_path_for_mode(data_dir: Path, mode: str) -> Path:
    """The SQLite file a mode uses (shared falls back to the pre-split name)."""
    path = data_dir / db_name_for_mode(mode)
    if path.name == SHARED_DB_NAME and not path.exists():
        legacy = data_dir / LEGACY_DB_NAME
        if legacy.exists():
            return legacy
    return path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    user_id INTEGER,
    title TEXT,
    title_source TEXT DEFAULT '',
    parent_session_id TEXT,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    last_activity_at REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_title ON sessions(title);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    user_id INTEGER,
    sender TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id)
);
"""

PREVIEW_CHARS = 60


def _new_id() -> str:
    return "s-" + secrets.token_hex(6)


def _now() -> float:
    return time.time()


def _content_to_json(content) -> str:
    if isinstance(content, dict):
        return json.dumps(content)
    if isinstance(content, str):
        return json.dumps({"text": content})
    return json.dumps({"parts": content})


def _content_from_json(raw: str):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict):
        # Tool-usage rows carry their API shape in the stored dict itself.
        if "tool_calls" in data or "tool_call_id" in data:
            return data
        if "parts" in data:
            return data["parts"]
        return data.get("text", raw)
    return data


def _row_to_api_message(role: str, raw: str) -> dict | None:
    """Rebuild an OpenAI-format message from a stored row.

    Assistant tool-call rows come back with ``tool_calls``; ``tool`` rows come
    back with ``tool_call_id``. Everything else is a plain {role, content}.
    """
    data = _content_from_json(raw)
    if role == "assistant" and isinstance(data, dict) and "tool_calls" in data:
        msg: dict = {"role": "assistant", "content": data.get("text") or None}
        if data["tool_calls"]:
            msg["tool_calls"] = data["tool_calls"]
        return msg
    if role == "tool" and isinstance(data, dict) and "tool_call_id" in data:
        return {
            "role": "tool",
            "tool_call_id": data["tool_call_id"],
            "content": data.get("text", ""),
        }
    if role in ("user", "assistant"):
        return {"role": role, "content": data}
    return None


class SessionStore:
    """One SQLite ledger for ONE session mode (``shared`` or ``per_user``).

    ``mode`` selects which database file to use (one per mode) and which
    one-time migrations to run. The pre-split ``sessions.db`` is renamed to
    ``sessions_shared.db`` on first start so existing shared history is kept.
    """

    def __init__(self, data_dir: Path, mode: str = "shared", db_name: str | None = None):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        if db_name is None:
            if mode == "shared":
                self._maybe_rename_legacy_db()
            db_name = db_name_for_mode(mode)
        self.db_path = self.dir / db_name
        self._lock = threading.RLock()  # reentrant: public methods may call each other
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_schema()
        self.migrate_legacy_json()
        # The per-user → shared merge only makes sense for the shared design:
        # in per_user mode ``ch-{channel}-{user}`` keys are normal sessions.
        if mode == "shared":
            self.migrate_per_user_sessions()

    # ---- one-time rename for the DB split -----------------------------------

    def _maybe_rename_legacy_db(self) -> None:
        """Rename pre-split ``sessions.db`` → ``sessions_shared.db``.

        The pre-split database always held shared-design sessions, so it
        belongs to the shared mode. The -wal/-shm sidecars move with it
        (SQLite pairs them by filename, so all three are renamed together).
        """
        legacy = self.dir / LEGACY_DB_NAME
        target = self.dir / SHARED_DB_NAME
        if target.exists() or not legacy.exists():
            return
        legacy.replace(target)
        for suffix in ("-wal", "-shm"):
            side = self.dir / (LEGACY_DB_NAME + suffix)
            if side.exists():
                side.replace(self.dir / (SHARED_DB_NAME + suffix))
        log.info("renamed pre-split %s → %s (one-time, per-mode DB split)",
                 LEGACY_DB_NAME, SHARED_DB_NAME)

    # ---- one-time migration from the old JSON-per-chat layout --------------

    def migrate_legacy_json(self) -> None:
        legacy = self.dir / "sessions"
        if not legacy.exists():
            return
        for p in sorted(legacy.glob("*.json")):
            session_key = p.stem
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM sessions WHERE session_key = ? LIMIT 1", (session_key,)
                ).fetchone()
                if exists:
                    continue
            try:
                with open(p) as f:
                    data = json.load(f)
                msgs = data.get("messages", [])
            except (json.JSONDecodeError, OSError) as e:
                log.warning("legacy migration: skipping %s (%s)", p.name, e)
                continue
            session_id = self.create_session(session_key, user_id=None)
            now = _now()
            with self._lock:
                for m in msgs:
                    role = m.get("role", "user")
                    self._conn.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                        (session_id, role, _content_to_json(m.get("content", "")), now),
                    )
                self._conn.execute(
                    "UPDATE sessions SET message_count = ?, last_activity_at = ? WHERE id = ?",
                    (len(msgs), now, session_id),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO chat_sessions (session_key, session_id) VALUES (?,?)",
                    (session_key, session_id),
                )
                self._conn.commit()
            log.info("migrated legacy session %s (%d messages)", session_key, len(msgs))

    # ---- one-time schema migration (sender columns) -------------------------

    def _migrate_schema(self) -> None:
        """Add the sender columns to pre-existing databases (new ones get them from SCHEMA)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        with self._lock:
            if "user_id" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN user_id INTEGER")
            if "sender" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN sender TEXT")
            self._conn.commit()

    # ---- one-time merge of legacy per-user channel sessions -----------------

    def migrate_per_user_sessions(self) -> None:
        """Merge legacy per-user channel sessions into one shared session per channel.

        Old key format was ``ch-{channel}-{user}`` (one isolated session per
        person). The bot now shares ONE session per channel so it can follow
        the whole conversation. Every message from a channel's per-user
        sessions is copied (timestamp order) into ``ch-{channel}``; the legacy
        sessions are ended (kept for the record). Idempotent: already-migrated
        sessions are skipped.
        """
        pat = re.compile(r"^ch-(\d+)-(\d+)$")
        with self._lock:
            sessions = self._conn.execute(
                "SELECT id, session_key, user_id, end_reason FROM sessions"
            ).fetchall()
        groups: dict[str, list] = {}
        for s in sessions:
            m = pat.match(s["session_key"])
            if m and s["end_reason"] != "migrated_to_shared":
                groups.setdefault(m.group(1), []).append(s)
        if not groups:
            return
        total = 0
        for chan, rows in groups.items():
            try:
                total += self._merge_channel_sessions(rows, f"ch-{chan}")
            except Exception:
                log.exception("per-user→shared session merge failed for channel %s", chan)
        if total:
            log.info("merged %d legacy messages into shared channel sessions", total)

    def _merge_channel_sessions(self, rows: list, shared_key: str) -> int:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM sessions WHERE session_key = ? LIMIT 1", (shared_key,)
            ).fetchone()
            shared_id = existing["id"] if existing else None
            if shared_id is None:
                shared_id = _new_id()
                now = _now()
                self._conn.execute(
                    "INSERT INTO sessions (id, session_key, started_at, last_activity_at) "
                    "VALUES (?,?,?,?)", (shared_id, shared_key, now, now),
                )
            msgs: list[tuple] = []
            for s in rows:
                for m in self._conn.execute(
                    "SELECT id, role, content, timestamp FROM messages "
                    "WHERE session_id = ? ORDER BY id", (s["id"],),
                ):
                    uid = s["user_id"] if m["role"] == "user" else None
                    msgs.append((m["timestamp"], s["id"], m["id"], m["role"], m["content"], uid))
            moved = 0
            if msgs:
                # Interleave all speakers by real time. (ts, legacy_session, row)
                msgs.sort(key=lambda t: (t[0], t[1], t[2]))
                for ts, _sid, _mid, role, content, uid in msgs:
                    self._conn.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp, user_id) "
                        "VALUES (?,?,?,?,?)", (shared_id, role, content, ts, uid),
                    )
                    moved += 1
                self._renumber_messages(shared_id)
                ids = tuple(s["id"] for s in rows)
                ph = ",".join("?" * len(ids))
                # Mark EVERY copied legacy session (including ones already ended,
                # e.g. by /new) so a re-run doesn't merge the same messages again.
                self._conn.execute(
                    f"UPDATE sessions SET end_reason = 'migrated_to_shared', "
                    f"ended_at = COALESCE(ended_at, ?) WHERE id IN ({ph})",
                    (_now(), *ids),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO chat_sessions (session_key, session_id) VALUES (?,?)",
                (shared_key, shared_id),
            )
            if moved:
                first, last, cnt = self._conn.execute(
                    "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM messages "
                    "WHERE session_id = ?", (shared_id,),
                ).fetchone()
                self._conn.execute(
                    "UPDATE sessions SET message_count = ?, started_at = ?, last_activity_at = ? "
                    "WHERE id = ?", (cnt, first or _now(), last or _now(), shared_id),
                )
            self._conn.commit()
            return moved

    def _renumber_messages(self, session_id: str) -> None:
        """Rewrite a session's message ids in (timestamp, id) order.

        AUTOINCREMENT ids only track insertion order; after merging several
        sessions, ids must reflect true chronological order because
        ``history()`` reads by id. Message ids are a GLOBAL primary key, so the
        new block is placed above the current max id (not at 1..N), and
        ``sqlite_sequence`` is advanced so the next AUTOINCREMENT doesn't
        collide. Two passes with a big offset avoid mid-update PK clashes.
        """
        rows = self._conn.execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        if not rows:
            return
        max_id = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        base = max_id
        OFFSET = 10**12
        for r in rows:
            self._conn.execute("UPDATE messages SET id = id + ? WHERE id = ?", (OFFSET, r["id"]))
        for i, r in enumerate(rows, 1):
            self._conn.execute(
                "UPDATE messages SET id = ? WHERE id = ?", (base + i, r["id"] + OFFSET)
            )
        self._conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'messages'", (base + len(rows),)
        )

    # ---- session lifecycle --------------------------------------------------

    def create_session(self, session_key: str, user_id: int | None = None,
                       parent_session_id: str | None = None) -> str:
        session_id = _new_id()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, session_key, user_id, started_at, last_activity_at, "
                "parent_session_id) VALUES (?,?,?,?,?,?)",
                (session_id, session_key, user_id, now, now, parent_session_id),
            )
            self._conn.commit()
        return session_id

    def ensure_session(self, session_key: str, user_id: int) -> str:
        """Return the active session for this chat, creating one if needed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT s.id FROM chat_sessions c JOIN sessions s ON s.id = c.session_id "
                "WHERE c.session_key = ? AND s.ended_at IS NULL",
                (session_key,),
            ).fetchone()
            if row:
                return row["id"]
            session_id = self.create_session(session_key, user_id)
            self._conn.execute(
                "INSERT OR REPLACE INTO chat_sessions (session_key, session_id) VALUES (?,?)",
                (session_key, session_id),
            )
            self._conn.commit()
            return session_id

    def end_current(self, session_key: str, reason: str) -> str | None:
        """End the chat's active session. Returns the new session id (a /new creates one)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT c.session_id FROM chat_sessions c WHERE c.session_key = ?", (session_key,)
            ).fetchone()
            old_id = row["session_id"] if row else None
            if old_id:
                self._conn.execute(
                    "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
                    (_now(), reason, old_id),
                )
            session_id = self.create_session(session_key)
            self._conn.execute(
                "INSERT OR REPLACE INTO chat_sessions (session_key, session_id) VALUES (?,?)",
                (session_key, session_id),
            )
            if old_id:
                self._conn.execute(
                    "UPDATE sessions SET parent_session_id = ? WHERE id = ? AND parent_session_id IS NULL",
                    (old_id, session_id),
                )
            self._conn.commit()
            return session_id

    def resume(self, session_key: str, session_id: str, user_id: int) -> bool:
        """Rebind the chat to an existing (ended or active) session."""
        with self._lock:
            target = self._conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not target:
                return False
            row = self._conn.execute(
                "SELECT c.session_id FROM chat_sessions c WHERE c.session_key = ?", (session_key,)
            ).fetchone()
            current = row["session_id"] if row else None
            if current and current != session_id:
                self._conn.execute(
                    "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
                    (_now(), "superseded_by_resume", current),
                )
            self._conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL, last_activity_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO chat_sessions (session_key, session_id) VALUES (?,?)",
                (session_key, session_id),
            )
            self._conn.commit()
            return True

    # ---- messages -----------------------------------------------------------

    def append(self, session_id: str, *messages: tuple[str, object],
               user_id: int | None = None, sender: str | None = None) -> None:
        """Append rows to a session. ``user_id``/``sender`` stamp the USER rows
        (assistant/tool rows are left unattributed) so shared channel history
        can say who said what."""
        now = _now()
        with self._lock:
            for role, content in messages:
                uid = user_id if role == "user" else None
                name = sender if role == "user" else None
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp, user_id, sender) "
                    "VALUES (?,?,?,?,?,?)",
                    (session_id, role, _content_to_json(content), now, uid, name),
                )
            self._conn.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_activity_at = ? WHERE id = ?",
                (len(messages), now, session_id),
            )
            self._conn.commit()

    def history(self, session_id: str, max_messages: int,
                label_senders: bool = False) -> list[dict]:
        """Most recent messages for the model context (oldest first).

        The window is measured in user turns, but assistant tool-call rows and
        tool-result rows are kept intact with their turns — the model has to
        see past tool usage or it stops calling tools and starts inventing the
        results in plain text (see the "hallucinated GIF send" bug).

        ``label_senders`` (shared channel sessions): prefix each user message
        with ``[Sender]`` so the model can tell who said what. The label is a
        view-time concern — stored content stays clean, and legacy rows without
        a stored name fall back to ``[id:<discord id>]`` (the owner id is in
        the system prompt, so the model can still match it).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, user_id, sender FROM messages "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                # A turn can be up to ~11 rows (user + 4 tool rounds + final
                # assistant); fetch generously and cut by user turns instead.
                (session_id, max_messages * 16),
            ).fetchall()
        kept: list = []
        turns = 0
        for r in rows:  # newest first
            kept.append(r)
            if r["role"] == "user":
                turns += 1
                if turns >= max_messages:
                    break
        out: list[dict] = []
        for r in reversed(kept):
            m = _row_to_api_message(r["role"], r["content"])
            if m is not None:
                if label_senders and m["role"] == "user":
                    m = _label_user_message(m, r["sender"], r["user_id"])
                out.append(m)
        out = _dedupe_repeated_replies(out)
        # Don't start on an assistant/tool message (would confuse the API)
        while out and out[0]["role"] != "user":
            out = out[1:]
        return out

    # ---- titles ---------------------------------------------------------------

    def set_title(self, session_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET title = ?, title_source = 'user' WHERE id = ?", (title, session_id)
            )
            self._conn.commit()

    # ---- listing / search ------------------------------------------------------

    def _list_rows(self, session_key: str, where: str = "", params: tuple = ()) -> list[dict]:
        current_id = None
        with self._lock:
            crow = self._conn.execute(
                "SELECT c.session_id FROM chat_sessions c WHERE c.session_key = ?", (session_key,)
            ).fetchone()
            current_id = crow["session_id"] if crow else None
            rows = self._conn.execute(
                f"""SELECT s.id, s.title, s.message_count, s.last_activity_at, s.ended_at,
                           (SELECT substr(m.content, 1, 200) FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' ORDER BY m.id LIMIT 1) AS first_user
                    FROM sessions s
                    WHERE s.session_key = ? {where}
                    ORDER BY s.last_activity_at DESC, s.started_at DESC
                    LIMIT 15""",
                (session_key, *params),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "title": r["title"],
                "preview": _preview_text(r["first_user"] or "")[:PREVIEW_CHARS],
                "message_count": r["message_count"],
                "last_activity_at": r["last_activity_at"],
                "is_current": r["id"] == current_id and r["ended_at"] is None,
                "ended": r["ended_at"] is not None,
            })
        return out

    def list_sessions(self, session_key: str) -> list[dict]:
        return self._list_rows(session_key)

    def list_named_sessions(self, session_key: str, limit: int = 10) -> list[dict]:
        """Titled sessions only, newest activity first (the /resume menu)."""
        return [r for r in self._list_rows(session_key) if r["title"]][:limit]

    def current_session_id(self, session_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT c.session_id FROM chat_sessions c WHERE c.session_key = ?",
                (session_key,),
            ).fetchone()
        return row["session_id"] if row else None

    def delete_session(self, session_id: str) -> dict | None:
        """Delete a session and all its messages.

        Returns ``{id, title, new_session}`` — ``new_session`` is True when the
        session was this chat's active one, in which case a fresh replacement
        is created so the chat keeps working (same as ``/new``). ``None`` when
        the id is unknown.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            # Children lose their lineage pointer (no FK on parent_session_id,
            # but don't leave dangling references around).
            self._conn.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                (session_id,),
            )
            bound = self._conn.execute(
                "SELECT session_key FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            new_session = False
            if bound:
                new_id = _new_id()
                now = _now()
                self._conn.execute(
                    "INSERT INTO sessions (id, session_key, started_at, last_activity_at) "
                    "VALUES (?,?,?,?)",
                    (new_id, bound["session_key"], now, now),
                )
                self._conn.execute(
                    "UPDATE chat_sessions SET session_id = ? WHERE session_key = ?",
                    (new_id, bound["session_key"]),
                )
                new_session = True
            self._conn.commit()
            return {"id": row["id"], "title": row["title"], "new_session": new_session}

    def sender_roster(self, session_id: str, limit: int = 20) -> list[tuple[int, str]]:
        """Distinct (user_id, display name) pairs of people who spoke in this session.

        Most recent speakers first. Feeds the per-turn mention roster so the
        model can @tag people with REAL Discord ids instead of inventing them.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.user_id, m.sender
                   FROM messages m
                   JOIN (SELECT user_id, MAX(id) AS mid FROM messages
                         WHERE session_id = ? AND role = 'user' AND user_id IS NOT NULL
                         GROUP BY user_id) g ON g.mid = m.id
                   ORDER BY m.id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [(r["user_id"], r["sender"] or f"id:{r['user_id']}") for r in rows]

    def user_message_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()
        return row["c"]

    def search_sessions(self, session_key: str, query: str) -> list[dict]:
        like = f"%{query}%"
        rows = self._list_rows(
            session_key,
            where="AND (s.title LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.content LIKE ?))",
            params=(like, like),
        )
        return rows

    def resolve(self, session_key: str, ref: str) -> str | None:
        """Resolve a /resume reference: session id (exact or prefix) or title (exact, then partial)."""
        ref = ref.strip()
        if not ref:
            return None
        low = ref.lower()
        with self._lock:
            if low.startswith("s-"):
                row = self._conn.execute(
                    "SELECT id FROM sessions WHERE session_key = ? AND (id = ? OR id LIKE ?) "
                    "ORDER BY LENGTH(id) LIMIT 1",
                    (session_key, low, f"{low}%"),
                ).fetchone()
                if row:
                    return row["id"]
                return None
            # Exact title first, then partial, most recently active first
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE session_key = ? AND lower(title) = ? "
                "ORDER BY last_activity_at DESC LIMIT 1",
                (session_key, low),
            ).fetchone()
            if row:
                return row["id"]
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE session_key = ? AND lower(title) LIKE ? "
                "ORDER BY last_activity_at DESC LIMIT 1",
                (session_key, f"%{low}%"),
            ).fetchone()
            return row["id"] if row else None

    def session_title(self, session_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row["title"] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _dedupe_repeated_replies(out: list[dict]) -> list[dict]:
    """Collapse runs of identical assistant replies, keeping the NEWEST one.

    The model imitates patterns it sees in the window: once the bot falls into
    a rut (e.g. answering "Saya tidak yakin." to five questions in a row), the
    window keeps teaching it that repeating itself is what happens here, and
    it repeats even on fresh turns. Dropping all but the latest duplicate
    removes the run as soon as it forms, without touching what was persisted
    (view-time only, like sender labels).
    """
    deduped: list[dict] = []
    last_assistant: str | None = None
    for m in reversed(out):  # newest first
        t = None
        if m["role"] == "assistant" and isinstance(m["content"], str) \
                and not m.get("tool_calls"):
            t = m["content"].strip()
        if t is not None and t == last_assistant:
            continue  # older copy of a run — keep the newer one
        deduped.append(m)
        if t is not None:
            last_assistant = t
    return list(reversed(deduped))


def _label_user_message(m: dict, sender: str | None, user_id: int | None) -> dict:
    """Prefix a user message with its speaker: ``[Name] ...`` (view-time only)."""
    name = sender or (f"id:{user_id}" if user_id else None)
    if not name:
        return m
    prefix = f"[{name}] "
    content = m.get("content")
    if isinstance(content, str):
        m["content"] = prefix + content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                part["text"] = prefix + part["text"]
                break
    return m


def _preview_text(raw: str) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            if "parts" in data:
                bits = [p.get("text", "") for p in data["parts"] if isinstance(p, dict) and p.get("type") == "text"]
                return " ".join(b for b in bits if b)
            return data.get("text", "")
        return json.dumps(data)
    except json.JSONDecodeError:
        return raw
