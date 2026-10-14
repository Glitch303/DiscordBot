#!/usr/bin/env bash
# Install the bot as a systemd user service (survives logout with lingering).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
UNIT="$HOME/.config/systemd/user/discordbot.service"
mkdir -p "$(dirname "$UNIT")"

cat > "$UNIT" <<EOF
[Unit]
Description=DiscordBot - standalone Discord chat bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m bot.main
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable discordbot.service
systemctl --user restart discordbot.service
sleep 2
systemctl --user status discordbot.service --no-pager || true

echo ""
echo "Enabled. To keep it running after logout:  sudo loginctl enable-linger $USER"
echo "Logs:  journalctl --user -u discordbot -f"
