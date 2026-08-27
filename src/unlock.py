"""Wake / unlock / sleep the attached phone for unattended jobs."""

from __future__ import annotations

import logging
import os
import time

from src.device import connect, wait_idle
from src.gestures import tap

log = logging.getLogger(__name__)


def _pin_from_env() -> str:
    return (os.environ.get("PHONE_UNLOCK_PIN") or os.environ.get("PIXEL_UNLOCK_PIN") or "").strip()


def _shell_output(device, command: str) -> str:
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


def _enter_pin(device, pin: str, width: int, height: int) -> None:
    # Prefer tapping visible digit nodes; Pixel lockscreens often ignore `input text`.
    for digit in pin:
        node = device(text=digit)
        if node.exists(timeout=0.6):
            node.click()
        else:
            mapping = {
                "1": (0, 0),
                "2": (1, 0),
                "3": (2, 0),
                "4": (0, 1),
                "5": (1, 1),
                "6": (2, 1),
                "7": (0, 2),
                "8": (1, 2),
                "9": (2, 2),
                "0": (1, 3),
            }
            col, row = mapping.get(digit, (1, 0))
            kx = int(width * (0.22 + col * 0.28))
            ky = int(height * (0.48 + row * 0.10))
            tap(device, kx, ky)
        time.sleep(0.15)

    enter = device(description="Enter")
    if enter.exists(timeout=0.8):
        enter.click()
    else:
        device.shell("input keyevent KEYCODE_ENTER")
    wait_idle(device, 1.0)
    log.info("unlock attempted")


def wake_and_unlock(device=None, *, serial: str | None = None, pin: str | None = None) -> None:
    """Wake the screen and unlock. No-op if already unlocked (so we don't swipe into Bumble)."""
    device = device or connect(serial)
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    pin = (pin if pin is not None else _pin_from_env()).strip()

    state = screen_lock_state(device)
    if state == "unlocked":
        log.info("screen already unlocked")
        return

    if state == "off":
        log.info("wake screen")
        device.shell("input keyevent KEYCODE_WAKEUP")
        wait_idle(device, 0.5)
        state = screen_lock_state(device)
        if state == "unlocked":
            log.info("woke already-unlocked screen")
            return

    log.info("unlock lockscreen")
    x = width // 2
    device.shell(f"input swipe {x} {int(height * 0.88)} {x} {int(height * 0.25)} 250")
    wait_idle(device, 1.0)

    if not pin:
        log.info("no PHONE_UNLOCK_PIN set — assuming swipe unlocked")
        return

    if screen_lock_state(device) == "unlocked":
        log.info("lockscreen dismissed without PIN")
        return

    _enter_pin(device, pin, width, height)


def sleep_screen(device=None, *, serial: str | None = None) -> None:
    device = device or connect(serial)
    log.info("sleep screen")
    device.shell("input keyevent KEYCODE_SLEEP")
    wait_idle(device, 0.3)
