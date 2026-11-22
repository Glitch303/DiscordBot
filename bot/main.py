"""Entry point: python -m bot.main"""
from __future__ import annotations

import logging
import sys

from .bot import BotClient
from .config import load
from .control_server import start_control_server, stop_control_server, SOCKET_PATH


def main() -> int:
    cfg = load()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    client = BotClient(cfg)

    # Start control server
    def status_callback(args):
        return {
            "connected": client.is_ready(),
            "running": True,
            "bot_name": client.user.name if client.user else None,
            "bot_id": str(client.user.id) if client.user else None,
            "provider": cfg.model_base_url,
            "model": cfg.model_name,
        }

    callbacks = {
        "status": status_callback,
    }

    control_server = start_control_server(SOCKET_PATH, callbacks)

    try:
        client.run(cfg.discord_token, log_handler=None)
    except KeyboardInterrupt:
        print("bye")
    finally:
        stop_control_server(control_server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
