"""
WhatsBot-style interactive terminal menu for DiscordBot.

Uses Unix socket communication with the running bot for status, settings,
and session management. Live log streaming via socket.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from queue import Queue, Empty

# ANSI colors
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"


def _supports_color() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.name == "posix"


_COLOR = _supports_color()


def c(color: str, text: str) -> str:
    if _COLOR:
        return f"{color}{text}{C.RESET}"
    return text


def clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def print_banner() -> None:
    print()
    print(c(C.CYAN, "\n"))
    print(c(C.CYAN, "  +======================================+"))
    print(c(C.CYAN, "  |      DiscordBot  --  AI Assistant    |"))
    print(c(C.CYAN, "  +======================================+"))
    print()


def prompt_choice(max_num: int) -> int | None:
    """Read a menu choice. Returns int, or None on quit."""
    while True:
        try:
            raw = input(c(C.BOLD, "  > ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            continue
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw.isdigit():
            n = int(raw)
            if 0 <= n <= max_num:
                return n
            print(c(C.RED, f"  Enter 0-{max_num}."))
        else:
            print(c(C.RED, f"  Enter a number (0-{max_num}) or 'quit'."))


# --- Unix socket communication ---

_SOCKET_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "discordbot.sock")


def _send_command(verb: str, cmd: str = None, args: dict = None) -> dict:
    """Send a one-off command to the control server. Returns response dict."""
    if args is None:
        args = {}
    request = {"verb": verb, "cmd": cmd, "args": args}
    data = json.dumps(request).encode("utf-8") + b"\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(_SOCKET_PATH)
        sock.sendall(data)
        response_data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if response_data.endswith(b"\n"):
                break
        return json.loads(response_data.decode("utf-8"))
    except (ConnectionRefusedError, FileNotFoundError):
        return {"ok": False, "error": "bot not running"}
    finally:
        sock.close()


def _stream_logs():
    """Generator that yields log records from the control server."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(_SOCKET_PATH)
        request = {"verb": "log_stream"}
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")

        buffer = b""
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line:
                        yield line.decode("utf-8")
            except (ConnectionResetError, OSError):
                break
    finally:
        sock.close()


# --- Service management (via systemctl) ---


def _service(action: str) -> int:
    try:
        r = subprocess.run(
            ["systemctl", "--user", action, "discordbot.service"],
            capture_output=True, text=True,
        )
        if r.stdout:
            print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="")
        return r.returncode
    except FileNotFoundError:
        print(c(C.RED, "  systemctl not found"))
        return 127


def _is_running() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "discordbot.service"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() == "active"
    except FileNotFoundError:
        return False


def _get_status() -> dict:
    """Get bot status via Unix socket."""
    return _send_command("status")


# --- Menu rendering ---


def print_status_box(status: dict, wait_for_connect: bool = False) -> None:
    """Print the banner + top status box (no menu items).

    If wait_for_connect is True, poll until the bot connects or timeout.
    """
    print_banner()

    running = status.get("running", False)
    connected = status.get("connected", False)
    bot_name = status.get("bot_name", "")
    bot_id = status.get("bot_id", "")

    # If waiting for connection, poll until connected or timeout
    if wait_for_connect and running and not connected:
        print(c(C.CYAN, "  Waiting for bot to connect..."))
        for i in range(30):  # Timeout after 30 seconds
            time.sleep(1)
            status = _get_status()
            if not status.get("ok"):
                break
            data = status.get("data", {})
            running = data.get("running", False)
            connected = data.get("connected", False)
            bot_name = data.get("bot_name", "")
            bot_id = data.get("bot_id", "")
            if connected and bot_name:
                print(c(C.GREEN, "  Bot connected!"))
                break
            if not running:
                print(c(C.YELLOW, "  Bot stopped while waiting."))
                break
        else:
            print(c(C.YELLOW, "  Timeout waiting for bot to connect."))

    # Get model name (from status if running, from .env if stopped)
    model = status.get("model", "")
    if not model:
        model = _get_current_model()
    if not model:
        model = "unknown"

    # Check model load status
    model_str = model
    try:
        models = _fetch_models()
        for m in models:
            if m["id"] == model:
                if m["status"] == "loaded":
                    model_str = c(C.GREEN, model)
                elif m["status"] == "unloaded":
                    model_str = c(C.YELLOW, model)
                else:
                    model_str = c(C.RED, model)
                break
    except Exception:
        pass

    print(c(C.BOLD, "  Status:"))
    print()
    print(f"    Bot:         {'running' if running else 'stopped'}")
    if connected and bot_name:
        print(f"    Logged in:   {c(C.GREEN, f'{bot_name}#{bot_id}')}")
    else:
        print(f"    Logged in:   {c(C.YELLOW, 'logged off')}")
    print(f"    Model:       {model_str}")
    print()


def print_main_items(connected: bool, running: bool) -> list:
    """Print the main menu item list. Returns the items."""
    items = [
        ("1", "Settings"),
        ("2", "Sessions"),
    ]

    if running:
        items.append(("3", "Stop bot"))
    else:
        items.append(("3", "Start bot"))

    items.extend([
        ("4", "Restart bot"),
        ("5", "Status"),
        ("6", "Logs"),
        ("7", "Health check"),
        ("0", "Quit"),
    ])

    for num, label in items:
        print(f"    [{num}] {label}")
    return items


def print_standby_menu(status: dict) -> list:
    """Print the full standby screen: status box + main menu items."""
    print_status_box(status)
    return print_main_items(
        status.get("connected", False),
        status.get("running", False),
    )


# --- Model selection submenu ---


def _get_current_model() -> str:
    """Get current model name from .env."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("MODEL_NAME="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def _set_model(model_id: str) -> None:
    """Set model name in .env."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    lines = []
    found = False
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("MODEL_NAME="):
                    lines.append(f"MODEL_NAME={model_id}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"MODEL_NAME={model_id}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


def _fetch_models() -> list:
    """Fetch available models from the endpoint."""
    import httpx
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    base_url = "http://localhost:27906/v1"
    api_key = ""
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("MODEL_BASE_URL="):
                    base_url = line.split("=", 1)[1].strip()
                elif line.strip().startswith("MODEL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        r = httpx.get(f"{base_url}/models", headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        models = []
        for m in data.get("data", []):
            model_id = m.get("id", "")
            name = m.get("name", model_id)
            status = m.get("status", {})
            status_value = status.get("value", "unknown") if isinstance(status, dict) else str(status)
            models.append({
                "id": model_id,
                "name": name,
                "status": status_value,
            })
        return models
    except Exception as e:
        print(c(C.RED, f"  Error fetching models: {e}"))
        return []


def _model_selection_menu() -> None:
    """Model selection submenu."""
    while True:
        clear()
        print_banner()
        print(c(C.BOLD, "  Select Model:"))
        print()

        current_model = _get_current_model()
        print(c(C.CYAN, f"  Current: {current_model}"))
        print()

        print(c(C.CYAN, "  Fetching available models..."))
        models = _fetch_models()

        if not models:
            print(c(C.YELLOW, "  No models found."))
            input("  Press Enter...")
            return

        print()
        print(c(C.BOLD, "  Available models:"))
        print()

        # Show models with status
        for i, m in enumerate(models, 1):
            status = m["status"]
            if status == "loaded":
                status_color = C.GREEN
            elif status == "unloaded":
                status_color = C.YELLOW
            else:
                status_color = C.RED

            if m["id"] == current_model:
                marker = " *"
            else:
                marker = ""

            print(f"    [{i}] {m['name']} ({m['id']}) - {c(status_color, status)}{marker}")

        print()
        print("    (*) = current model")
        print()

        # Prompt for selection
        try:
            raw = input(c(C.BOLD, "  Select model (number or ID): ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not raw:
            print(c(C.YELLOW, "  Model unchanged."))
            input("  Press Enter...")
            continue

        # Check if it's a number
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                selected = models[idx]["id"]
            else:
                print(c(C.RED, f"  Enter 1-{len(models)}."))
                input("  Press Enter...")
                continue
        else:
            # Try to match by ID
            selected = raw
            found = any(m["id"] == raw for m in models)
            if not found:
                print(c(C.RED, f"  Model '{raw}' not found."))
                input("  Press Enter...")
                continue

        print(c(C.CYAN, f"  Setting model to {selected}..."))
        _set_model(selected)
        print(c(C.GREEN, "  Model updated. Restart to apply."))
        input("  Press Enter...")
        return


# --- Settings submenu ---


def _get_session_mode() -> str:
    """Get current session mode from .env."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("SESSION_MODE="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return "shared"


def _get_require_mention() -> bool:
    """Get current require-mention setting from .env."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("REQUIRE_MENTION="):
                    return line.split("=", 1)[1].strip() == "1"
    except FileNotFoundError:
        pass
    return True


def _settings_menu() -> None:
    """Settings submenu."""
    while True:
        clear()
        print_banner()
        print(c(C.BOLD, "  Settings:"))
        print()

        mode = _get_session_mode()
        require_mention = _get_require_mention()
        current_model = _get_current_model()

        items = [
            ("1", f"Session mode ({mode})"),
            ("2", f"Require mention ({'ON' if require_mention else 'off'})"),
            ("3", f"Model ({current_model or 'unknown'})"),
            ("4", "Discord token"),
            ("5", "Channel detection"),
            ("0", "Back"),
        ]
        for num, label in items:
            print(f"    [{num}] {label}")
        print()

        choice = prompt_choice(5)
        if choice is None or choice == 0:
            return

        if choice == 1:
            # Toggle session mode
            new_mode = "per_user" if mode == "shared" else "shared"
            print(c(C.CYAN, f"  Switching to {new_mode} mode..."))
            env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            lines = []
            found = False
            try:
                with open(env_path) as f:
                    for line in f:
                        if line.strip().startswith("SESSION_MODE="):
                            lines.append(f"SESSION_MODE={new_mode}\n")
                            found = True
                        else:
                            lines.append(line)
            except FileNotFoundError:
                pass
            if not found:
                lines.append(f"SESSION_MODE={new_mode}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            print(c(C.GREEN, "  Session mode updated. Restart to apply."))
            input("  Press Enter...")
        elif choice == 2:
            # Toggle require mention
            new_val = "0" if require_mention else "1"
            print(c(C.CYAN, f"  Setting REQUIRE_MENTION={new_val}..."))
            env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            lines = []
            found = False
            try:
                with open(env_path) as f:
                    for line in f:
                        if line.strip().startswith("REQUIRE_MENTION="):
                            lines.append(f"REQUIRE_MENTION={new_val}\n")
                            found = True
                        else:
                            lines.append(line)
            except FileNotFoundError:
                pass
            if not found:
                lines.append(f"REQUIRE_MENTION={new_val}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            print(c(C.GREEN, "  Require mention updated. Restart to apply."))
            input("  Press Enter...")
        elif choice == 3:
            # Model selection
            _model_selection_menu()
        elif choice == 4:
            # Set Discord token
            print()
            print(c(C.CYAN, "  Enter Discord bot token:"))
            try:
                token = input(c(C.BOLD, "  Token: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                input("  Press Enter...")
                continue
            if not token:
                print(c(C.YELLOW, "  Token unchanged."))
                input("  Press Enter...")
                continue
            print(c(C.CYAN, "  Setting DISCORD_BOT_TOKEN..."))
            env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            lines = []
            found = False
            try:
                with open(env_path) as f:
                    for line in f:
                        if line.strip().startswith("DISCORD_BOT_TOKEN="):
                            lines.append(f"DISCORD_BOT_TOKEN={token}\n")
                            found = True
                        else:
                            lines.append(line)
            except FileNotFoundError:
                pass
            if not found:
                lines.append(f"DISCORD_BOT_TOKEN={token}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            print(c(C.GREEN, "  Discord token updated. Restart to apply."))
            input("  Press Enter...")
        elif choice == 5:
            # Channel detection management
            _channel_detection_menu()


# --- Channel detection submenu ---


def _channel_detection_menu() -> None:
    """Channel detection management submenu."""
    while True:
        clear()
        print_banner()
        print(c(C.BOLD, "  Channel Detection:"))
        print()
        print(c(C.DIM, "  Channels where the bot has chatted can be enabled/disabled."))
        print(c(C.DIM, "  Disabled channels won't create new sessions."))
        print()

        # List channels with sessions
        channels = _list_channels_with_sessions()
        ignored = _get_ignored_channels()
        if channels:
            print(c(C.CYAN, "  Channels with sessions:"))
            print()
            for i, ch in enumerate(channels, 1):
                is_ignored = int(ch["id"]) in ignored
                if is_ignored:
                    name_str = c(C.RED, f"✗ {ch['name']}")
                else:
                    name_str = c(C.GREEN, f"✓ {ch['name']}")
                print(f"    {i}. {name_str} (#{ch['id']}) - {ch['messages']} messages")
            print()
        else:
            print(c(C.YELLOW, "  No channels with sessions found."))
            print()

        items = [
            ("1", "Disable channel"),
            ("2", "Enable channel"),
            ("0", "Back"),
        ]
        for num, label in items:
            print(f"    [{num}] {label}")
        print()

        choice = prompt_choice(2)
        if choice is None or choice == 0:
            return

        if choice == 1:
            # Disable channel
            if not channels:
                print(c(C.YELLOW, "  No channels to disable."))
                input("  Press Enter...")
                continue
            print(c(C.CYAN, "  Enter channel number to disable:"))
            try:
                ch_num = int(input(c(C.BOLD, "  Channel: ")).strip())
            except (EOFError, KeyboardInterrupt, ValueError):
                print()
                input("  Press Enter...")
                continue
            if ch_num < 1 or ch_num > len(channels):
                print(c(C.YELLOW, "  Invalid channel number."))
                input("  Press Enter...")
                continue
            ch = channels[ch_num - 1]
            print(c(C.CYAN, f"  Disabling channel {ch['name']}..."))
            _disable_channel(ch["id"])
            print(c(C.GREEN, "  Channel disabled."))
            input("  Press Enter...")
        elif choice == 2:
            # Enable channel
            if not channels:
                print(c(C.YELLOW, "  No channels to enable."))
                input("  Press Enter...")
                continue
            print(c(C.CYAN, "  Enter channel number to enable:"))
            try:
                ch_num = int(input(c(C.BOLD, "  Channel: ")).strip())
            except (EOFError, KeyboardInterrupt, ValueError):
                print()
                input("  Press Enter...")
                continue
            if ch_num < 1 or ch_num > len(channels):
                print(c(C.YELLOW, "  Invalid channel number."))
                input("  Press Enter...")
                continue
            ch = channels[ch_num - 1]
            print(c(C.CYAN, f"  Enabling channel {ch['name']}..."))
            _enable_channel(ch["id"])
            print(c(C.GREEN, "  Channel enabled."))
            input("  Press Enter...")


def _list_channels_with_sessions() -> list[dict]:
    """List channels with sessions."""
    try:
        from bot.store import SessionStore
        from pathlib import Path
        import os

        # Determine session store path based on session mode
        mode = _get_session_mode()
        data_dir = Path(os.path.join(os.path.dirname(__file__), "..", "data"))

        if not data_dir.exists():
            return []

        store = SessionStore(data_dir, mode=mode)
        # Query all unique session keys with message counts
        rows = store._conn.execute(
            "SELECT s.session_key, COUNT(s.id) as session_count, SUM(s.message_count) as total_messages "
            "FROM sessions s GROUP BY s.session_key ORDER BY s.last_activity_at DESC LIMIT 20"
        ).fetchall()

        channels = []
        seen = set()
        for row in rows:
            session_key = row["session_key"]
            # Parse channel name from session key (format: ch-<channel_id> or ch-<channel_id>-<user_id>)
            if session_key.startswith("ch-"):
                parts = session_key.split("-")
                if len(parts) >= 2:
                    channel_id = parts[1]
                    if channel_id not in seen:
                        seen.add(channel_id)
                        channels.append({
                            "id": channel_id,
                            "name": f"#{channel_id}",
                            "sessions": row["session_count"],
                            "messages": row["total_messages"],
                        })
        store.close()

        # Try to resolve channel names via Discord API
        channels = _resolve_channel_names(channels)
        return channels
    except Exception as e:
        print(f"  [error] Failed to list channels: {e}")
        return []


def _get_ignored_channels() -> set[int]:
    """Get set of ignored channel IDs from .env."""
    ignored = set()
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("IGNORED_CHANNELS="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        for ch_id in value.split(","):
                            ch_id = ch_id.strip()
                            if ch_id.isdigit():
                                ignored.add(int(ch_id))
    except FileNotFoundError:
        pass
    return ignored


def _resolve_channel_names(channels: list[dict]) -> list[dict]:
    """Try to resolve channel IDs to names via Discord API."""
    try:
        import httpx
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        token = ""
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("DISCORD_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        if not token:
            return channels

        for ch in channels:
            try:
                r = httpx.get(
                    f"https://discord.com/api/v10/channels/{ch['id']}",
                    headers={"Authorization": f"Bot {token}"},
                    timeout=5,
                )
                if r.status_code == 200:
                    data = r.json()
                    ch["name"] = f"#{data.get('name', ch['id'])}"
            except Exception:
                pass
        return channels
    except Exception:
        return channels


def _disable_channel(channel_id: int) -> None:
    """Disable a channel by adding it to IGNORED_CHANNELS."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    lines = []
    found = False
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("IGNORED_CHANNELS="):
                    # Add channel to ignored list
                    current = line.split("=", 1)[1].strip()
                    if current:
                        new_value = f"{current},{channel_id}"
                    else:
                        new_value = str(channel_id)
                    lines.append(f"IGNORED_CHANNELS={new_value}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"IGNORED_CHANNELS={channel_id}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


def _enable_channel(channel_id: int) -> None:
    """Enable a channel by removing it from IGNORED_CHANNELS."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    lines = []
    try:
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("IGNORED_CHANNELS="):
                    # Remove channel from ignored list
                    current = line.split("=", 1)[1].strip()
                    if current:
                        ignored = [x for x in current.split(",") if x.strip() != str(channel_id)]
                        new_value = ",".join(ignored)
                        if new_value:
                            lines.append(f"IGNORED_CHANNELS={new_value}\n")
                    # If list is empty, don't write the line
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    with open(env_path, "w") as f:
        f.writelines(lines)


# --- Sessions submenu ---


def _sessions_menu() -> None:
    """Sessions submenu."""
    while True:
        clear()
        print_banner()
        print(c(C.BOLD, "  Sessions:"))
        print()

        items = [
            ("1", "List recent sessions"),
            ("2", "Search sessions"),
            ("0", "Back"),
        ]
        for num, label in items:
            print(f"    [{num}] {label}")
        print()

        choice = prompt_choice(2)
        if choice is None or choice == 0:
            return

        if choice == 1:
            # List recent sessions
            print()
            print(c(C.CYAN, "  Recent sessions:"))
            print()
            subprocess.run(["discordbot", "sessions"])
            print()
            input("  Press Enter...")
        elif choice == 2:
            # Search sessions
            print()
            print(c(C.CYAN, "  Search sessions:"))
            try:
                query = input(c(C.BOLD, "  Query: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                input("  Press Enter...")
                continue
            if not query:
                print(c(C.YELLOW, "  Search cancelled."))
                input("  Press Enter...")
                continue
            print()
            subprocess.run(["discordbot", "sessions", "search", query])
            print()
            input("  Press Enter...")


# --- Live log view ---


def _live_log_view() -> None:
    """Live log view with pause/resume."""
    clear()
    print_banner()
    print(c(C.BOLD, "  Live logs (type 'quit' to exit):"))
    print()

    try:
        for line in _stream_logs():
            print(line)
            # Check for user input
            try:
                import select
                if select.select([sys.stdin], [], [], 0.01)[0]:
                    cmd = sys.stdin.readline().strip().lower()
                    if cmd in ("quit", "exit", "q"):
                        break
            except (ImportError, Exception):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print(c(C.GREEN, "  Logs stopped."))


# --- Main menu loop ---


def run() -> None:
    """Run the interactive menu."""
    while True:
        clear()

        # Get status
        status = _get_status()
        if not status.get("ok"):
            # Bot not running, show fallback status
            status = {
                "ok": True,
                "data": {
                    "running": _is_running(),
                    "connected": False,
                    "provider": "unknown",
                    "bot_name": "",
                    "bot_id": "",
                },
            }

        data = status.get("data", {})
        items = print_standby_menu(data)

        choice = prompt_choice(7)
        if choice is None or choice == 0:
            print(c(C.CYAN, "  Bye!"))
            return

        if choice == 1:
            # Settings
            _settings_menu()
        elif choice == 2:
            # Sessions
            _sessions_menu()
        elif choice == 3:
            # Start/Stop bot
            if data.get("running"):
                print(c(C.CYAN, "  Stopping bot..."))
                _service("stop")
                print(c(C.GREEN, "  Bot stopped."))
            else:
                print(c(C.CYAN, "  Starting bot..."))
                _service("start")
                print(c(C.GREEN, "  Bot started."))
                # Wait for bot to connect
                time.sleep(2)  # Give the bot a moment to start
                status = _get_status()
                if status.get("ok"):
                    data = status.get("data", {})
                    if data.get("running") and not data.get("connected"):
                        print(c(C.CYAN, "  Waiting for bot to connect..."))
                        for i in range(30):  # Timeout after 30 seconds
                            time.sleep(1)
                            status = _get_status()
                            if not status.get("ok"):
                                break
                            data = status.get("data", {})
                            if data.get("connected") and data.get("bot_name"):
                                print(c(C.GREEN, "  Bot connected!"))
                                break
                            if not data.get("running"):
                                print(c(C.YELLOW, "  Bot stopped while waiting."))
                                break
                        else:
                            print(c(C.YELLOW, "  Timeout waiting for bot to connect."))
            input("  Press Enter...")
        elif choice == 4:
            # Restart bot
            print(c(C.CYAN, "  Restarting bot..."))
            _service("restart")
            print(c(C.GREEN, "  Bot restarted."))
            input("  Press Enter...")
        elif choice == 5:
            # Status
            print()
            print(c(C.CYAN, "  Status:"))
            print()
            subprocess.run(["discordbot", "status"])
            print()
            input("  Press Enter...")
        elif choice == 6:
            # Logs
            _live_log_view()
        elif choice == 7:
            # Health check
            print()
            print(c(C.CYAN, "  Health check:"))
            print()
            subprocess.run(["discordbot", "health"])
            print()
            input("  Press Enter...")
