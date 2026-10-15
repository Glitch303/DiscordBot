"""Test the per-user → shared channel session migration + history labeling.

Runs against a throwaway copy of the real DB (passed as argv[1]); also tests a
brand-new empty DB.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.store import SessionStore  # noqa: E402

failures = []


def _text_of(m):
    c = m["content"]
    if isinstance(c, str):
        return c
    for p in c:
        if isinstance(p, dict) and p.get("type") == "text":
            return p["text"]
    return ""


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- 1. migration on a copy of the real DB --------------------------------
print("== migration on real DB copy ==")
tmp = Path(tempfile.mkdtemp(prefix="dbtest-"))
src = Path(sys.argv[1])
shutil.copy(src, tmp / "sessions.db")
# Copy the WAL sidecars too so the test sees un-checkpointed rows.
for suffix in ("-wal", "-shm"):
    side = src.with_name(src.name + suffix)
    if side.exists():
        shutil.copy(side, tmp / (src.name + suffix))
store = SessionStore(tmp, mode="shared")

with store._lock:
    chans = store._conn.execute(
        "SELECT id, session_key, message_count FROM sessions "
        "WHERE session_key LIKE 'ch-%' AND session_key NOT LIKE 'ch-%-%' "
        "ORDER BY session_key"
    ).fetchall()
    # NOTE: NOT LIKE 'ch-%-%' is wrong for real ids (no dashes) — count shared keys properly:
    shared = [r for r in store._conn.execute(
        "SELECT id, session_key, message_count, user_id FROM sessions"
    ).fetchall() if r["session_key"].count("-") == 1]
    migrated = [r for r in store._conn.execute(
        "SELECT id, session_key FROM sessions WHERE end_reason = 'migrated_to_shared'"
    ).fetchall()]
    total_msgs = store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

check("shared channel sessions created", len(shared) >= 3, f"got {len(shared)}")
check("legacy sessions marked migrated", len(migrated) >= 10, f"got {len(migrated)}")

# The big channel should now hold ALL of its former per-user messages
BIG_KEY = "ch-1560173151550308506"
# The merge lands in whichever shared session is current at the time; a later
# /new ends it and creates a fresh session with the SAME key — so use the
# fullest shared session of the channel for the checks below.
big_row = store._conn.execute(
    "SELECT s.id, COUNT(m.id) c FROM sessions s "
    "LEFT JOIN messages m ON m.session_id = s.id "
    "WHERE s.session_key = ? AND s.session_key NOT LIKE 'ch-%-%' "
    "GROUP BY s.id ORDER BY c DESC LIMIT 1", (BIG_KEY,)
).fetchone()
if big_row:
    big_id = big_row["id"]
    # All legacy per-user sessions of the channel (any end state — history from
    # sessions ended by /new is channel history too) must be in the shared one.
    # The merge copies rows with fresh ids, interleaved by timestamp, so check
    # each legacy session's content sequence as a subsequence of the channel's
    # shared-session content (order within a speaker is preserved).
    shared_msgs = []
    for r in store._conn.execute(
            "SELECT id FROM sessions WHERE session_key = ? "
            "AND session_key NOT LIKE 'ch-%-%' ORDER BY id", (BIG_KEY,)):
        shared_msgs.extend(row["content"] for row in store._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id", (r["id"],)))
    bad, total = [], 0
    for srow in store._conn.execute(
            "SELECT id FROM sessions WHERE session_key LIKE 'ch-1560173151550308506-%'"):
        seq = [r["content"] for r in store._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id", (srow["id"],))]
        total += len(seq)
        it = iter(shared_msgs)
        if not all(any(c == target for c in it) for target in seq):
            bad.append(srow["id"])
    check(f"big channel merged all {total} per-user messages (subsequence)", not bad,
          str(bad))
    # ids in chronological order
    rows = store._conn.execute(
        "SELECT id, timestamp FROM messages WHERE session_id = ? ORDER BY id", (big_id,)
    ).fetchall()
    ts = [r["timestamp"] for r in rows]
    check("merged ids are chronological", ts == sorted(ts))
    # binding exists and points at a session with the shared key (the live bot
    # may have re-bound it to a /new session since the merge)
    bound = store._conn.execute(
        "SELECT c.session_key FROM chat_sessions c "
        "WHERE c.session_key = 'ch-1560173151550308506'"
    ).fetchone()
    check("chat_sessions bound to a shared session", bound is not None)

    # legacy user rows (no sender name) survived the merge with sender NULL —
    # the [id:…] rendering of such rows is covered by the fresh-DB section
    legacy_null = store._conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE session_id = ? AND role = 'user' "
        "AND sender IS NULL", (big_id,)
    ).fetchone()["c"]
    check("merged legacy user rows kept (sender NULL)", legacy_null >= 1,
          f"got {legacy_null}")
else:
    check("big channel merged", False, "ch-1560173151550308506 shared session missing")

# ---- 2. idempotency: second store on same dir -------------------------------
print("== idempotency (reopen) ==")
store.close()
store2 = SessionStore(tmp)
total2 = store2._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
check("no duplicate rows after reopen", total2 == total_msgs, f"{total_msgs} -> {total2}")
store2.close()

# ---- 3. brand-new DB: fresh schema + labeled append/history ------------------
print("== fresh DB + labeling ==")
tmp2 = Path(tempfile.mkdtemp(prefix="dbtest2-"))
s = SessionStore(tmp2)
sid = s.ensure_session("ch-12345", user_id=111)
s.append(sid, ("user", [{"type": "text", "text": "hey yuki"}]), user_id=111, sender="Alice")
s.append(sid, ("assistant", "Hi Alice."))
s.append(sid, ("user", "what did she mean?"), user_id=222, sender="Bob")
s.append(sid, ("assistant", "She was greeting you, Bob."))
s.append(sid, ("user", "old legacy row"))  # no sender at all

hist = s.history(sid, 10, label_senders=True)
u = [m for m in hist if m["role"] == "user"]
check("3 user rows in history", len(u) == 3, f"got {len(u)}")
check("Alice labeled (parts list)", _text_of(u[0]).startswith("[Alice] "), repr(u[0]["content"]))
check("Bob labeled (plain string)", _text_of(u[1]).startswith("[Bob] "), repr(u[1]["content"]))
check("senderless row unlabeled", not _text_of(u[2]).startswith("["), repr(u[2]["content"]))

# unlabeled mode (DMs)
hist_dm = s.history(sid, 10, label_senders=False)
u2 = [m for m in hist_dm if m["role"] == "user"]
check("DM mode: no labels", not any(_text_of(m).startswith("[") for m in u2))

# persisted rows stay clean (labels are a read-time view, no double-labeling)
s2 = SessionStore(tmp2)
row = s2._conn.execute(
    "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
    (sid,),
).fetchone()
stored = row["content"]
check("stored content stays clean (no [Alice] prefix in DB)", "[Alice]" not in stored, stored[:60])
check("sender column stored", s2._conn.execute(
    "SELECT sender FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
    (sid,),
).fetchone()["sender"] == "Alice")
s2.close()

print()
if failures:
    print(f"FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
