"""Persistent per-user facts store (ported from WhatsBot's database.py).

Durable facts about users — names, preferences, plans, relationships —
extracted from their messages and injected into the model's system prompt.
Lives in its own database (``data/facts.db``), independent of session mode.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("bot.facts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    entity TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    created_at INTEGER NOT NULL,
    last_confirmed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts (user_id, last_confirmed DESC);
"""


class FactsStore:
    """Per-user durable facts in SQLite. One connection, guarded by a lock."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save_fact(self, user_id: str, fact: str, entity: str, confidence: float,
                  now: int | None = None) -> int:
        """Insert a fact, or update the existing one if (user_id, entity, fact)
        matches (confidence keeps the higher value, last_confirmed is bumped).
        Returns the fact ID."""
        if now is None:
            now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT id, confidence FROM facts WHERE user_id = ? AND entity = ? AND fact = ?",
                (user_id, entity, fact),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE facts SET confidence = ?, last_confirmed = ? WHERE id = ?",
                    (max(confidence, row["confidence"]), now, row["id"]),
                )
                self._conn.commit()
                return row["id"]
            cur = self._conn.execute(
                "INSERT INTO facts (user_id, fact, entity, confidence, created_at, last_confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, fact, entity, confidence, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_facts(self, user_id: str, limit: int = 10) -> list[tuple]:
        """Most recently confirmed facts for a user.
        Returns [(id, fact, entity, confidence, created_at, last_confirmed)]."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fact, entity, confidence, created_at, last_confirmed "
                "FROM facts WHERE user_id = ? ORDER BY last_confirmed DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [tuple(r) for r in rows]

    def delete_fact(self, fact_id: int) -> bool:
        """Delete a fact by ID. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def get_fact_count(self, user_id: str | None = None) -> int:
        """Total fact count (all users if user_id is None)."""
        with self._lock:
            if user_id is None:
                row = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE user_id = ?", (user_id,)
                ).fetchone()
        return row[0]

    def prune_stale_facts(self, cutoff_days: int = 60) -> int:
        """Delete facts not re-confirmed within cutoff_days. Returns rowcount."""
        cutoff = int(time.time()) - cutoff_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM facts WHERE last_confirmed < ?", (cutoff,)
            )
            self._conn.commit()
        return cur.rowcount
