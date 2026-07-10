# Deployment — Notion-saved-message Bot

Production runs as its **own Docker Compose project** in `~/notion-saved-message/`
on the Hetzner VPS, in **webhook mode**. CI builds the image, pushes it to GHCR,
and deploys over SSH. The shared **Caddy** (running in the separate tracker-stack
compose project) terminates TLS and reverse-proxies the public subdomain to this
bot's container over the shared external Docker network `app_app-network` — the
same pattern as `namaz-bot`.

- Image: `ghcr.io/axmedovxasanboy/notion_saved_message`
- Public URL: `https://notion-saved-message.xasanboy.dev` → `reverse_proxy notion-saved-message:8080`
- Entry: `python -m app.main` with `RUN_MODE=webhook`

## Architecture

```
Telegram ──HTTPS──> Caddy (tracker stack) ──app_app-network──> notion-saved-message:8080
                                                                 └─ SQLite /app/data (host ./data)
```

The bot publishes **no host ports**; only Caddy reaches it, by container name on
the shared `app_app-network`.

## Webhook & security

- Telegram POSTs to `https://notion-saved-message.xasanboy.dev/webhook/<WEBHOOK_SECRET>`.
- The secret is in the URL path **and** validated on every request via the
  `X-Telegram-Bot-Api-Secret-Token` header (aiogram `secret_token`).
- `RUN_MODE=webhook` **fails closed**: the bot refuses to start without
  `WEBHOOK_SECRET` (an empty secret would let aiogram accept any POST).
- `GET /health` → `200 ok` (unauthenticated) for Caddy / Docker HEALTHCHECK.
- On startup the bot calls `set_webhook(..., drop_pending_updates=True)`; on
  shutdown `delete_webhook()`.

## Config: how the container gets its env

`docker-compose.yml` pins only the container-critical settings (`RUN_MODE`,
`WEBHOOK_HOST`, `PORT`). **Everything else** (`BOT_TOKEN`, `CHAT_ID`, `NOTION_*`,
`OPENAI_API_KEY`, `CLAUDE_API_KEY`, message templates, …) is read by the app's
`load_dotenv()` from `~/notion-saved-message/.env`, which is **bind-mounted
read-only** into the container at `/app/.env`.

> Why mounted and not `env_file:` — two values (`PROMPT_POST_OVERVIEW`,
> `MSG_WELCOME`) are **multi-line**. Docker Compose's `env_file` parser mangles
> multi-line values; python-dotenv parses them correctly. Mounting the file
> preserves the exact behaviour the bot already has locally and on the old host.

## CI/CD pipeline — `.github/workflows/deploy.yml`

On push to `main` (or manual `workflow_dispatch`):

1. **build** — builds `Dockerfile`, pushes `:latest` and `:<short-sha>` to GHCR.
   GHCR auth uses the built-in `GITHUB_TOKEN` (no PAT needed).
2. **deploy** — SSHes to the server and runs, in `~/notion-saved-message`:
   `docker compose pull notion-saved-message && docker compose up -d notion-saved-message && docker image prune -f`.

### Required GitHub repository secrets

| Secret | Purpose |
|---|---|
| `SERVER_HOST` | VPS host/IP for the SSH deploy |
| `SERVER_USER` | SSH user that owns `~/notion-saved-message` |
| `SERVER_SSH_KEY` | Private SSH key for that user |

No GHCR PAT is required for the build (it uses `GITHUB_TOKEN`). For the **server**
to pull, its existing `docker login ghcr.io` (already set up for namaz-bot) must
have **read access to this new package** — if the pull 403s, either make the
`notion_saved_message` package public in GHCR, or link it / log in with a PAT
that has `read:packages`.

## One-time server setup (`~/notion-saved-message/`)

Prerequisites (already true on this box from the namaz-bot / tracker setup):
- Docker + Compose v2; the deploy user is logged into GHCR.
- External network `app_app-network` exists: `docker network ls | grep app_app-network`.
- DNS `notion-saved-message.xasanboy.dev` → the VPS (**done**); Caddy serves 80/443.

Steps:
```bash
mkdir -p ~/notion-saved-message/data
cd ~/notion-saved-message
# put docker-compose.yml here (scp from the repo, or curl the raw file from GitHub)

# create the secrets/config file the container mounts read-only at /app/.env.
# Copy your full working .env here (all BOT_TOKEN / CHAT_ID / NOTION_* / AI keys /
# message templates) and ADD one new line:
#   WEBHOOK_SECRET=$(openssl rand -hex 32)
# RUN_MODE / WEBHOOK_HOST / PORT are pinned by compose, so they are optional here.
chmod 600 .env
```

Bootstrap the first run (before CI ever fires):
```bash
cd ~/notion-saved-message
docker compose pull notion-saved-message
docker compose up -d notion-saved-message
```
After that, every push to `main` redeploys automatically.

## Caddy block (add to the tracker stack's Caddyfile)

See `Caddyfile.example`. Add:
```
notion-saved-message.xasanboy.dev {
    reverse_proxy notion-saved-message:8080
}
```
Then reload Caddy: `docker exec <caddy-container> caddy reload --config /etc/caddy/Caddyfile`.
Caddy must be attached to `app_app-network` (it already is for namaz-bot).

## Verify

```bash
docker compose ps                            # notion-saved-message -> "healthy"
docker compose logs -f notion-saved-message  # look for "Webhook set to https://notion-saved-message.xasanboy.dev/webhook/..."
curl -s https://notion-saved-message.xasanboy.dev/health                    # -> ok
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"            # url set, pending=0, no last_error
```
Then send `/start` in Telegram.

## Data & persistence

SQLite lives at `/app/data/ai_agent_bot.db` in the container, bind-mounted to
`~/notion-saved-message/data/`. It survives restarts, rebuilds, and image
updates, and is never baked into the image (`.dockerignore` excludes `data/` and
`*.db`). Back up by copying `~/notion-saved-message/data/ai_agent_bot.db`.

## Rollback

Every build is tagged `:latest` **and** `:<short-sha>`:
```bash
cd ~/notion-saved-message
sed -i 's#notion_saved_message:.*#notion_saved_message:<short-sha>#' docker-compose.yml
docker compose pull notion-saved-message && docker compose up -d notion-saved-message
```
Restore `:latest` the same way once a fix is deployed.

## Local development

No Docker needed: keep `RUN_MODE=polling` (the default) in your local `.env` and
run `python -m app.main`. Polling first clears any webhook so the two modes never
fight over updates.
