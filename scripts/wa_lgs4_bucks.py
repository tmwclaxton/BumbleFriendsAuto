#!/usr/bin/env python3
"""Read the Bucks WhatsApp poll, then create LGS #4 (Bucks). Pixel only."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.device import dump_hierarchy
from src.whatsapp import (
    WA,
    _click,
    _open_whatsapp,
    _runtime,
    _wait,
    add_to_group,
    create_group,
)

BUCKS = "Let's Go Social (Bucks)"
NEW = "LGS #4 (Bucks)"
ORGANIZERS = "Toby + Archie"
# Pixel WhatsApp / known LGS numbers (also in tests).
TOBY = {"name": "Toby", "phone": "07837370669"}
ARCHIE = {"name": "Archie", "phone": "+447398727993"}

SKIP_NAMES = {
    "whatsapp",
    "you",
    "meta ai",
    "message",
    "search",
    "poll",
    "votes",
    "vote",
    "today",
    "yesterday",
    "online",
    "type a message",
}


def _texts(xml: str) -> list[str]:
    return [t for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]


def _descs(xml: str) -> list[str]:
    return [t for t in re.findall(r'content-desc="([^"]+)"', xml) if t.strip()]


def _open_chat(device, title: str) -> bool:
    from src.input_ime import type_into

    if not (
        _click(device, description="Ask Meta AI or Search")
        or _click(device, description="Search")
        or _click(device, resourceId="com.whatsapp:id/menuitem_search")
    ):
        return False
    box = (
        _wait(device, lambda: device(resourceId="com.whatsapp:id/search_src_text"))
        or _wait(device, lambda: device(className="android.widget.EditText"))
    )
    if box is None:
        return False
    type_into(device, box, title)
    time.sleep(0.9)
    return _click(device, text=title) or _click(device, textContains=title[:18])


def _connect():
    connect, _, serial_for, wake_and_unlock = _runtime()
    serial = serial_for("toby")
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        raise SystemExit("pixel still locked")
    return device


def _looks_like_name(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or raw.lower() in SKIP_NAMES:
        return False
    if re.fullmatch(r"\d+", raw):
        return False
    if re.search(r"\d+\s*vote", raw, re.I):
        return False
    if len(raw) > 40:
        return False
    if raw.lower() in {BUCKS.lower(), NEW.lower(), ORGANIZERS.lower()}:
        return False
    return bool(re.search(r"[A-Za-z]", raw))


def collect_poll_voters(device) -> list[str]:
    xml = dump_hierarchy(device)
    print("==== open bucks ====")
    for line in _texts(xml) + _descs(xml):
        if any(k in line.lower() for k in ("poll", "vote", "group", "join", "lgs")):
            print("ui:", line)

    tapped = False
    for needle in ("votes", "Vote", "poll", "Poll"):
        if device(textContains=needle).exists:
            device(textContains=needle).click()
            tapped = True
            time.sleep(1.2)
            break
        if device(descriptionContains=needle).exists:
            device(descriptionContains=needle).click()
            tapped = True
            time.sleep(1.2)
            break
    if not tapped:
        for _ in range(8):
            device.swipe(0.5, 0.28, 0.5, 0.78, 0.35)
            time.sleep(0.7)
            xml = dump_hierarchy(device)
            blob = " ".join(_texts(xml) + _descs(xml)).lower()
            if "poll" in blob or "vote" in blob:
                if device(textContains="vote").exists:
                    device(textContains="vote").click()
                    tapped = True
                    time.sleep(1.2)
                    break
                if device(descriptionContains="Poll").exists:
                    device(descriptionContains="Poll").click()
                    tapped = True
                    time.sleep(1.2)
                    break

    names: list[str] = []
    seen: set[str] = set()
    for _ in range(6):
        xml = dump_hierarchy(device)
        print("==== poll view ====")
        for line in _texts(xml):
            print("t:", line)
        for line in _descs(xml):
            print("d:", line)
        for line in _texts(xml) + _descs(xml):
            # "Alex voted" / contact rows
            m = re.match(r"^(.+?)\s+voted\b", line, re.I)
            candidate = m.group(1) if m else line
            if not _looks_like_name(candidate):
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(candidate.strip())
        device.swipe(0.5, 0.75, 0.5, 0.35, 0.3)
        time.sleep(0.6)

    # Prefer the "Yes / join" option's names if we also scraped option labels.
    return names


def main() -> int:
    device = _connect()
    _open_whatsapp(device)
    if not _open_chat(device, BUCKS):
        print("could not open", BUCKS)
        return 1
    time.sleep(1.0)
    voters = collect_poll_voters(device)
    print("VOTERS", voters)

    people = [TOBY, ARCHIE]
    seen = {TOBY["name"].casefold(), ARCHIE["name"].casefold()}
    for name in voters:
        key = name.casefold()
        if key in seen:
            continue
        if key in {"toby", "archie"}:
            continue
        seen.add(key)
        people.append({"name": name, "phone": ""})

    print("PEOPLE", people)
    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        return 0

    result = create_group(title=NEW, people=people, phone_id="toby")
    print("CREATE", result)
    if result.get("ok"):
        return 0
    # Group may already exist — add whoever we can.
    added = add_to_group(group=NEW, people=people, phone_id="toby")
    print("ADD", added)
    return 0 if added.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
