"""Terminal CLI for DiscordBot (the `discordbot` command).

Usage:
  discordbot run                 run the bot in the foreground
  discordbot start|stop|restart  control the systemd user service
  discordbot status              service state + bot login + backend health
  discordbot logs [-f] [-n N]    show / follow logs
  discordbot health              model, SearXNG, Discord API checks
  discordbot sessions [status | search <q> | <terms>] [--limit N]
  discordbot session-mode [shared|per_user]
  discordbot config [KEY] [VALUE]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .config import SESSION_MODES
from .store import db_name_for_mode, db_path_for_mode

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "discordbot.service"


# ---- helpers -----------------------------------------------------------------

def _env() -> dict:
    """systemd --user needs XDG_RUNTIME_DIR + DBUS; headless shells often lack them."""
    uid = os.getuid()
    xrd = f"/run/user/{uid}"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", xrd)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={xrd}/bus")
    return env


def _sh(cmd: list[str], follow: bool = False) -> int:
    try:
        if follow:
            return subprocess.call(cmd, env=_env())
        return subprocess.call(cmd, env=_env())
    except FileNotFoundError:
        print(f"command not found: {cmd[0]}")
        return 127


def _load_dotenv() -> dict:
    vals: dict[str, str] = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    for k, v in os.environ.items():
        vals.setdefault(k, v)
    return vals


def _mask(v: str) -> str:
    return v[:6] + "…" if len(v) > 10 else v


def _ago(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _ok(label: str, detail: str = "") -> str:
    return f"  ✓ {label}" + (f" — {detail}" if detail else "")


def _fail(label: str, detail: str = "") -> str:
    return f"  ✗ {label}" + (f" — {detail}" if detail else "")


# ---- service commands ----------------------------------------------------------

def cmd_run(_: argparse.Namespace) -> int:
    from .main import main
    return main()


def _service(action: str, verbose: bool = True) -> int:
    cmd = ["systemctl", "--user", action, SERVICE]
    if verbose:
        print(f"$ {' '.join(cmd)}")
    return _sh(cmd)


def cmd_start(_: argparse.Namespace) -> int:
    r = _service("start")
    time.sleep(3)
    return r or cmd_status(None)


def cmd_stop(_: argparse.Namespace) -> int:
    return _service("stop")


def cmd_restart(_: argparse.Namespace) -> int:
    r = _service("restart")
    time.sleep(3)
    return r or cmd_status(None)


def _journal(lines: int) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE, "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, env=_env(), timeout=10,
        )
        return out.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _bot_login_line() -> str:
    for line in reversed(_journal(200).splitlines()):
        if "logged in as" in line:
            m = re.search(r"logged in as (.+?) \|", line)
            return m.group(1) if m else line.strip()
    return ""


def cmd_status(_args: argparse.Namespace) -> int:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE],
        capture_output=True, text=True, env=_env(),
    ).stdout.strip()
    print(f"service:  {active if active else 'not running'}")
    bot = _bot_login_line()
    print(f"bot:      {bot or 'no login recorded yet'}")
    for line in _health_lines():
        print(line)
    return 0 if active == "active" else 1


def cmd_logs(args: argparse.Namespace) -> int:
    cmd = ["journalctl", "--user", "-u", SERVICE, "--no-pager", "-n", str(args.lines)]
    if args.follow:
        cmd = cmd[:-2] + ["-f"]
    return _sh(cmd, follow=args.follow)


# ---- health ----------------------------------------------------------------------

def _health_lines() -> list[str]:
    import httpx
    vals = _load_dotenv()
    lines: list[str] = []

    base = vals.get("MODEL_BASE_URL", "http://localhost:27906/v1").rstrip("/")
    model = vals.get("MODEL_NAME", "")
    try:
        r = httpx.get(f"{base}/models", timeout=5)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])]
        mark = "ready" if model in ids else f"model '{model}' not in list"
        lines.append(_ok("model endpoint", mark))
    except Exception as e:
        lines.append(_fail("model endpoint", f"{base} unreachable ({e.__class__.__name__})"))

    srx = vals.get("SEARXNG_URL", "http://localhost:8080").rstrip("/")
    try:
        r = httpx.get(f"{srx}/search", params={"q": "test", "format": "json"}, timeout=5)
        r.raise_for_status()
        n = len(r.json().get("results", []))
        lines.append(_ok("searxng", f"{n} sample results"))
    except Exception:
        lines.append(_fail("searxng", f"{srx} unreachable"))

    token = vals.get("DISCORD_BOT_TOKEN", "")
    if token:
        try:
            r = httpx.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"}, timeout=5,
            )
            name = r.json().get("username", "?") if r.status_code == 200 else f"HTTP {r.status_code}"
            lines.append(_ok("discord token", name) if r.status_code == 200 else _fail("discord token", str(name)))
        except Exception as e:
            lines.append(_fail("discord token", e.__class__.__name__))
    else:
        lines.append(_fail("discord token", "DISCORD_BOT_TOKEN not set"))
    return lines


def cmd_health(_args: argparse.Namespace) -> int:
    vals = _load_dotenv()
    print(f"model:    {vals.get('MODEL_NAME', '?')} @ {vals.get('MODEL_BASE_URL', '?')}")
    print(f"search:   {vals.get('SEARXNG_URL', '?')}")
    print(f"allowlist: {vals.get('DISCORD_ALLOWED_USERS') or 'ALL users'}")
    print(f"owner:    {vals.get('OWNER_ID', 'not set')}")
    print()
    for line in _health_lines():
        print(line)
    return 0


# ---- sessions ----------------------------------------------------------------------

def _active_mode() -> str:
    vals = _load_dotenv()
    mode = (vals.get("SESSION_MODE", "shared") or "shared").strip().lower()
    return mode if mode in SESSION_MODES else "shared"


def _db_path() -> Path:
    """The session DB for the ACTIVE mode (each mode has its own file).

    Shared mode falls back to the pre-split sessions.db until the bot renames
    it on startup (see store.db_path_for_mode).
    """
    return db_path_for_mode(ROOT / "data", _active_mode())


def _open_db() -> sqlite3.Connection:
    db = _db_path()
    if not db.exists():
        mode = _active_mode()
        print(f"no session database for mode '{mode}' yet: {db}")
        print("start the bot in this mode first — it creates the DB on first use")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _print_sessions(rows: list[sqlite3.Row]) -> int:
    if not rows:
        print("no sessions found.")
        return 0
    for i, r in enumerate(rows, 1):
        title = r["title"] or "(untitled)"
        ended = "" if r["ended_at"] is None else " (ended)"
        print(f"[{i}] {title} — {r['message_count']} msgs, {_ago(r['last_activity_at'])} "
              f"key={r['session_key']} id={r['id']}{ended}")
    return 0


def cmd_sessions_status() -> int:
    conn = _open_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        named = conn.execute("SELECT COUNT(*) c FROM sessions WHERE title IS NOT NULL").fetchone()["c"]
        msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        db_path = _db_path()
        db_size = db_path.stat().st_size
        print(f"mode: {_active_mode()} (db: data/{db_path.name})")
        print(f"sessions: {total} total ({named} named, {total - named} unnamed) | {msgs} messages | db {db_size // 1024} KB")
        print()
        print("current chat bindings:")
        rows = conn.execute(
            """SELECT c.session_key, c.session_id, s.title, s.message_count, s.ended_at
               FROM chat_sessions c JOIN sessions s ON s.id = c.session_id
               ORDER BY s.last_activity_at DESC"""
        ).fetchall()
        for r in rows:
            state = "ended" if r["ended_at"] else "active"
            title = r["title"] or "(untitled)"
            print(f"  {r['session_key']} -> {r['session_id']} ({state}, {r['message_count']} msgs, {title})")
        ended = conn.execute(
            "SELECT id, session_key, title, end_reason FROM sessions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 10"
        ).fetchall()
        if ended:
            print()
            print("recently ended:")
            for r in ended:
                t = r["title"] or "(untitled)"
                print(f"  {r['id']} ({r['session_key']}, {t}) — {r['end_reason']}")
        return 0
    finally:
        conn.close()


def cmd_sessions(args: argparse.Namespace) -> int:
    terms = [t for t in args.terms if t.lower() not in ("list", "ls", "browse")]
    if terms and terms[0].lower() == "status":
        return cmd_sessions_status()
    if terms and terms[0].lower() in ("search", "find"):
        terms = terms[1:]
    query = " ".join(terms).strip()
    conn = _open_db()
    try:
        if query:
            like = f"%{query.lower()}%"
            rows = conn.execute(
                """SELECT s.id, s.session_key, s.title, s.message_count, s.last_activity_at, s.ended_at
                   FROM sessions s
                   WHERE (lower(s.title) LIKE ? OR EXISTS
                         (SELECT 1 FROM messages m WHERE m.session_id = s.id AND lower(m.content) LIKE ?))
                   ORDER BY s.last_activity_at DESC LIMIT ?""",
                (like, like, args.limit),
            ).fetchall()
            print(f"search: {query!r}")
        else:
            rows = conn.execute(
                """SELECT id, session_key, title, message_count, last_activity_at, ended_at
                   FROM sessions ORDER BY last_activity_at DESC LIMIT ?""",
                (args.limit,),
            ).fetchall()
        return _print_sessions(rows)
    finally:
        conn.close()


# ---- session mode ------------------------------------------------------------------

def cmd_session_mode(args: argparse.Namespace) -> int:
    """Show the current SESSION_MODE, or switch and restart to apply it.

    Each mode has its own database (see bot/store.py); switching never loses
    history — it just reads/writes a different DB from the next start.
    """
    current = _active_mode()
    if not args.mode:
        print(f"session mode: {current} — db: data/{db_name_for_mode(current)}")
        print(f"switch: discordbot session-mode {'|'.join(SESSION_MODES)} (writes .env + restarts the service)")
        return 0
    if args.mode == current:
        print(f"already in {args.mode} mode")
        return 0
    set_env_value("SESSION_MODE", args.mode)
    print(f"set SESSION_MODE={args.mode} — restarting the service to apply")
    r = _service("restart")
    time.sleep(3)
    return r or cmd_status(None)


# ---- config ----------------------------------------------------------------------

def _is_secret(key: str) -> bool:
    return any(t in key.upper() for t in ("TOKEN", "KEY", "SECRET"))


def set_env_value(key: str, value: str) -> None:
    """Write KEY=VALUE into .env (replacing the line if already present).

    Used by `discordbot config` and by the in-Discord /session-mode command.
    """
    env_path = ROOT / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    lines = text.splitlines()
    new_line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$")
    for i, line in enumerate(lines):
        if pat.match(line.strip()):
            lines[i] = new_line
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new_line)
    env_path.write_text("\n".join(lines) + "\n")


def cmd_config(args: argparse.Namespace) -> int:
    env_path = ROOT / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    lines = text.splitlines()
    vals = _load_dotenv()

    if not args.key:
        # list all
        for k, v in vals.items():
            shown = _mask(v) if _is_secret(k) else v
            print(f"{k}={shown}")
        return 0

    if not args.value:
        v = vals.get(args.key)
        if v is None:
            print(f"{args.key} is not set")
            return 1
        print(_mask(v) if _is_secret(args.key) else v)
        return 0

    # set
    set_env_value(args.key, args.value)
    print(f"set {args.key} — restart the bot to apply:  discordbot restart")
    return 0


# ---- entry point -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discordbot",
        description="Terminal control for the standalone Discord chat bot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="run the bot in the foreground (Ctrl-C to stop)")
    sp.set_defaults(fn=cmd_run)

    for name, fn, help_ in (
        ("start", cmd_start, "start the systemd user service"),
        ("stop", cmd_stop, "stop the service"),
        ("restart", cmd_restart, "restart the service"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("status", help="service + bot + backend status")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("logs", help="show logs (-f to follow)")
    sp.add_argument("-f", "--follow", action="store_true", help="follow log output")
    sp.add_argument("-n", "--lines", type=int, default=50, help="lines to show (default 50)")
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("health", help="check model, SearXNG, Discord token, allowlist")
    sp.set_defaults(fn=cmd_health)

    sp = sub.add_parser("sessions", help="list or search sessions from the database")
    sp.add_argument("terms", nargs="*",
                    help="'status' = overview, 'search <q>' or bare terms = search, nothing = list recent")
    sp.add_argument("--limit", type=int, default=15, help="max rows (default 15)")
    sp.set_defaults(fn=cmd_sessions)

    sp = sub.add_parser("session-mode", help="show or switch session mode (shared | per_user); switching restarts the service")
    sp.add_argument("mode", nargs="?", choices=SESSION_MODES, default=None,
                    help="which session design to use (omit to show the current mode)")
    sp.set_defaults(fn=cmd_session_mode)

    sp = sub.add_parser("config", help="show or set .env values")
    sp.add_argument("key", nargs="?", default=None, help="variable name")
    sp.add_argument("value", nargs="?", default=None, help="new value (to set)")
    sp.set_defaults(fn=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
