"""ADB / uiautomator2 device connection helpers."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import uiautomator2 as u2

log = logging.getLogger(__name__)

_SYSTEM_PACKAGES = frozenset(
    {
        "com.android.systemui",
        "com.android.launcher",
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.huawei.android.launcher",
        "com.miui.home",
    }
)


def connect(serial: str | None = None) -> u2.Device:
    """Connect to a USB/Wi-Fi Android device via ADB."""
    if serial:
        device = u2.connect(serial)
    else:
        device = u2.connect()
    info = device.info
    log.info(
        "Connected: %s (%sx%s)",
        info.get("productName") or info.get("model") or "device",
        info.get("displayWidth"),
        info.get("displayHeight"),
    )
    return device


def wait_idle(device: u2.Device, seconds: float = 0.8) -> None:
    """Brief settle so the UI hierarchy is stable before dumping."""
    time.sleep(seconds)


def _package_from_adb_focus(serial: str | None = None) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "dumpsys", "window"])
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=8)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    for line in out.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            match = re.search(r"([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.]+", line)
            if match:
                return match.group(1)
    return ""


def _package_from_hierarchy(xml: str) -> str:
    counts: dict[str, int] = {}
    try:
        root = ET.fromstring(xml)
        for node in root.iter():
            pkg = node.attrib.get("package") or ""
            if pkg and pkg not in _SYSTEM_PACKAGES:
                counts[pkg] = counts.get(pkg, 0) + 1
    except ET.ParseError:
        for pkg in re.findall(r'package="([^"]+)"', xml):
            if pkg not in _SYSTEM_PACKAGES:
                counts[pkg] = counts.get(pkg, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def current_package(device: u2.Device, xml: str | None = None) -> str:
    """
    Resolve the foreground app package.

    On some Huawei devices uiautomator2's app_current() reports a stale package,
    so prefer hierarchy / dumpsys / device.info.
    Transition frames are often systemui-only — ignore those and use dumpsys.
    """
    from_xml = _package_from_hierarchy(xml) if xml else ""
    serial = getattr(device, "serial", None)
    from_adb = _package_from_adb_focus(serial if isinstance(serial, str) else None)

    if from_xml and from_xml not in _SYSTEM_PACKAGES:
        return from_xml
    if from_adb and from_adb not in _SYSTEM_PACKAGES:
        return from_adb

    info_pkg = str(device.info.get("currentPackageName") or "")
    if info_pkg and info_pkg not in _SYSTEM_PACKAGES:
        return info_pkg

    try:
        app = device.app_current()
        return str(app.get("package") or "")
    except Exception:
        return from_xml or from_adb or info_pkg


def bring_app_foreground(device: u2.Device, package: str) -> None:
    """Launch / resume the app if installed. Does not handle login."""
    device.app_start(package, stop=False)
    wait_idle(device, 1.2)


def dump_hierarchy(device: u2.Device) -> str:
    """Return the current UI hierarchy as XML. Retry briefly on ADB blips."""
    last: Exception | None = None
    for attempt in range(4):
        try:
            return device.dump_hierarchy()
        except Exception as exc:
            last = exc
            log.warning("hierarchy dump failed (%s); retry %d", exc, attempt + 1)
            time.sleep(0.6 + attempt * 0.4)
    if last:
        raise last
    return ""


def take_screenshot(device: u2.Device, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    device.screenshot(str(path))
    return path


def dump_artifacts(device: u2.Device, dump_dir: Path, prefix: str = "ui") -> dict[str, Path]:
    """Write hierarchy XML + screenshot for selector tuning."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    xml_path = dump_dir / f"{prefix}-{stamp}.xml"
    png_path = dump_dir / f"{prefix}-{stamp}.png"

    wait_idle(device)
    xml = dump_hierarchy(device)
    xml_path.write_text(xml, encoding="utf-8")
    take_screenshot(device, png_path)

    pkg = current_package(device, xml)
    log.info("Dumped %s (package=%s)", xml_path.name, pkg)
    log.info("Screenshot %s", png_path.name)
    return {"xml": xml_path, "png": png_path}
