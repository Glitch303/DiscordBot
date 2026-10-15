"""Session store: SQLite-backed sessions + messages (Hermes-style, standalone).

Design adapted from Hermes Agent's state layer (hermes_state_*.py), simplified:

- ``sessions`` table: every conversation is a row with a stable id, title,
  lineage (``parent_session_id``), and activity timestamps. Sessions are never
  deleted on ``/new`` — they're ended and kept, so they can be resumed.
- ``messages`` table: ALL messages are retained forever (Hermes keeps full
  history for search). Only a sliding window (``max_messages``) is loaded
  into the model context at read time.
- ``chat_sessions`` table: binds a chat key to its active session (Hermes
  calls this gateway routing). ``/resume`` simply re-points this binding.

This is what powers /new, /title, /sessions, and /resume.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("bot.store")

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
    timestamp REAL NOT NULL
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
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "sessions.db"
        self._lock = threading.RLock()  # reentrant: public methods may call each other
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.migrate_legacy_json()

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

    def append(self, session_id: str, *messages: tuple[str, object]) -> None:
        now = _now()
        with self._lock:
            for role, content in messages:
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (session_id, role, _content_to_json(content), now),
                )
            self._conn.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_activity_at = ? WHERE id = ?",
                (len(messages), now, session_id),
            )
            self._conn.commit()

    def history(self, session_id: str, max_messages: int) -> list[dict]:
        """Most recent messages for the model context (oldest first).

        The window is measured in user turns, but assistant tool-call rows and
        tool-result rows are kept intact with their turns — the model has to
        see past tool usage or it stops calling tools and starts inventing the
        results in plain text (see the "hallucinated GIF send" bug).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
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
                out.append(m)
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
