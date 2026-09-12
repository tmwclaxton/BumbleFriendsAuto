"""Dump rematch/unmatch controls on an expired thread. Does not confirm."""

from __future__ import annotations

import logging
import re
import sys
from xml.etree import ElementTree as ET

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.messenger import leave_chat
from src.phones import serial_for
from src.sync_chats import open_chat_from_list, open_chat_via_search, recover_to_list
from src.unlock import sleep_screen, wake_and_unlock

log = logging.getLogger(__name__)
KEYS = (
    "unmatch",
    "rematch",
    "extend",
    "reconnect",
    "expired",
    "overflow",
    "option",
    "report",
)


def dump_nodes(xml: str, title: str) -> None:
    print(f"=== {title} ===")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        print("parse fail")
        return
    texts = []
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").split("/")[-1]
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        click = node.attrib.get("clickable")
        bounds = node.attrib.get("bounds") or ""
        if text:
            texts.append(text)
        blob = f"{rid} {text} {desc}".lower()
        keep = any(k in blob for k in KEYS) or (
            click == "true" and bool(text or desc) and "chatmessage" not in rid.lower()
        )
        if keep:
            print(f"  click={click} rid={rid!r} text={text!r} desc={desc!r} bounds={bounds}")
    print("TEXT:", " | ".join(texts[:100]))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    name = (sys.argv[1] if len(sys.argv) > 1 else "Callum").strip()
    phone = (sys.argv[2] if len(sys.argv) > 2 else "toby").strip()
    cfg = load_config()
    package = str(cfg["package"])
    serial = serial_for(phone)
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        print("unlock failed")
        return 1
    bring_app_foreground(device, package)
    wait_idle(device, 0.6)
    recover_to_list(device, package)
    partner = open_chat_via_search(device, package, name) or open_chat_from_list(device, package, name)
    print("opened", partner, "wanted", name)
    xml = dump_hierarchy(device)
    dump_nodes(xml, "THREAD")
    overflow = None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = None
    if root is not None:
        for node in root.iter():
            rid = node.attrib.get("resource-id") or ""
            desc = (node.attrib.get("content-desc") or "").lower()
            if rid.endswith("chatToolbar_overflow") or desc == "chat options":
                box = node.attrib.get("bounds") or ""
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", box)
                if m:
                    overflow = ((int(m.group(1)) + int(m.group(3))) // 2, (int(m.group(2)) + int(m.group(4))) // 2)
    if overflow:
        tap(device, overflow[0], overflow[1])
        wait_idle(device, 1.0)
        dump_nodes(dump_hierarchy(device), "OVERFLOW")
        device.press("back")
        wait_idle(device, 0.5)
    try:
        leave_chat(device)
    finally:
        sleep_screen(device, serial=serial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
