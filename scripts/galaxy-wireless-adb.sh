#!/system/bin/sh
# Magisk late_start → /data/adb/service.d/99-wireless-adb.sh
# Keep Galaxy Wi-Fi alive and ADB on TCP 5555. If wlan0 loses its IP,
# bounce Wi-Fi (with a cooldown) then bring adbd back.

PORT=5555
LOG=/data/local/tmp/wireless-adb.log
LOG_MAX=80000
TOGGLE_COOLDOWN=180
FAILS_BEFORE_TOGGLE=8
WAKE_COOLDOWN=600

log() {
    echo "$(date '+%F %T') $*" >> "$LOG"
    if [ -f "$LOG" ]; then
        sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
        if [ "$sz" -gt "$LOG_MAX" ]; then
            tail -c 20000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
        fi
    fi
}

wifi_has_ip() {
    ip -f inet addr show wlan0 2>/dev/null | grep -q "inet "
}

wlan_up() {
    ip link show wlan0 2>/dev/null | grep -q "state UP"
}

screen_awake() {
    dumpsys power 2>/dev/null | grep -q "mWakefulness=Awake"
}

wake_display() {
    # herolte drops STA when the panel is off; a display wake brings the radio back.
    log "waking display (wifi down, screen=$(screen_awake && echo on || echo off))"
    input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || input keyevent 224 >/dev/null 2>&1 || true
    svc power stayon usb >/dev/null 2>&1 || true
    sleep 2
}

adb_listening() {
    ss -lnt 2>/dev/null | grep -q ":${PORT} " && return 0
    netstat -lnt 2>/dev/null | grep -q ":${PORT} " && return 0
    return 1
}

usb_tethering() {
    ip link show usb0 2>/dev/null | grep -q "state UP" && return 0
    ip link show rndis0 2>/dev/null | grep -q "state UP" && return 0
    getprop sys.usb.config 2>/dev/null | grep -q rndis && return 0
    return 1
}

prefer_mtp_adb() {
    # A Mac cable often auto-picks Samsung RNDIS. That gadget has no ADB
    # and Lineage herolte then drops the Wi-Fi station.
    if ! usb_tethering; then
        return 0
    fi
    log "usb tethering — switching gadget to mtp,adb"
    settings put global tether_offload_disabled 1 >/dev/null 2>&1
    svc usb setFunctions mtp,adb >/dev/null 2>&1 || true
    setprop persist.sys.usb.config mtp,adb
    setprop sys.usb.config mtp,adb
}

apply_keepalive() {
    prefer_mtp_adb
    settings put global wifi_on 1 >/dev/null 2>&1
    settings put global wifi_sleep_policy 2 >/dev/null 2>&1
    settings put global wifi_wakeup_enabled 1 >/dev/null 2>&1
    settings put global wifi_scan_always_enabled 1 >/dev/null 2>&1
    settings put global wifi_suspend_optimizations_enabled 0 >/dev/null 2>&1
    settings put global airplane_mode_on 0 >/dev/null 2>&1
    settings put global low_power 0 >/dev/null 2>&1
    # Keep the radio up while USB/AC is plugged (this S7 sits on charge).
    settings put global stay_on_while_plugged_in 7 >/dev/null 2>&1
    settings put global adb_enabled 1 >/dev/null 2>&1
    settings put global adb_wifi_enabled 1 >/dev/null 2>&1
    dumpsys deviceidle disable >/dev/null 2>&1 || true
    dumpsys deviceidle unforce >/dev/null 2>&1 || true
    cmd wifi set-wifi-enabled enabled >/dev/null 2>&1 || svc wifi enable >/dev/null 2>&1 || true
    # 802.11 power-save is what drops this Lineage herolte link with the screen off.
    iw wlan0 set power_save off >/dev/null 2>&1 || true
    ip link set wlan0 up >/dev/null 2>&1 || true
}

enable_wireless_adb() {
    apply_keepalive
    if command -v resetprop >/dev/null 2>&1; then
        resetprop persist.service.adb.tcp.port "$PORT"
        resetprop service.adb.tcp.port "$PORT"
    else
        setprop persist.service.adb.tcp.port "$PORT"
        setprop service.adb.tcp.port "$PORT"
    fi
    if adb_listening; then
        return 0
    fi
    log "starting adbd on :$PORT"
    stop adbd
    start adbd
    sleep 2
    if adb_listening; then
        log "adbd listening on :$PORT"
        return 0
    fi
    setprop ctl.restart adbd
    sleep 2
    if adb_listening; then
        log "adbd listening on :$PORT after ctl.restart"
        return 0
    fi
    log "failed to bind :$PORT"
    return 1
}

recover_wifi() {
    wake_display
    log "wifi dead — airplane cycle"
    settings put global airplane_mode_on 1 >/dev/null 2>&1
    am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true >/dev/null 2>&1 || true
    sleep 4
    settings put global airplane_mode_on 0 >/dev/null 2>&1
    am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false >/dev/null 2>&1 || true
    cmd wifi set-wifi-enabled enabled >/dev/null 2>&1 || svc wifi enable >/dev/null 2>&1 || true
    sleep 8
    ip link set wlan0 up >/dev/null 2>&1 || true
    iw wlan0 set power_save off >/dev/null 2>&1 || true
    if wifi_has_ip; then
        log "wifi recovered $(ip -f inet addr show wlan0 | awk '/inet /{print $2}')"
        return 0
    fi
    log "wifi still down after airplane cycle"
    return 1
}

sleep 15
log "watcher started"
apply_keepalive
misses=0
last_toggle=0
last_wake=0

while true; do
    if wifi_has_ip; then
        misses=0
        if ! adb_listening; then
            enable_wireless_adb
        fi
    else
        misses=$((misses + 1))
        now=$(date +%s)
        log "wifi missing (miss=$misses wlan_up=$(wlan_up && echo 1 || echo 0))"
        apply_keepalive
        wake_delta=$((now - last_wake))
        if [ "$last_wake" -eq 0 ] || [ "$wake_delta" -ge "$WAKE_COOLDOWN" ]; then
            wake_display
            last_wake=$now
        fi
        if [ "$misses" -ge "$FAILS_BEFORE_TOGGLE" ]; then
            delta=$((now - last_toggle))
            if [ "$delta" -ge "$TOGGLE_COOLDOWN" ]; then
                recover_wifi
                last_toggle=$now
                misses=0
            fi
        fi
        if wifi_has_ip; then
            enable_wireless_adb
        fi
    fi
    sleep 12
done
