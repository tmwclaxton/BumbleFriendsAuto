#!/usr/bin/env bash
# Host watchdog: keep reconnecting Archie's Galaxy on a fixed wireless ADB port.
set -u
ADB="${ADB:-$HOME/platform-tools/adb}"
[[ -x "$ADB" ]] || ADB="$(command -v adb || true)"
ENV_FILE="${LGS_ENV:-$HOME/lgspipeline/.env}"
STATE="${LGS_ADB_STATE:-$HOME/lgspipeline/data/archie_adb.txt}"
DEFAULT_EP="192.168.0.168:5555"
LOG="${LGS_ADB_LOG:-$HOME/lgspipeline/data/galaxy-adb-watch.log}"

endpoint() {
    local ep=""
    if [[ -f "$ENV_FILE" ]]; then
        ep=$(awk -F= '$1=="ARCHIE_ADB"{gsub(/\r/,"",$2); gsub(/^["'\'']|["'\'']$/,"",$2); print $2; exit}' "$ENV_FILE")
    fi
    if [[ -z "$ep" && -f "$STATE" ]]; then
        ep=$(tr -d '[:space:]' < "$STATE")
    fi
    printf '%s' "${ep:-$DEFAULT_EP}"
}

online() {
    local ep=$1
    "$ADB" devices 2>/dev/null | awk -v s="$ep" 'NR>1 && $1==s && $2=="device" {found=1} END{exit !found}'
}

mkdir -p "$(dirname "$LOG")"
EP=$(endpoint)
echo "$(date '+%F %T') watch $EP" >> "$LOG"
if [[ -z "$ADB" ]]; then
    echo "$(date '+%F %T') adb missing" >> "$LOG"
    exit 1
fi
"$ADB" start-server >/dev/null 2>&1 || true
if online "$EP"; then
    exit 0
fi
out=$("$ADB" connect "$EP" 2>&1 || true)
echo "$(date '+%F %T') connect $EP — $out" >> "$LOG"
if online "$EP"; then
    printf '%s\n' "$EP" > "$STATE"
    exit 0
fi
exit 1
