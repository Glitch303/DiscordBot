"""Throwaway verification for the WhatsBot-style memory port.

Run: .venv/bin/python scripts/test_memory.py
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.facts import FactsStore
from bot.memory import (
    _cleanup_loop,
    extract_facts,
    facts_text,
    normalize_entity,
    summarize_session,
    summary_text,
    user_text_from_parts,
)
from bot.store import SessionStore

FAILS = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeModel:
    """Canned responses: summary first, then fact JSON (per call)."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "conversation summarizer" in system:
            return {"role": "assistant", "content": "User is Shyn, likes coffee, planning a trip to Bali."}
        if "fact extractor" in system:
            return {
                "role": "assistant",
                "content": '```json\n[{"fact": "user likes coffee", "entity": "User", "confidence": 0.9}, {"fact": "user is planning a trip to Bali", "entity": "user", "confidence": 0.8}]\n```',
            }
        return {"role": "assistant", "content": ""}


print("== SessionStore memory methods ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    store = SessionStore(d, mode="shared")
    key = "dm-123"
    sid = store.ensure_session(key, 123)
    check("ensure_session returns id", sid.startswith("s-"))

    # 12 user turns, 1s apart, starting at t0
    t0 = time.time() - 1000
    for i in range(12):
        store.append(sid, ("user", f"message {i}"), user_id=123, sender="Shyn",
                     timestamp=t0 + i)
        store.append(sid, ("assistant", f"reply {i}"), timestamp=t0 + i + 0.5)

    check("user_message_count == 12", store.user_message_count(sid) == 12,
          str(store.user_message_count(sid)))

    # tail of 10 user turns starts at user turn #2 (0-based), ts = t0+2
    tail_ts = store.oldest_tail_timestamp(sid, 10)
    check("oldest_tail_timestamp == t0+2", tail_ts is not None and abs(tail_ts - (t0 + 2)) < 0.01,
          f"got {tail_ts}, want {t0 + 2}")

    msgs = store.messages_since(sid, 0)
    check("messages_since returns all 24", len(msgs) == 24, str(len(msgs)))
    check("messages_since oldest first", msgs[0][0] == "message 0" and msgs[0][2] == "user")

    # summarize: should only summarize messages before tail_ts (turns 0,1 + replies)
    model = FakeModel()
    ok = summarize_session(store, model, sid, 10)
    check("summarize_session returns True", ok)
    summary, last_ts = store.get_summary(sid)
    check("summary stored", summary == "User is Shyn, likes coffee, planning a trip to Bali.",
          repr(summary))
    check("last_summarized_timestamp == t0+1.5", last_ts is not None and abs(last_ts - (t0 + 1.5)) < 0.01,
          f"got {last_ts}, want {t0 + 1.5}")
    # the summary input must NOT contain any tail message (turn >= 2)
    sent = model.calls[0][0]["content"]
    check("summary input excludes tail", "message 2" not in sent and "message 1" in sent)

    # second summarize: nothing new before tail → False, no model call
    ok2 = summarize_session(store, model, sid, 10)
    check("second summarize is no-op", not ok2 and len(model.calls) == 1)

    # add 3 more turns, then summarize again: only new pre-tail messages
    for i in range(3):
        store.append(sid, ("user", f"message {12 + i}"), user_id=123, sender="Shyn",
                     timestamp=t0 + 12 + i)
        store.append(sid, ("assistant", f"reply {12 + i}"), timestamp=t0 + 12 + i + 0.5)
    ok3 = summarize_session(store, model, sid, 10)
    check("third summarize returns True", ok3)
    sent3 = model.calls[1][0]["content"]
    check("merged summary includes existing", "Existing summary" in sent3 and "likes coffee" in sent3)
    # at 15 turns the tail-10 window starts at user turn 5: turns 2-4 are new, 5+ stay verbatim
    check("third summarize only new msgs", "message 3" in sent3 and "message 5" not in sent3)
    check("summary capped at 1200", len(store.get_summary(sid)[0]) <= 1200)

    check("summary_text formats",
          summary_text(store, sid) == "[CONVERSATION SUMMARY]\nUser is Shyn, likes coffee, planning a trip to Bali.")
    store.clear_summary(sid)
    check("clear_summary", store.get_summary(sid) == (None, None))

    # prune: messages older than cutoff (24 initial + 6 from the extra 3 turns = 30)
    pruned = store.prune_old_messages(sid, cutoff_days=0)  # cutoff = now → all old (t0 < now)
    check("prune_old_messages prunes all old", pruned == 30, str(pruned))
    check("message_count recomputed", store.session_info(sid)["message_count"] == 0,
          str(store.session_info(sid)["message_count"]))

    # session_info / stats_for_key
    store.append(sid, ("user", "fresh"), user_id=123, sender="Shyn", timestamp=time.time())
    info = store.session_info(sid)
    check("session_info fields", info and info["id"] == sid and info["message_count"] == 1)
    stats = store.stats_for_key(key)
    check("stats_for_key", stats == {"sessions": 1, "messages": 1}, str(stats))

    # delete_session removes summary too
    store.save_summary(sid, "x", time.time())
    store.delete_session(sid)
    check("delete_session clears summary", store.get_summary(sid) == (None, None))
    store.close()

print("== FactsStore ==")
with tempfile.TemporaryDirectory() as td:
    facts = FactsStore(Path(td) / "facts.db")
    uid = "123"
    fid1 = facts.save_fact(uid, "user likes coffee", "user", 0.9)
    fid2 = facts.save_fact(uid, "user is planning a trip to Bali", "user", 0.8)
    check("save_fact inserts", fid1 == 1 and fid2 == 2, f"{fid1},{fid2}")

    # dedupe: same (user, entity, fact) → same id, confidence max, last_confirmed bumped
    now = int(time.time())
    fid3 = facts.save_fact(uid, "user likes coffee", "user", 0.5, now=now - 10)
    check("dedupe returns existing id", fid3 == fid1, str(fid3))
    rows = facts.get_facts(uid, 10)
    coffee = [r for r in rows if r[1] == "user likes coffee"][0]
    check("dedupe keeps higher confidence", coffee[3] == 0.9, str(coffee[3]))
    check("dedupe bumps last_confirmed", coffee[5] == now - 10, str(coffee[5]))

    check("get_facts ordering (Bali newer)", rows[0][1] == "user is planning a trip to Bali", str(rows[0][1]))
    check("get_facts limit", len(facts.get_facts(uid, 1)) == 1)
    check("get_fact_count", facts.get_fact_count(uid) == 2)
    check("get_fact_count all", facts.get_fact_count() == 2)

    # prune: age the Bali fact past cutoff
    facts._conn.execute("UPDATE facts SET last_confirmed = ? WHERE id = ?", (now - 70 * 86400, fid2))
    facts._conn.commit()
    pruned = facts.prune_stale_facts(cutoff_days=60)
    check("prune_stale_facts", pruned == 1, str(pruned))
    check("pruned fact gone", facts.get_fact_count(uid) == 1)

    check("delete_fact", facts.delete_fact(fid1) and not facts.delete_fact(fid1))
    check("get_facts empty", facts.get_facts(uid) == [])
    facts.close()

print("== memory.py helpers ==")
check("normalize_entity", normalize_entity("  User!! ") == "user" and normalize_entity("") == "unknown")
check("user_text_from_parts", user_text_from_parts(
    [{"type": "text", "text": "  hi "}, {"type": "image", "image_url": "x"}, {"type": "text", "text": "there"}]
) == "hi there")

with tempfile.TemporaryDirectory() as td:
    facts = FactsStore(Path(td) / "facts.db")
    model = FakeModel()
    n = extract_facts(facts, model, "123", "I really like coffee, and I'm planning a trip to Bali")
    check("extract_facts saves 2", n == 2, str(n))
    rows = facts.get_facts("123")
    check("entities normalized", all(r[2] == "user" for r in rows), str([r[2] for r in rows]))
    check("confidence parsed", sorted(r[3] for r in rows) == [0.8, 0.9], str([r[3] for r in rows]))
    check("facts_text formats",
          facts_text(facts, "123", 8).startswith("[FACTS about this user]\n- "))
    check("facts_text empty", facts_text(facts, "999", 8) == "")

    # extract_facts: empty / garbage model output
    class BadModel:
        def chat(self, messages, tools=None):
            return {"role": "assistant", "content": "not json at all"}
    check("extract_facts bad json → 0", extract_facts(facts, BadModel(), "123", "hi") == 0)
    facts.close()

print("== cleanup loop ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    store = SessionStore(d, mode="shared")
    facts = FactsStore(d / "facts.db")
    sid = store.ensure_session("dm-9", 9)
    store.append(sid, ("user", "old"), user_id=9, sender="X", timestamp=time.time() - 100 * 86400)
    facts.save_fact("9", "old fact", "user", 0.5, now=int(time.time()) - 100 * 86400)

    # let the first prune pass run, then stop the loop on the second sleep
    import bot.memory as mem
    real_sleep = time.sleep
    calls = {"n": 0}

    def fake_sleep(s):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit  # ends the loop after the first prune pass

    mem.time.sleep = fake_sleep
    t = threading.Thread(target=_cleanup_loop, args=(store, facts, 30, 60), daemon=True)
    t.start()
    t.join(timeout=5)
    mem.time.sleep = real_sleep
    check("cleanup pruned old message", store.user_message_count(sid) == 0)
    check("cleanup pruned stale fact", facts.get_fact_count() == 0)
    check("cleanup ran", calls["n"] >= 2)
    store.close()
    facts.close()

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
