"""Test the SESSION_MODE split: per-mode databases, migrations, mode-aware keys.

Everything runs against throwaway temp dirs — safe to run while the bot is up.
Usage: .venv/bin/python scripts/test-session-modes.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.store import SessionStore, db_name_for_mode  # noqa: E402

failures = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _write_legacy_json(d: Path, chan: int, user_a: int, user_b: int) -> None:
    sess_dir = d / "sessions"
    sess_dir.mkdir()
    (sess_dir / f"ch-{chan}-{user_a}.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "hey"}, {"role": "assistant", "content": "hi"}]}))
    (sess_dir / f"ch-{chan}-{user_b}.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "hello"}]}))
    (sess_dir / f"dm-{user_a}.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "dm hi"}]}))


# ---- 1. shared mode: legacy sessions.db is renamed on first open --------------
print("== shared mode: legacy sessions.db auto-rename ==")
tmp = Path(tempfile.mkdtemp(prefix="smodes-"))
(tmp / "sessions.db").write_bytes(b"")
store = SessionStore(tmp, mode="shared")
check("legacy renamed to sessions_shared.db",
      not (tmp / "sessions.db").exists() and (tmp / "sessions_shared.db").exists())
check("db_name_for_mode", db_name_for_mode("shared") == "sessions_shared.db"
      and db_name_for_mode("per_user") == "sessions_per_user.db")
store.close()
# Reopen: rename is a no-op, no data loss
store2 = SessionStore(tmp, mode="shared")
check("reopen keeps renamed db", (tmp / "sessions_shared.db").exists())
store2.close()

# ---- 2. per_user mode: fresh start, no merge migration -------------------------
print("== per_user mode: fresh empty dir ==")
tmp2 = Path(tempfile.mkdtemp(prefix="smodes-"))
p = SessionStore(tmp2, mode="per_user")
check("per_user db created", (tmp2 / "sessions_per_user.db").exists())
check("per_user starts empty",
      p._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0)
p.close()

# ---- 3. per_user mode with legacy JSON: imported, NOT merged --------------------
print("== per_user mode: legacy JSON import (no merge) ==")
tmp3 = Path(tempfile.mkdtemp(prefix="smodes-"))
CHAN, UA, UB = 111222, 9001, 9002
_write_legacy_json(tmp3, CHAN, UA, UB)
p = SessionStore(tmp3, mode="per_user")
keys = {r["session_key"] for r in p._conn.execute("SELECT session_key FROM sessions")}
check("per_user: ch-{chan}-{user} keys kept",
      {f"ch-{CHAN}-{UA}", f"ch-{CHAN}-{UB}"} <= keys, str(keys))
check("per_user: dm kept", f"dm-{UA}" in keys, str(keys))
check("per_user: NO shared ch-{chan} session (no merge ran)", f"ch-{CHAN}" not in keys, str(keys))
bound = {r["session_key"] for r in p._conn.execute("SELECT session_key FROM chat_sessions")}
check("per_user: bindings point at per-user sessions",
      {f"ch-{CHAN}-{UA}", f"ch-{CHAN}-{UB}", f"dm-{UA}"} <= bound, str(bound))
p.close()

# ---- 4. shared mode with the same JSON: imported AND merged --------------------
print("== shared mode: same legacy JSON (import + merge) ==")
tmp4 = Path(tempfile.mkdtemp(prefix="smodes-"))
_write_legacy_json(tmp4, CHAN, UA, UB)
s = SessionStore(tmp4, mode="shared")
keys = {r["session_key"] for r in s._conn.execute("SELECT session_key FROM sessions")}
check("shared: merged ch-{chan} session exists", f"ch-{CHAN}" in keys, str(keys))
merged = s._conn.execute(
    "SELECT COUNT(*) c FROM messages WHERE session_id = "
    "(SELECT session_id FROM chat_sessions WHERE session_key = ?)", (f"ch-{CHAN}",),
).fetchone()["c"]
check("shared: merged all 3 JSON messages into it", merged == 3, f"got {merged}")
s.close()

# ---- 5. isolation: same data dir, two modes ------------------------------------
print("== isolation: two modes, one data dir ==")
tmp5 = Path(tempfile.mkdtemp(prefix="smodes-"))
s1 = SessionStore(tmp5, mode="shared")
sid = s1.ensure_session("ch-42", user_id=1)
s1.append(sid, ("user", "hello shared"))
s1.close()
p1 = SessionStore(tmp5, mode="per_user")
check("per_user db empty despite shared writes",
      p1._conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0)
pid = p1.ensure_session("ch-42-1", user_id=1)
p1.append(pid, ("user", "hello per-user"))
p1.close()
s2 = SessionStore(tmp5, mode="shared")
n = s2._conn.execute(
    "SELECT COUNT(*) c FROM messages WHERE session_id = ?", (sid,),
).fetchone()["c"]
check("shared db unaffected by per_user writes", n == 1, f"got {n}")
s2.close()

# ---- 6. mode-aware chat keys (BotClient) ----------------------------------------
print("== BotClient: mode-aware chat keys ==")
import discord  # noqa: E402
from bot.bot import BotClient  # noqa: E402
from bot.config import Config  # noqa: E402


class FakeChannel:  # a plain server channel (not a DMChannel)
    def __init__(self, cid):
        self.id = cid


class FakeDM(discord.DMChannel):
    def __init__(self, uid):
        self._uid = uid


class FakeAuthor:
    def __init__(self, uid):
        self.id = uid
        self.display_name = "Shyn"


class FakeMsg:
    def __init__(self, channel, uid):
        self.channel = channel
        self.author = FakeAuthor(uid)


def make_client(tmpdir: Path, mode: str) -> BotClient:
    cfg = Config(
        discord_token="t", allowed_users=frozenset(), require_mention=True,
        owner_id=None, model_base_url="http://localhost:1/v1", model_name="m",
        model_api_key=None, searxng_url="http://localhost:1", giphy_api_key=None,
        klipy_api_key=None, gif_provider="auto", session_mode=mode,
        max_history_messages=10, max_attachment_bytes=1000, log_level="INFO",
        data_dir=tmpdir, downloads_dir=tmpdir / "dl", soul_path=tmpdir / "SOUL.md",
    )
    return BotClient(cfg)


for mode, want_ch, want_db in (
    ("shared", "ch-555", "sessions_shared.db"),
    ("per_user", "ch-555-777", "sessions_per_user.db"),
):
    c = make_client(Path(tempfile.mkdtemp(prefix="smodes-")), mode)
    got = c._chat_key(FakeMsg(FakeChannel(555), 777))
    check(f"{mode}: channel key = {want_ch}", got == want_ch, f"got {got}")
    got_dm = c._chat_key(FakeMsg(FakeDM(777), 777))
    check(f"{mode}: dm key = dm-777", got_dm == "dm-777", f"got {got_dm}")
    check(f"{mode}: store on {want_db}", c.store.db_path.name == want_db,
          str(c.store.db_path.name))
    # interaction-style keying (slash commands)
    class FakeInter:
        pass
    inter = FakeInter()
    inter.channel = FakeChannel(555)
    inter.user = FakeAuthor(777)
    got_i = c._chat_key_for(inter)
    check(f"{mode}: interaction key = {want_ch}", got_i == want_ch, f"got {got_i}")
    c.store.close()
    c.model.close()
    c.web.close()

print()
if failures:
    print(f"FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
