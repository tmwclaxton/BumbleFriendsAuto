#!/usr/bin/env python3
"""Read-only probe of every attached ADB phone: size, package, Bumble ids."""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.device import connect, dump_hierarchy, take_screenshot  # noqa: E402
from src.phones import list_phones, phone_by_serial  # noqa: E402
from src.unlock import wake_and_unlock  # noqa: E402

log = logging.getLogger("probe_phones")

_INTERESTING = (
    "chatInput_text",
    "chatInput_button_send",
    "connectionItem",
    "personName",
    "connectionItem_badge",
    "tabBar",
    "People",
    "Chats",
)


def _adb(*args: str) -> str:
    try:
        return subprocess.check_output(["adb", *args], text=True, stderr=subprocess.STDOUT, timeout=12)
    except Exception as exc:
        return f"ERR {exc}"


def _ids(xml: str) -> list[str]:
    found = sorted(set(re.findall(r'resource-id="([^"]+)"', xml)))
    return [i for i in found if any(k.lower() in i.lower() for k in _INTERESTING)]


def _root_size(xml: str) -> tuple[int, int]:
    w = h = 0
    for match in re.finditer(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", xml):
        w = max(w, int(match.group(3)))
        h = max(h, int(match.group(4)))
    return w, h


def probe_serial(serial: str, dump_dir: Path, *, unlock: bool) -> None:
    known = phone_by_serial(serial)
    label = f"{known['id']} ({known['label']})" if known else serial
    print(f"\n=== {label}  serial={serial} ===")
    print("wm size   ", _adb("-s", serial, "shell", "wm", "size").strip())
    print("wm density", _adb("-s", serial, "shell", "wm", "density").strip())
    print("getprop model", _adb("-s", serial, "shell", "getprop", "ro.product.model").strip())
    print("android   ", _adb("-s", serial, "shell", "getprop", "ro.build.version.release").strip())
    print("devices -l", _adb("devices", "-l"))
    device = connect(serial)
    info = device.info or {}
    print(
        "u2 info   ",
        info.get("productName") or info.get("model"),
        f"{info.get('displayWidth')}x{info.get('displayHeight')}",
        "sdk",
        info.get("sdkInt"),
    )
    if unlock:
        from src.phones import pin_for

        pid = (known or {}).get("id")
        ok = wake_and_unlock(device, serial=serial, pin=pin_for(pid) if pid else None)
        print("unlock    ", ok)
    xml = dump_hierarchy(device)
    rw, rh = _root_size(xml)
    print("xml root  ", f"{rw}x{rh}")
    pkgs = sorted(set(re.findall(r'package="([^"]+)"', xml)))
    print("packages  ", ", ".join(pkgs[:8]))
    ids = _ids(xml)
    print("bumble ids")
    for rid in ids[:40]:
        print("  ", rid)
    slug = (known or {}).get("id") or serial[-6:]
    png = dump_dir / f"probe-{slug}.png"
    xml_path = dump_dir / f"probe-{slug}.xml"
    take_screenshot(device, png)
    xml_path.write_text(xml, encoding="utf-8")
    print("wrote     ", png, xml_path)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-unlock", action="store_true")
    parser.add_argument("--dump-dir", default="dumps/probe")
    args = parser.parse_args()
    dump_dir = Path(args.dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    print("configured phones:")
    for p in list_phones():
        print(f"  {p['id']:8} {p['label']:8} serial={p.get('serial') or '(none)'}  {p.get('notes')}")
    listing = _adb("devices")
    print("adb devices\n", listing)
    serials = [
        line.split()[0]
        for line in listing.splitlines()
        if "\tdevice" in line
    ]
    if not serials:
        print("no attached devices")
        return 1
    for serial in serials:
        try:
            probe_serial(serial, dump_dir, unlock=not args.no_unlock)
        except Exception as exc:
            log.exception("probe failed for %s: %s", serial, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
