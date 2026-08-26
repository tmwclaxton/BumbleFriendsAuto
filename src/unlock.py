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


def wake_and_unlock(device=None, *, serial: str | None = None, pin: str | None = None) -> None:
    """Wake the screen, swipe up the lock sheet, enter the PIN if set."""
    device = device or connect(serial)
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    pin = (pin if pin is not None else _pin_from_env()).strip()

    log.info("wake screen")
    device.shell("input keyevent KEYCODE_WAKEUP")
    wait_idle(device, 0.5)
    # Swipe up from lower third to dismiss lock / go to PIN pad.
    x = width // 2
    device.shell(f"input swipe {x} {int(height * 0.88)} {x} {int(height * 0.25)} 250")
    wait_idle(device, 1.0)

    if not pin:
        log.info("no PHONE_UNLOCK_PIN set — assuming unlocked")
        return

    # Prefer keyevents when the PIN pad accepts text; otherwise tap digits.
    # Pixel lockscreens often ignore `input text`, so tap via uiautomator when possible.
    for digit in pin:
        node = device(text=digit)
        if node.exists(timeout=0.6):
            node.click()
        else:
            # Fallback keypad grid for a typical 3x4 PIN pad in the lower half.
            # Columns 0-2, rows 0-3 for 1-9 / blank 0 blank.
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

    # Enter / confirm
    enter = device(description="Enter")
    if enter.exists(timeout=0.8):
        enter.click()
    else:
        device.shell("input keyevent KEYCODE_ENTER")
    wait_idle(device, 1.0)
    log.info("unlock attempted")


def sleep_screen(device=None, *, serial: str | None = None) -> None:
    device = device or connect(serial)
    log.info("sleep screen")
    device.shell("input keyevent KEYCODE_SLEEP")
    wait_idle(device, 0.3)
