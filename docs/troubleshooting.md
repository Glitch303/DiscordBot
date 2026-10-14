# Troubleshooting

## Bot online but ignores me
- Your user ID isn't in `DISCORD_ALLOWED_USERS` (empty = allow all, double-check it's actually empty)
- In a channel you're not @mentioning it (`REQUIRE_MENTION=1`)
- Check logs: `journalctl --user -u discordbot -f`

## Bot never comes online
- Bad `DISCORD_BOT_TOKEN` → log shows HTTP 401/403
- **Message Content Intent** not enabled in the Developer Portal (Bot → Privileged Gateway Intents)
- `scripts/health-check.sh` shows the discord API check — 200 = token valid

## "search failed" / no web results
- SearXNG not running: `curl localhost:8080/search?q=test&format=json`
- SearXNG must have `json` in its enabled `search.formats` (in `settings.yml`)
- Different port? Set `SEARXNG_URL`

## Model errors
- `curl $MODEL_BASE_URL/models` should list your `MODEL_NAME`
- 404 → model name mismatch; 401 → set `MODEL_API_KEY`; connection refused → server down
- "too many tool rounds" in replies → model looping on tools; usually a flaky search backend

## Images not understood
- Model must accept image input (check `input_modalities` includes `image`)
- Huge images (> ~10 MB) can exceed request limits — smaller uploads work better

## Service won't start
- `systemctl --user status discordbot` shows the failure
- venv missing? Run `bash scripts/setup.sh` first
- Port/permissions: the service runs as your user; make sure the model endpoint is reachable from localhost

## Reset everything
```bash
rm -rf data/sessions   # clears all conversation memory
systemctl --user restart discordbot
```
