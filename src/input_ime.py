"""Keep AdbKeyboard selected so Gboard cannot swallow automation input."""

from __future__ import annotations

import logging
import subprocess
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

_ADB_IMES = (
    "com.github.uiautomator/.AdbKeyboard",
    "com.android.adbkeyboard/.AdbIME",
)


def _serial_of(device) -> str:
    return str(getattr(device, "serial", None) or "").strip()


def _adb(serial: str, *args: str) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=8).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("adb %s failed: %s", args[:3], exc)
        return ""


def current_ime(device) -> str:
    serial = _serial_of(device)
    return _adb(serial, "shell", "settings", "get", "secure", "default_input_method")


def set_ime(device, ime: str) -> bool:
    serial = _serial_of(device)
    if not ime:
        return False
    _adb(serial, "shell", "ime", "enable", ime)
    out = _adb(serial, "shell", "ime", "set", ime)
    now = current_ime(device)
    ok = now == ime or ime.split("/")[0] in now
    log.info("ime set %s (%s)", ime, "ok" if ok else f"now {now or out}")
    return ok


def ensure_adb_keyboard(device) -> str:
    """Select AdbKeyboard and leave it selected."""
    current = current_ime(device)
    if any(ime.split("/")[0] in current for ime in _ADB_IMES if current):
        return current
    for ime in _ADB_IMES:
        if set_ime(device, ime):
            return ime
    log.warning("no AdbKeyboard IME available — typing with %s", current)
    return current


@contextmanager
def automation_keyboard(device) -> Iterator[str]:
    """Switch to AdbKeyboard for typing and keep it on afterwards."""
    yield ensure_adb_keyboard(device)


def type_into(device, field, text: str) -> None:
    """Fill a focused EditText via AdbKeyboard. Newlines stay as line breaks."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    with automation_keyboard(device):
        field.click()
        try:
            field.clear_text()
        except Exception:
            try:
                field.set_text("")
            except Exception:
                pass
        try:
            field.set_text(text)
            return
        except Exception:
            log.warning("set_text failed", exc_info=True)
        _type_multiline(device, text)


def _type_multiline(device, text: str) -> None:
    serial = _serial_of(device)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            encoded = line.replace(" ", "%s").replace("'", "")
            if encoded:
                _adb(serial, "shell", "input", "text", encoded)
        if i < len(lines) - 1:
            # Multiline composers insert a break; do not use this as Send.
            _adb(serial, "shell", "input", "keyevent", "66")
