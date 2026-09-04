"""Wake / unlock / sleep the attached phone for unattended jobs."""

from __future__ import annotations

import logging
import os
import subprocess
import time

from src.device import connect, wait_idle

log = logging.getLogger(__name__)

def _pin_from_env() -> str:
    return (os.environ.get("PHONE_UNLOCK_PIN") or os.environ.get("PIXEL_UNLOCK_PIN") or "").strip()


def _serial_of(device) -> str:
    return str(
        getattr(device, "serial", None)
        or os.environ.get("SERIAL")
        or os.environ.get("PIXEL_SERIAL")
        or ""
    ).strip()


def _adb_shell(device, *args: str, timeout: float = 8) -> str:
    """Talk to the phone with the adb CLI. uiautomator2 shell misses keyguard input."""
    cmd = ["adb"]
    serial = _serial_of(device)
    if serial:
        cmd.extend(["-s", serial])
    cmd.append("shell")
    cmd.extend(args)
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout)
        return out or ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("adb shell %s failed: %s", args[:2], exc)
        ret = device.shell(" ".join(args))
        if isinstance(ret, str):
            return ret
        return str(getattr(ret, "output", None) or ret or "")


def _shell_output(device, command: str) -> str:
    parts = command.split()
    if parts:
        return _adb_shell(device, *parts)
    ret = device.shell(command)
    if isinstance(ret, str):
        return ret
    output = getattr(ret, "output", None)
    if output is not None:
        return str(output)
    return str(ret or "")


def lock_state_from_dumpsys(
    *,
    nfc: str = "",
    power: str = "",
    window: str = "",
    screen_on: bool | None = None,
) -> str:
    """Return 'off', 'locked', or 'unlocked' from dumpsys snippets."""
    for line in nfc.splitlines():
        if "mScreenState=" not in line:
            continue
        val = line.split("=", 1)[-1].strip().upper()
        if "UNLOCKED" in val and not val.startswith("OFF"):
            return "unlocked"
        if val in {"ON_LOCKED", "LOCKED"} or (val.endswith("LOCKED") and "UNLOCKED" not in val):
            return "locked"
        if val.startswith("OFF"):
            return "off"
    if screen_on is False:
        return "off"
    if "mWakefulness=Asleep" in power or "mWakefulness=Dozing" in power:
        return "off"
    window_l = window.lower()
    if "mdreaminglockscreen=true" in window_l or "mkeyguardshowing=true" in window_l:
        return "locked"
    if "isstatusbarkeyguard=true" in window_l:
        return "locked"
    if screen_on is True:
        return "unlocked"
    if "mWakefulness=Awake" in power:
        return "unlocked"
    return "locked"


def screen_lock_state(device) -> str:
    info = device.info or {}
    screen_on = info.get("screenOn")
    if not isinstance(screen_on, bool):
        screen_on = None
    return lock_state_from_dumpsys(
        nfc=_shell_output(device, "dumpsys nfc"),
        power=_shell_output(device, "dumpsys power"),
        window=_shell_output(device, "dumpsys window"),
        screen_on=screen_on,
    )


def hierarchy_looks_locked(xml: str) -> bool:
    """True if a uiautomator dump is the Pixel lock / PIN pad, not Bumble."""
    blob = xml or ""
    return (
        "com.android.systemui:id/keyguard_pin_view" in blob
        or "com.android.systemui:id/pin_container" in blob
        or "com.android.systemui:id/keyguard_root_view" in blob
        or ">Enter PIN<" in blob
        or 'content-desc="Device locked"' in blob
        or 'content-desc="PIN area"' in blob
    )


def _hierarchy_xml(device) -> str:
    """Secure lockscreen nodes often hide from uiautomator2 selectors; dump via adb."""
    device.shell("uiautomator dump /sdcard/uidump.xml")
    return _shell_output(device, "cat /sdcard/uidump.xml")


def _pin_pad_ready(device) -> bool:
    xml = _hierarchy_xml(device)
    return "pin_container" in xml or "Enter PIN" in xml or "keyguard_pin_view" in xml


def _enter_pin(device, pin: str) -> None:
    """Type the PIN with keyevents. Pixel keyguard ignores `input text` and u2 clicks."""
    for digit in pin:
        if not digit.isdigit():
            continue
        # KEYCODE_0 is 7, KEYCODE_1 is 8, ...
        _adb_shell(device, "input", "keyevent", str(7 + int(digit)))
        time.sleep(0.12)
    _adb_shell(device, "input", "keyevent", "KEYCODE_ENTER")
    wait_idle(device, 1.2)
    log.info("PIN submitted")


def _swipe_to_pin(device, width: int, height: int) -> None:
    # Start at the lock-icon band (~73%), not the gesture bar (~88%) — that
    # swipe misses the PIN pad and later taps land on the clock.
    x = width // 2
    y0 = int(height * 0.73)
    y1 = int(height * 0.28)
    _adb_shell(device, "input", "swipe", str(x), str(y0), str(x), str(y1), "280")


def wake_and_unlock(device=None, *, serial: str | None = None, pin: str | None = None) -> bool:
    """Wake and unlock. Returns True if the screen is unlocked when we finish."""
    device = device or connect(serial)
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    pin = (pin if pin is not None else _pin_from_env()).strip()

    try:
        _adb_shell(device, "cmd", "statusbar", "collapse")
    except Exception:
        pass

    for attempt in range(2):
        state = screen_lock_state(device)
        if state == "unlocked":
            log.info("screen already unlocked")
            return True

        log.info("wake screen")
        _adb_shell(device, "input", "keyevent", "KEYCODE_WAKEUP")
        wait_idle(device, 1.0)
        if screen_lock_state(device) == "unlocked":
            log.info("woke already-unlocked screen")
            return True

        log.info("unlock lockscreen (attempt %d)", attempt + 1)
        _swipe_to_pin(device, width, height)
        wait_idle(device, 1.4)

        if screen_lock_state(device) == "unlocked":
            log.info("lockscreen dismissed without PIN")
            return True

        if not pin:
            log.info("no PHONE_UNLOCK_PIN set — assuming swipe unlocked")
            return screen_lock_state(device) == "unlocked"

        # Do not wait for a dump of the PIN pad. `uiautomator dump` via
        # uiautomator2 often misses the keyguard, which skipped PIN entry
        # and left jobs looping on a locked phone.
        _enter_pin(device, pin)
        for _ in range(8):
            if screen_lock_state(device) == "unlocked":
                log.info("screen unlocked")
                return True
            time.sleep(0.25)
        log.warning("still locked after PIN")

    log.error("failed to unlock phone")
    return False


def sleep_screen(device=None, *, serial: str | None = None) -> None:
    device = device or connect(serial)
    log.info("sleep screen")
    _adb_shell(device, "input", "keyevent", "KEYCODE_SLEEP")
    wait_idle(device, 0.3)
