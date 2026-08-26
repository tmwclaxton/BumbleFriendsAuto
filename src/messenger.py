"""Message New friends (Chats tab circles) with a template opener."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src.chats import (
    chat_partner_name,
    dismiss_icebreaker_if_present,
    find_back_button,
    find_send_button,
    format_opener,
    is_empty_outbound_chat,
    list_new_friends,
)
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import sleep_between_swipes, tap
from src.screen import find_tab_point

log = logging.getLogger(__name__)

INPUT_RID = "com.bumblebff.app:id/chatInput_text"


def go_to_chats(device, package: str) -> str:
    xml = dump_hierarchy(device)
    for _ in range(3):
        if "navbar_search" not in xml:
            break
        log.info("leave chats search")
        device.press("back")
        wait_idle(device, 0.8)
        xml = dump_hierarchy(device)
    if chat_partner_name(xml) or "chatInput_text" in xml:
        leave_chat(device)
        xml = dump_hierarchy(device)
    point = find_tab_point(xml, "Chats")
    if point is None:
        log.warning("Chats tab not visible; refusing composer-area fallback")
        return dump_hierarchy(device)
    log.info("navigate Chats tab @ %s", point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.8)
    return dump_hierarchy(device)


def ensure_chats_list(device, package: str) -> str:
    """Return hierarchy for Chats with New friends row; navigate if needed."""
    xml = dump_hierarchy(device)
    if list_new_friends(xml) or "New friends" in xml:
        return xml
    return go_to_chats(device, package)


def leave_chat(device) -> None:
    xml = dump_hierarchy(device)
    if "connectionItem" in xml and "chatInput_text" not in xml:
        return
    if not (chat_partner_name(xml) or "chatInput_text" in xml):
        return
    back = find_back_button(xml)
    if back:
        log.info("leave chat via toolbar Back @ %s", back)
        tap(device, back[0], back[1])
    else:
        log.info("leave chat via system back")
        device.press("back")
    wait_idle(device, 1.3)
    xml = dump_hierarchy(device)
    if "connectionItem" in xml and "chatInput_text" not in xml:
        return
    if chat_partner_name(xml) or "chatInput_text" in xml:
        log.info("still in thread after leave; system back")
        device.press("back")
        wait_idle(device, 1.0)


def send_opener(device, message: str) -> bool:
    field = device(resourceId=INPUT_RID)
    if not field.exists(timeout=3.0):
        log.warning("composer not found")
        return False

    field.click()
    wait_idle(device, 0.4)
    field.set_text(message)
    wait_idle(device, 0.8)

    xml = dump_hierarchy(device)
    send = find_send_button(xml)
    if send is None:
        # Send often appears only after text is set; try resource id directly.
        btn = device(resourceId="com.bumblebff.app:id/chatInput_button_send")
        if btn.exists(timeout=2.0):
            btn.click()
        else:
            log.warning("send button not found — leaving text unsent and backing out")
            try:
                field.clear_text()
            except Exception:
                pass
            return False
    else:
        tap(device, send[0], send[1])

    wait_idle(device, 1.0)
    return True


def send_named_message(name: str, text: str, *, serial: str | None = None) -> tuple[bool, str]:
    """Search-open a chat on the phone, send `text`, record it in SQLite."""
    text = text.strip()
    name = name.strip()
    if not name or not text:
        return False, "name and message required"

    from src.store import add_message, connect as db_connect, db_path_from_config, upsert_chat
    from src.sync_chats import open_chat_from_list, open_chat_via_search, recover_to_list

    cfg = load_config()
    package = str(cfg["package"])
    device = connect(serial)
    bring_app_foreground(device, package)
    wait_idle(device, 0.8)
    partner = open_chat_via_search(device, package, name)
    if not partner:
        recover_to_list(device, package)
        partner = open_chat_from_list(device, package, name)
    if not partner:
        return False, f"could not open chat with {name}"
    ok = send_opener(device, text)
    try:
        leave_chat(device)
    except Exception:
        pass
    if not ok:
        return False, "composer/send failed — nothing sent"
    conn = db_connect(db_path_from_config(cfg))
    person_id = upsert_chat(conn, partner, last_from="you", last_text=text, badge="")
    add_message(conn, person_id, "you", text)
    conn.commit()
    return True, f"sent to {partner}"


def run_messenger(cfg: dict, *, dry_run: bool = False, serial: str | None = None) -> int:
    package = str(cfg["package"])
    msg_cfg = dict(cfg.get("messenger") or {})
    template = str(
        msg_cfg.get(
            "template",
            "Hi {name}, I'm putting together a wee group for hiking / board games / sports. "
            "Does that sound like something you would be interested in?",
        )
    )
    delay_min = float(msg_cfg.get("delay_min", cfg.get("delay_min", 2.5)))
    delay_max = float(msg_cfg.get("delay_max", cfg.get("delay_max", 5.0)))
    max_messages = int(msg_cfg.get("max_messages", 20))
    type_pause = float(msg_cfg.get("type_pause", 0.8))

    device = connect(serial)
    if cfg.get("bring_to_foreground", True):
        bring_app_foreground(device, package)

    xml = go_to_chats(device, package)
    friends = list_new_friends(xml)
    log.info("new friends visible: %d — %s", len(friends), ", ".join(f.name for f in friends) or "-")

    if not friends:
        log.info("nothing to message")
        return 0

    if dry_run:
        for friend in friends[:max_messages]:
            body = format_opener(template, friend.name)
            log.info("dry-run would message %s: %s", friend.name, body)
        log.info("dry-run done (no messages sent)")
        return 0

    sent = 0
    skipped = 0
    # Re-query the row each time — circles disappear after you message.
    attempted: set[str] = set()
    empty_rounds = 0

    while sent < max_messages:
        xml = ensure_chats_list(device, package)
        friends = list_new_friends(xml)
        remaining = [f for f in friends if f.name.lower() not in attempted]
        if not remaining:
            empty_rounds += 1
            if empty_rounds >= 2:
                log.info("no more new-friend circles")
                break
            wait_idle(device, 1.0)
            go_to_chats(device, package)
            continue
        empty_rounds = 0

        friend = remaining[0]
        attempted.add(friend.name.lower())
        log.info("open new friend: %s", friend.name)
        tap(device, friend.x, friend.y)
        wait_idle(device, 2.2)

        chat_xml = dump_hierarchy(device)
        partner = chat_partner_name(chat_xml) or friend.name

        ice = dismiss_icebreaker_if_present(chat_xml)
        if ice:
            log.info("dismiss icebreaker @ %s", ice)
            tap(device, ice[0], ice[1])
            wait_idle(device, 1.0)
            chat_xml = dump_hierarchy(device)

        if not is_empty_outbound_chat(chat_xml):
            log.info("skip %s — chat not empty / they may have messaged", partner)
            skipped += 1
            leave_chat(device)
            ensure_chats_list(device, package)
            continue

        body = format_opener(template, partner)
        log.info("send to %s: %s", partner, body)
        time.sleep(type_pause)
        ok = send_opener(device, body)
        if not ok:
            log.warning("failed to send to %s", partner)
            leave_chat(device)
            ensure_chats_list(device, package)
            skipped += 1
            continue

        sent += 1
        log.info("sent %d/%d → %s", sent, max_messages, partner)
        try:
            from src.store import connect as db_connect, mark_opener_sent

            mark_opener_sent(db_connect(), partner, body)
        except Exception as exc:
            log.warning("could not record opener in db: %s", exc)
        leave_chat(device)
        ensure_chats_list(device, package)

        if sent < max_messages:
            more = list_new_friends(dump_hierarchy(device))
            if not more:
                wait_idle(device, 1.2)
                more = list_new_friends(go_to_chats(device, package))
            if not more:
                log.info("no more new-friend circles")
                break
            sleep_between_swipes(delay_min, delay_max)

    log.info("messenger done sent=%d skipped=%d", sent, skipped)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Message Bumble BFF New friends with a template opener",
    )
    parser.add_argument("--serial", help="ADB device serial (optional)")
    parser.add_argument("--config", type=Path, help="Path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List who would be messaged; do not open chats or send",
    )
    parser.add_argument("--max-messages", type=int, help="Cap how many openers to send")
    parser.add_argument(
        "--no-foreground",
        action="store_true",
        help="Do not bring Bumble to the foreground at start",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    if args.max_messages is not None:
        cfg.setdefault("messenger", {})
        cfg["messenger"]["max_messages"] = args.max_messages
    if args.no_foreground:
        cfg["bring_to_foreground"] = False

    return run_messenger(cfg, dry_run=args.dry_run, serial=args.serial)


if __name__ == "__main__":
    sys.exit(main())
