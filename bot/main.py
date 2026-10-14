"""Entry point: python -m bot.main"""
from __future__ import annotations

import logging
import sys

from .bot import BotClient
from .config import load


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
    try:
        client.run(cfg.discord_token, log_handler=None)
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
