#!/bin/sh
set -eu

mkdir -p /app/data

# Attach Archie's Galaxy over wireless debugging (USB stays charge-only).
python -m src.adb_wireless || true

# Start scheduled recapture in the background (Europe/London via TZ).
if [ "${ENABLE_CRON:-1}" = "1" ]; then
  echo "starting supercronic for inbox recapture"
  supercronic /app/docker/crontab &
fi

exec python -m src.dashboard --foreground --host 0.0.0.0 --port "${PORT:-8765}"
