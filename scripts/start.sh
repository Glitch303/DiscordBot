#!/usr/bin/env bash
# Run the bot in the foreground (Ctrl-C to stop).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m bot.main "$@"
