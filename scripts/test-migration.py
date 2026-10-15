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
shutil.copy(Path(sys.argv[1]), tmp / "sessions.db")
store = SessionStore(tmp)

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
big = next((r for r in shared if r["session_key"] == "ch-1560173151550308506"), None)
if big:
    n = store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (big["id"],)
    ).fetchone()[0]
    # All legacy per-user sessions of the channel (any end state — history from
    # sessions ended by /new is channel history too) must be in the shared one.
    expected = store._conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE s.session_key LIKE 'ch-1560173151550308506-%'"
    ).fetchone()[0]
    check(f"big channel merged all per-user messages (expected {expected})", n == expected,
          f"got {n}")
    # ids in chronological order
    rows = store._conn.execute(
        "SELECT id, timestamp FROM messages WHERE session_id = ? ORDER BY id", (big["id"],)
    ).fetchall()
    ts = [r["timestamp"] for r in rows]
    check("merged ids are chronological", ts == sorted(ts))
    # binding points at it
    bound = store._conn.execute(
        "SELECT session_id FROM chat_sessions WHERE session_key = 'ch-1560173151550308506'"
    ).fetchone()
    check("chat_sessions bound to shared session", bound and bound["session_id"] == big["id"])

    # history with labels: user rows must be [id:...] prefixed (legacy has no names)
    hist = store.history(big["id"], 5, label_senders=True)
    user_msgs = [m for m in hist if m["role"] == "user"]
    labeled = [m for m in user_msgs if _text_of(m).startswith("[id:")]
    check("legacy history user rows labeled [id:…]",
          bool(labeled) and len(labeled) == len(user_msgs),
          f"{len(labeled)}/{len(user_msgs)}")
    sample = _text_of(labeled[0])[:80] if labeled else "(none)"
    print(f"     sample: {sample!r}")
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
