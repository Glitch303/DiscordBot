#!/usr/bin/env bash
# One-time setup: create venv, install deps, seed .env.
set -euo pipefail
cd "$(dirname "$0")/.."

uv venv .venv
source .venv/bin/activate
uv pip install -r pyproject.toml

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env — edit it with your DISCORD_BOT_TOKEN, then run: bash scripts/start.sh"
fi

echo "Setup complete. Activate with: source .venv/bin/activate"
