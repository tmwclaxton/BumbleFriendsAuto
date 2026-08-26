# Bumble Friends ADB auto-swiper + LGS inbox pipeline

Mac-side (or server) Python that drives an Android phone over ADB for Bumble For Friends: swipe, message New friends, sync chats into SQLite, and a password-protected inbox UI.

**Warning:** Automating Bumble violates their terms of service and can get your account banned. Use only on a phone and account you own. This project does **not** solve verification, paywalls, or anti-bot checks — it stops when those appear.

## Requirements

- macOS/Linux with Python 3.10+
- Android phone with **USB debugging** enabled
- [`adb`](https://developer.android.com/tools/adb): `brew install android-platform-tools`
- Bumble **Friends / BFF** (`com.bumblebff.app`)

Layout math is **screen-size relative** (Honor ~1280×2800 and Pixel ~1080×2400).

## Setup

```bash
cd BumbleFriendsAuto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # optional local overrides
```

```bash
adb devices
```

## Local workflow

```bash
python -m src.dump_ui
python -m src.swiper
python -m src.messenger --dry-run
python -m src.sync_chats --full --recapture
python -m src.dashboard --foreground   # http://127.0.0.1:8765
```

Send/Refresh go on a phone queue. Status: `needs_reply` / `waiting` / `expired` / `unknown`.

## Production (admin.grantgunner.org)

Docker image talks to the **host ADB server** and the USB **Pixel 7** (`29081FDH200GZ8`).

- Inbox: https://lgspipeline.grantgunner.org (HTTP basic auth)
- MCP: https://lgspipeline.grantgunner.org/mcp (same auth)
- Recapture cron (Europe/London): 10:00, 13:00, 17:00, 23:00

On the server:

```bash
mkdir -p ~/lgspipeline
cd ~/lgspipeline
# Place docker-compose.yml, .env, config.yaml, cloudflared/ from deploy
docker compose pull && docker compose up -d
```

`.env` (not in git) must set `SERIAL`, `DASHBOARD_BASIC_USER`, `DASHBOARD_BASIC_PASSWORD`, `PHONE_UNLOCK_PIN`.

Host ADB must show the Pixel:

```bash
export PATH="$HOME/.local/opt/platform-tools:$PATH"
adb devices -l
# keep adb server up: adb start-server
```

### MCP (agents)

Add a Cursor MCP server (Streamable HTTP):

```json
{
  "mcpServers": {
    "lgspipeline": {
      "url": "https://lgspipeline.grantgunner.org/mcp",
      "headers": {
        "Authorization": "Basic <base64 of admin:password>"
      }
    }
  }
}
```

Tools: `list_inbox_people`, `list_needs_reply_people`, `get_thread`, `list_jobs`, `start_refresh_chat`, `start_send_reply`, `start_recapture_inbox`, `get_job`, `wait_for_job`.

Phone work is **async**: `start_*` returns `job_id` immediately; poll `get_job` until `done`/`error` (full recapture can take ~40 minutes). Do not treat `queued`/`running` as success.

### Deploy pipeline

Push to `master` builds `ghcr.io/tmwclaxton/bumblefriendsauto` and SSHs to the host to `docker compose pull && up -d`. Required GitHub secrets: `LGPIPELINE_SSH_KEY`, `GHCR_PULL_TOKEN`, `LGPIPELINE_BASIC_AUTH` (`user:pass`), `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` (Cloudflare Access SSH).

## Project layout

```
src/config.py       # YAML + env overrides (SERIAL, DB_PATH)
src/device.py       # ADB / uiautomator2
src/sync_chats.py   # Layout-relative inbox sync / recapture
src/phone_queue.py  # Serialized phone jobs
src/dashboard.py    # Inbox UI entry + basic auth
src/server.py       # Combined ASGI (UI + /mcp)
src/mcp_server.py   # MCP tools (start + poll)
src/unlock.py       # Wake / PIN / sleep for cron
src/jobs/           # Cron entrypoints
docker-compose.yml
Dockerfile
```

## Troubleshooting

- **`unauthorized` in `adb devices`**: unlock phone and re-accept the RSA prompt.
- **Wrong taps on a new phone**: dumps in `dumps/`; row/search targeting uses resource-ids and height fractions.
- **Container cannot see the phone**: `network_mode: host` + host `adb start-server`; check `ADB_SERVER_SOCKET=tcp:127.0.0.1:5037`.
