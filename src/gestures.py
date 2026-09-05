"""Human-like swipe gestures with jitter."""

from __future__ import annotations

import logging
import random
import subprocess
import time
from typing import Any

import uiautomator2 as u2

log = logging.getLogger(__name__)


def _jitter(value: float, amount: float) -> float:
    return value + random.uniform(-amount, amount)


def _clamp(value: float, lo: float = 0.02, hi: float = 0.98) -> float:
    return max(lo, min(hi, value))


def _screen_size(device: u2.Device) -> tuple[int, int]:
    info = device.info
    return int(info["displayWidth"]), int(info["displayHeight"])


def _frac_to_px(
    device: u2.Device,
    fx: float,
    fy: float,
    jitter: float,
) -> tuple[int, int]:
    width, height = _screen_size(device)
    x = _clamp(_jitter(fx, jitter)) * width
    y = _clamp(_jitter(fy, jitter)) * height
    return int(x), int(y)


def _adb_swipe(
    device: u2.Device,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int,
) -> None:
    """
    Prefer raw `adb shell input swipe` — Bumble often ignores soft
    uiautomator2 multi-segment swipes on Huawei devices.
    """
    serial = getattr(device, "serial", None)
    cmd = ["adb"]
    if isinstance(serial, str) and serial:
        cmd.extend(["-s", serial])
    cmd.extend(
        [
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(max(duration_ms, 120)),
        ]
    )
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def swipe(
    device: u2.Device,
    swipe_cfg: dict[str, Any],
    *,
    like: bool,
) -> None:
    """
    Single horizontal fling across the card.
    like=True → right; like=False → left.
    """
    jitter = float(swipe_cfg.get("jitter", 0.03))
    start_y = float(swipe_cfg["start_y"])
    end_y = float(swipe_cfg.get("end_y", start_y))
    # Fling across the full card from the trailing edge so Bumble registers it.
    # Like → start left, fling right. Pass → start right, fling left.
    if like:
        start_x = float(swipe_cfg.get("start_x", 0.22))
        end_x = float(swipe_cfg["end_x_like"])
    else:
        start_x = float(swipe_cfg.get("pass_start_x", 0.80))
        end_x = float(swipe_cfg["end_x_pass"])
    # Slight vertical drift so the path isn't perfectly flat.
    end_y = _clamp(_jitter(end_y, 0.02))

    x1, y1 = _frac_to_px(device, start_x, start_y, jitter)
    x2, y2 = _frac_to_px(device, end_x, end_y, jitter)

    duration = random.randint(
        int(swipe_cfg.get("duration_ms_min", 200)),
        int(swipe_cfg.get("duration_ms_max", 320)),
    )

    action = "like" if like else "pass"
    log.info("%s swipe (%d,%d) -> (%d,%d) %dms", action, x1, y1, x2, y2, duration)

    try:
        _adb_swipe(device, x1, y1, x2, y2, duration)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("adb swipe failed (%s); falling back to uiautomator2", exc)
        device.swipe(x1, y1, x2, y2, duration=duration / 1000.0)


def tap(device: u2.Device, x: int, y: int) -> None:
    log.info("tap (%d,%d)", x, y)
    try:
        serial = getattr(device, "serial", None)
        cmd = ["adb"]
        if isinstance(serial, str) and serial:
            cmd.extend(["-s", serial])
        cmd.extend(["shell", "input", "tap", str(x), str(y)])
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        device.click(x, y)


def sleep_between_swipes(delay_min: float, delay_max: float) -> None:
    seconds = random.uniform(delay_min, delay_max)
    log.info("wait %.1fs", seconds)
    time.sleep(seconds)
