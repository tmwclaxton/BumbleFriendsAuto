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
from src.gestures import _adb_swipe, sleep_between_swipes, tap
from src.screen import find_tab_point

log = logging.getLogger(__name__)

INPUT_RID = "com.bumblebff.app:id/chatInput_text"
_STRIP_RID = "com.bumblebff.app:id/connections_connectionsListExpiring"


def _strip_to_start(device) -> None:
    rv = device(resourceId=_STRIP_RID)
    if rv.exists:
        try:
            rv.fling.horiz.toBeginning()
            wait_idle(device, 0.6)
            return
        except Exception:
            pass
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    y = int(height * 0.365)
    _adb_swipe(device, int(width * 0.18), y, int(width * 0.82), y, 480)
    wait_idle(device, 0.45)


def _advance_strip(device) -> None:
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    y = int(height * 0.365)
    rv = device(resourceId=_STRIP_RID)
    if rv.exists:
        try:
            rv.swipe("left", steps=45)
            wait_idle(device, 0.45)
            return
        except Exception:
            pass
    _adb_swipe(device, int(width * 0.82), y, int(width * 0.18), y, 480)
    wait_idle(device, 0.45)


def _tappable_friends(xml: str, device) -> list:
    width = int(device.info["displayWidth"])
    friends = list_new_friends(xml)
    return [f for f in friends if 120 <= int(f.x) <= width - 120]


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

    from src.unlock import screen_lock_state, wake_and_unlock

    cfg = load_config()
    package = str(cfg["package"])
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial) or screen_lock_state(device) != "unlocked":
        return False, "phone still locked — unlock failed"
    from src.phone_queue import check_cancel

    check_cancel()
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


def send_new_friend_openers(cfg: dict, *, dry_run: bool = False, serial: str | None = None) -> tuple[int, int]:
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

    already: set[str] = set()
    try:
        from src.store import connect as db_connect, db_path_from_config, list_people

        conn = db_connect(db_path_from_config(cfg))
        try:
            already = {
                str(row["name"]).lower()
                for row in list_people(conn)
                if int(row["opener_sent"] or 0)
            }
        finally:
            conn.close()
    except Exception:
        log.warning("could not load opener_sent flags")

    device = connect(serial)
    if cfg.get("bring_to_foreground", True):
        bring_app_foreground(device, package)

    xml = go_to_chats(device, package)
    _strip_to_start(device)
    xml = dump_hierarchy(device)
    friends = _tappable_friends(xml, device)
    log.info("new friends visible: %d — %s", len(friends), ", ".join(f.name for f in friends) or "-")

    if dry_run:
        shown = _tappable_friends(xml, device)[:max_messages]
        for friend in shown:
            body = format_opener(template, friend.name)
            log.info("dry-run would message %s: %s", friend.name, body)
        log.info("dry-run done (no messages sent)")
        return 0, 0

    sent = 0
    skipped = 0
    attempted: set[str] = set()
    empty_rounds = 0

    def _token(friend) -> str:
        return f"{friend.name.lower()}@{int(friend.x) // 40}"

    while sent < max_messages:
        from src.phone_queue import check_cancel

        check_cancel()
        xml = ensure_chats_list(device, package)
        friends = [f for f in _tappable_friends(xml, device) if _token(f) not in attempted]
        if not friends:
            empty_rounds += 1
            if empty_rounds >= 6:
                log.info("no more new-friend circles")
                break
            _advance_strip(device)
            continue
        empty_rounds = 0

        friend = friends[0]
        attempted.add(_token(friend))
        face = None
        try:
            from src.photos import crop_list_face, match_face_to_namesakes

            face = crop_list_face(
                device, xml, friend.name, x=int(friend.x), y=int(friend.y)
            )
            matched = match_face_to_namesakes(face, friend.name) if face is not None else None
            if matched and matched.lower() in already:
                log.info("skip %s — face already messaged as %s", friend.name, matched)
                skipped += 1
                continue
        except Exception:
            log.debug("strip face match failed", exc_info=True)
            matched = None
        log.info("open new friend: %s @%s", friend.name, friend.x)
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
            from src.store import connect as db_connect, db_path_from_config, mark_opener_sent
            from src.sync_chats import _save_name_for_thread

            conn = db_connect(db_path_from_config(cfg))
            try:
                save_as = _save_name_for_thread(conn, partner, [], face=face)
                mark_opener_sent(conn, save_as, body)
                already.add(save_as.lower())
                if save_as != partner:
                    log.info("recorded opener for %s as %s", partner, save_as)
            finally:
                conn.close()
        except Exception as exc:
            log.warning("could not record opener in db: %s", exc)
        leave_chat(device)
        ensure_chats_list(device, package)

        if sent < max_messages:
            sleep_between_swipes(delay_min, delay_max)

    log.info("messenger done sent=%d skipped=%d", sent, skipped)
    return sent, skipped


def message_new_friends(*, serial: str | None = None, sleep_after: bool = True) -> tuple[bool, str]:
    """Unlock, send the template opener to every empty New-friends match, sleep."""
    from src.unlock import sleep_screen, wake_and_unlock

    cfg = load_config()
    msg = dict(cfg.get("messenger") or {})
    msg["max_messages"] = max(int(msg.get("max_messages") or 20), 80)
    cfg = {**cfg, "messenger": msg}
    device = connect(serial)
    try:
        if not wake_and_unlock(device, serial=serial):
            return False, "phone still locked — unlock failed"
        sent, skipped = send_new_friend_openers(cfg, serial=serial)
        return True, f"sent opener to {sent} new friend(s), skipped {skipped}"
    except Exception as exc:
        from src.phone_queue import QueueCancelled

        if isinstance(exc, QueueCancelled):
            log.info("message_new_friends cancelled")
            return False, "cancelled"
        log.exception("message_new_friends failed")
        return False, str(exc)
    finally:
        if sleep_after:
            try:
                sleep_screen(device, serial=serial)
            except Exception:
                log.warning("could not sleep screen after messaging new friends")


def run_messenger(cfg: dict, *, dry_run: bool = False, serial: str | None = None) -> int:
    send_new_friend_openers(cfg, dry_run=dry_run, serial=serial)
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
