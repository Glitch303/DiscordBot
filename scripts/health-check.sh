#!/usr/bin/env bash
# One-shot status report: service, model endpoint, SearXNG, Discord reachability.
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
set -a; source .env 2>/dev/null; set +a

echo "== discordbot health check =="
if systemctl --user is-active discordbot.service >/dev/null 2>&1; then
    echo "[ok] systemd service: running"
else
    echo "[..] systemd service: not running (use scripts/install-service.sh, or run scripts/start.sh)"
fi

echo -n "[..] model endpoint: "
curl -sf --max-time 5 "$MODEL_BASE_URL/models" >/dev/null 2>&1 && echo "reachable ($MODEL_NAME)" || echo "UNREACHABLE ($MODEL_BASE_URL)"

echo -n "[..] searxng: "
curl -sf --max-time 5 "$SEARXNG_URL/search?q=health&format=json" >/dev/null 2>&1 && echo "reachable" || echo "UNREACHABLE ($SEARXNG_URL)"

if [ -n "${DISCORD_BOT_TOKEN:-}" ]; then
    echo -n "[..] discord api (bot token): "
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -H "Authorization: Bot $DISCORD_BOT_TOKEN" https://discord.com/api/v10/users/@me)
    [ "$CODE" = "200" ] && echo "valid" || echo "HTTP $CODE (check token)"
else
    echo "[..] discord api: no DISCORD_BOT_TOKEN set"
fi
echo "== done =="
