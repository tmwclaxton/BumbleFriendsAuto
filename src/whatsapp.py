"""Create WhatsApp groups or add people on the Pixel (Toby) handset."""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

WA = "com.whatsapp"
KNOWN_GROUPS = (
    "Let's Go Social (Bucks)",
    "Let’s Go Social (London)",
    "Let's Go Social (London)",
    "Toby + Archie",
)


def listed_groups() -> list[str]:
    """Stable chooser labels. Keep both London apostrophes for matching."""
    seen: set[str] = set()
    out: list[str] = []
    for name in KNOWN_GROUPS:
        key = name.replace("’", "'")
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _digits(phone: str) -> str:
    raw = re.sub(r"\D+", "", phone or "")
    if raw.startswith("00"):
        raw = raw[2:]
    if raw.startswith("44") and len(raw) >= 12:
        raw = "0" + raw[2:]
    return raw


def _runtime():
    from src.device import connect
    from src.input_ime import type_into
    from src.phones import serial_for
    from src.unlock import wake_and_unlock

    return connect, type_into, serial_for, wake_and_unlock


def _wait(device, getter, timeout: float = 8.0):
    end = time.time() + timeout
    while time.time() < end:
        node = getter()
        if node.exists:
            return node
        time.sleep(0.25)
    return None


def _click(device, **sel) -> bool:
    node = device(**sel)
    if node.exists:
        node.click()
        time.sleep(0.6)
        return True
    return False


def _open_whatsapp(device) -> None:
    try:
        device.app_stop("com.bumblebff.app")
    except Exception:
        pass
    device.app_stop(WA)
    time.sleep(0.4)
    device.app_start(WA)
    time.sleep(2.2)


def _open_new_group(device) -> bool:
    _open_whatsapp(device)
    if device(text="New group").exists and device(description="Name, number, @username").exists:
        return True
    if not _click(device, description="New chat"):
        return False
    time.sleep(0.8)
    return _click(device, text="New group")


def parse_people(raw) -> list[dict]:
    people = []
    if not isinstance(raw, list):
        return people
    for item in raw:
        if not isinstance(item, dict):
            continue
        phone = str(item.get("phone") or item.get("number") or "").strip()
        name = str(item.get("name") or "").strip()
        if not phone:
            continue
        people.append({"name": name, "phone": phone})
    return people


def _select_participant(device, phone: str, label: str = "") -> bool:
    from src.input_ime import type_into

    query = _digits(phone) or (label or "").strip()
    if not query:
        return False
    field = (
        _wait(device, lambda: device(resourceId="com.whatsapp:id/search_src_text"))
        or _wait(device, lambda: device(description="Name, number, @username"))
        or _wait(device, lambda: device(text="Name, number, @username"))
    )
    if field is None:
        return False
    type_into(device, field, query[-10:] if query.isdigit() else query)
    time.sleep(0.9)
    row = (
        _wait(device, lambda: device(resourceId="com.whatsapp:id/chat_able_contacts_row_name"), 4)
        or _wait(device, lambda: device(resourceId="com.whatsapp:id/contactpicker_row_name"), 2)
    )
    if row is None:
        return False
    name = ""
    try:
        name = str(row.get_text() or "")
    except Exception:
        name = ""
    row.click()
    time.sleep(0.5)
    return True


def _create_named_group(device, title: str) -> bool:
    from src.input_ime import type_into

    if (
        not _click(device, resourceId="com.whatsapp:id/next_btn")
        and not _click(device, description="Next")
        and not _click(device, text="Next")
    ):
        return False
    time.sleep(0.8)
    subject = (
        _wait(device, lambda: device(resourceId="com.whatsapp:id/group_name"))
        or _wait(device, lambda: device(textContains="Group subject"))
        or _wait(device, lambda: device(className="android.widget.EditText"))
    )
    if subject is None:
        return False
    type_into(device, subject, title)
    time.sleep(0.4)
    if _click(device, description="Create") or _click(device, text="Create"):
        time.sleep(2.0)
        return True
    # Checkmark FAB
    if _click(device, resourceId="com.whatsapp:id/ok_btn"):
        time.sleep(2.0)
        return True
    device.click(0.90, 0.92)
    time.sleep(2.0)
    return True


def create_group(
    *,
    title: str,
    people: list[dict],
    phone_id: str = "toby",
) -> dict:
    """people: [{name, phone}, ...]. Uses the Pixel WhatsApp account."""
    title = (title or "").strip() or "Let's Go Social"
    members = parse_people(people)
    if len(members) < 1:
        return {"ok": False, "error": "need at least one phone number"}

    connect, _, serial_for, wake_and_unlock = _runtime()
    serial = serial_for(phone_id)
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        return {"ok": False, "error": "phone still locked"}

    if not _open_new_group(device):
        return {"ok": False, "error": "could not open WhatsApp New group"}

    selected = []
    failed = []
    for person in members:
        if _select_participant(device, person["phone"], person["name"]):
            selected.append(person)
        else:
            failed.append(person)

    if not selected:
        return {"ok": False, "error": "could not select anyone in WhatsApp", "failed": failed}

    if not _create_named_group(device, title):
        return {
            "ok": False,
            "error": "selected people but could not finish creating the group",
            "selected": selected,
            "failed": failed,
        }

    return {
        "ok": True,
        "title": title,
        "selected": selected,
        "failed": failed,
    }


def add_to_group(
    *,
    group: str,
    people: list[dict],
    phone_id: str = "toby",
) -> dict:
    """Open an existing WhatsApp group and add participants."""
    group = (group or "").strip()
    members = parse_people(people)
    if not group:
        return {"ok": False, "error": "group name required"}
    if not members:
        return {"ok": False, "error": "need at least one phone number"}

    connect, type_into, serial_for, wake_and_unlock = _runtime()
    serial = serial_for(phone_id)
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        return {"ok": False, "error": "phone still locked"}

    _open_whatsapp(device)
    if not (
        _click(device, description="Ask Meta AI or Search")
        or _click(device, description="Search")
        or _click(device, resourceId="com.whatsapp:id/menuitem_search")
    ):
        return {"ok": False, "error": "could not search chats"}
    time.sleep(0.4)
    box = (
        _wait(device, lambda: device(resourceId="com.whatsapp:id/search_src_text"))
        or _wait(device, lambda: device(className="android.widget.EditText"))
    )
    if box is None:
        return {"ok": False, "error": "could not search chats"}
    type_into(device, box, group)
    time.sleep(0.8)
    if not _click(device, text=group) and not _click(device, textContains=group[:18]):
        return {"ok": False, "error": f"could not open group {group!r}"}
    time.sleep(1.0)
    if _click(device, resourceId="com.whatsapp:id/conversation_contact"):
        time.sleep(0.8)
    elif _click(device, description="More options"):
        time.sleep(0.4)
        if not _click(device, text="Group info") and not _click(device, textContains="Group info"):
            return {"ok": False, "error": "no Group info"}
        time.sleep(0.8)
    if not (
        _click(device, text="Add members")
        or _click(device, text="Add participants")
        or _click(device, textContains="Add member")
    ):
        return {"ok": False, "error": "no Add members"}
    time.sleep(0.8)

    selected, failed = [], []
    for person in members:
        if _select_participant(device, person["phone"], person["name"]):
            selected.append(person)
        else:
            failed.append(person)
    if not selected:
        return {"ok": False, "error": "could not select anyone to add", "failed": failed}
    try:
        device.press("back")
        time.sleep(0.3)
    except Exception:
        pass
    if not (
        _click(device, text="ADD MEMBERS")
        or _click(device, text="Add members")
        or _click(device, resourceId="com.whatsapp:id/next_btn")
        or _click(device, description="Next")
        or _click(device, text="OK")
        or _click(device, description="Add")
        or _click(device, resourceId="com.whatsapp:id/ok_btn")
    ):
        return {"ok": False, "error": "could not confirm add", "selected": selected, "failed": failed}
    time.sleep(1.2)
    if device(text="Add").exists:
        device(text="Add").click()
        time.sleep(1.0)
    return {"ok": True, "group": group, "selected": selected, "failed": failed}
