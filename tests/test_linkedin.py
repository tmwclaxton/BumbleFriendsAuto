import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.linkedin_screen import (
    find_archive_action,
    find_conversation_hit,
    find_dismiss_point,
    find_inbox_search,
    find_more_options,
    find_messaging_entry,
    find_nav_point,
    find_reaction_tray,
    find_send_point,
    fold_thread_chunk,
    hierarchy_is_linkedin,
    inmail_kind,
    is_thread_chrome,
    looks_like_feed,
    looks_like_in_app_web,
    looks_like_inmail_promo,
    looks_like_messaging,
    looks_like_profile,
    looks_like_share_sheet,
    message_visible,
    names_match,
    parse_feed_posts,
    parse_messaging_list,
    parse_open_thread,
    should_open_row,
)
from src.phone_queue import cron_skip_reason
from src.phones import phone_scope
from src.store import connect, list_people, upsert_chat


FIXTURES = Path(__file__).parent / "fixtures"


class LinkedInParseTests(unittest.TestCase):
    def test_messaging_list_and_unread(self):
        xml = (FIXTURES / "linkedin_messaging.xml").read_text(encoding="utf-8")
        hits = parse_messaging_list(xml)
        names = [h.name for h in hits]
        self.assertIn("Ada Lovelace", names)
        grace = next(h for h in hits if h.name == "Grace Hopper")
        self.assertTrue(grace.unread)
        self.assertTrue(should_open_row(grace, "old preview"))
        ada = next(h for h in hits if h.name == "Ada Lovelace")
        self.assertFalse(should_open_row(ada, ada.preview))
        self.assertTrue(should_open_row(ada, "different preview"))
        self.assertEqual(find_nav_point(xml, "Messaging")[0], 780)
        self.assertTrue(names_match("Ada Lovelace", "ada  lovelace"))
        self.assertTrue(names_match("Patricia Mae Fregil", "Patricia Fregil"))
        self.assertTrue(names_match("Francesco Federico", "Francesco  Federico"))
        self.assertFalse(names_match("Michael Otto", "Michael Smith"))
        self.assertFalse(names_match("Ada", "Ada Lovelace"))
        self.assertEqual(find_conversation_hit(xml, "Ada Lovelace").name, "Ada Lovelace")
        self.assertIsNone(find_conversation_hit(xml, "Nobody Here"))

    def test_open_thread(self):
        xml = (FIXTURES / "linkedin_thread.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "Grace Hopper")
        self.assertEqual(msgs[0], ("them", "Can you send the deck?"))
        self.assertEqual(msgs[-1][0], "you")

    def test_fold_older_screen_in_front(self):
        thread: list = []
        seen: set = set()
        latest = [("them", "Thanks but I don’t run a charity."), ("you", "Hi Rebecca, Founder of GrantGunner")]
        older = [
            ("you", "Hey Rebecca, keen to donate a week"),
            ("you", "Hi again Rebecca, just following up"),
            ("them", "Thanks but I don’t run a charity."),
        ]
        self.assertEqual(fold_thread_chunk(thread, seen, latest, prepend=False), 2)
        self.assertEqual(fold_thread_chunk(thread, seen, older, prepend=True), 2)
        self.assertEqual(
            [body for _side, body in thread],
            [
                "Hey Rebecca, keen to donate a week",
                "Hi again Rebecca, just following up",
                "Thanks but I don’t run a charity.",
                "Hi Rebecca, Founder of GrantGunner",
            ],
        )

    def test_fold_overlap_ignores_incomplete_header_timestamp(self):
        thread = [
            ("them", "Same visible reply", "2026-09-12T11:45:00Z"),
            ("you", "Same visible follow-up", "2026-06-11T12:39:00Z"),
        ]
        older = [
            ("you", "Earlier opener", "2026-02-06T13:26:00Z"),
            ("them", "Same visible reply"),
            ("you", "Same visible follow-up"),
        ]
        added = fold_thread_chunk(thread, set(), older, prepend=True)
        self.assertEqual(added, 1)
        self.assertEqual([item[1] for item in thread].count("Same visible reply"), 1)

    def test_toby_sender_is_you_with_clock(self):
        from src.linkedin_screen import parse_open_thread, polish_message, sender_is_self

        self.assertTrue(sender_is_self("Toby Claxton x  •  5:49 PM"))
        self.assertFalse(sender_is_self("Henry VV Hughes DLY x  •  5:49 PM"))
        side, body = polish_message(
            "them",
            "Hi Henry, thanks for connecting. I am the founder of GrantGunner",
            "Henry VV Hughes DLY",
        )
        self.assertEqual(side, "you")
        follow, _body = polish_message(
            "them",
            "Hi Henry, floating this back up in case it got buried.",
            "Henry VV Hughes DLY",
        )
        self.assertEqual(follow, "you")
        from src.linkedin_screen import repair_thread_messages

        neil = repair_thread_messages(
            [
                ("you", "Hi Neil, floating this back up in case it got buried."),
                ("them", "Morning Toby\nHappy to look at what you have as I am involved in numerous charities"),
                ("them", "Absolutely I'm trying to get a demo video on my linkedin cause it's pretty cool, my co-founder and I basc told it the other day to apply for 10 grants"),
                ("them", "Are you free Wednesday morning or Friday any time?"),
                ("them", "Next Friday is good 10 or 11 am ? KR"),
            ],
            "Neil Mehta",
        )
        self.assertEqual([s for s, _b in neil], ["you", "them", "you", "you", "them"])
        archie = repair_thread_messages(
            [
                ("them", "FEB 6"),
                ("them", "Archie Wilding x  •  1:26 PM"),
                ("them", "Hey Rebecca,\n\nMy co-founder and I would be keen to donate a week of our time"),
            ],
            "Rebecca Denny",
        )
        self.assertEqual(archie, [("you", "Hey Rebecca,\n\nMy co-founder and I would be keen to donate a week of our time")])
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node resource-id="com.linkedin.android:id/messaging_toolbar_title" text="Henry VV Hughes DLY"/>
          <node resource-id="com.linkedin.android:id/messaging_header_time" text="TODAY"/>
          <node resource-id="com.linkedin.android:id/message_list_item_container">
            <node resource-id="com.linkedin.android:id/sender_name" text="Toby Claxton x  •  5:49 PM"/>
            <node resource-id="com.linkedin.android:id/body" text="Hi Henry, thanks for connecting."/>
          </node>
        </hierarchy>"""
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "Henry VV Hughes DLY")
        self.assertEqual(msgs[0][0], "you")
        self.assertEqual(msgs[0][1], "Hi Henry, thanks for connecting.")
        self.assertTrue(msgs[0][2].endswith("Z"))

    def test_pixel_thread_uses_toolbar_title(self):
        xml = (FIXTURES / "linkedin_pixel_thread.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "David Parry")
        self.assertTrue(any("thanks for connecting" in m[1].lower() for m in msgs))
        self.assertFalse(any("LOGOS_BUGS" in m[1] or "SmartBRG" in m[1] for m in msgs))
        from src.linkedin_screen import parse_thread_profile

        card = parse_thread_profile(xml)
        self.assertTrue(card["verified"])
        self.assertIn("SmartBRG", card["headline"])
        self.assertFalse(any("LOGOS_BUGS" in m[1] or "SmartBRG" in m[1] for m in msgs))
        from src.linkedin_screen import parse_thread_profile

        card = parse_thread_profile(xml)
        self.assertTrue(card["verified"])
        self.assertIn("SmartBRG", card["headline"])
        self.assertFalse(any(m[1] in {"Active now", "TODAY", "Thanks David", "Okay"} for m in msgs))

    def test_galaxy_inbox_rows(self):
        xml = (FIXTURES / "linkedin_galaxy_messaging.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_messaging(xml))
        hits = parse_messaging_list(xml)
        names = [h.name for h in hits]
        self.assertIn("Toby Claxton", names)
        self.assertIn("Patricia Mae Fregil", names)
        self.assertNotIn("Button", names)
        self.assertNotIn("Message", names)
        toby = next(h for h in hits if h.name == "Toby Claxton")
        self.assertTrue(toby.unread)
        self.assertEqual(toby.preview, "oh dear")
        patricia = next(h for h in hits if h.name == "Patricia Mae Fregil")
        self.assertEqual(patricia.preview, "InMail • Grow Your Hiring Pipeline, Expand Your BD Reach")
        self.assertTrue(looks_like_inmail_promo(patricia.preview))
        self.assertEqual(inmail_kind(patricia.preview), "inmail")
        self.assertFalse(should_open_row(patricia, patricia.preview))
        self.assertTrue(is_thread_chrome(patricia.preview))
        search = find_inbox_search(xml)
        self.assertIsNotNone(search)
        self.assertGreater(search[0], 200)

    def test_galaxy_feed_has_header_inbox(self):
        xml = (FIXTURES / "linkedin_galaxy_feed.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_feed(xml))
        point = find_messaging_entry(xml)
        self.assertIsNotNone(point)
        self.assertGreater(point[0], 1200)

    def test_pixel_profile_is_not_inbox(self):
        xml = (FIXTURES / "linkedin_pixel_profile.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_profile(xml))
        self.assertFalse(looks_like_messaging(xml))
        self.assertEqual(parse_messaging_list(xml), [])

    def test_pixel_share_sheet_is_not_inbox(self):
        xml = (FIXTURES / "linkedin_pixel_share.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_share_sheet(xml))
        self.assertTrue(hierarchy_is_linkedin(xml))
        self.assertFalse(looks_like_feed(xml))
        self.assertEqual(parse_messaging_list(xml), [])

    def test_pixel_article_webview_is_not_inbox(self):
        xml = (FIXTURES / "linkedin_pixel_webview.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_in_app_web(xml))
        self.assertIsNotNone(find_dismiss_point(xml))
        self.assertEqual(parse_messaging_list(xml), [])


class LinkedInPartnerRecoverTests(unittest.TestCase):
    def test_stamp_beats_sentence(self):
        from src.linkedin_store import recover_linkedin_partner

        self.assertEqual(
            recover_linkedin_partner(
                "Active now",
                [
                    ("them", "Happy to take a call in any case"),
                    ("them", "Ross Griffin x  •  6:51 PM"),
                ],
            ),
            "Ross Griffin",
        )
        self.assertIsNone(
            recover_linkedin_partner(
                "Trying",
                [("them", "Try LinkedIn Ads"), ("them", "1Password")],
            )
        )


class LinkedInProfileUrlTests(unittest.TestCase):
    def test_extract_and_search(self):
        from src.linkedin_store import extract_profile_url, public_profile_href, search_profile_url

        self.assertEqual(
            extract_profile_url("see https://www.linkedin.com/in/renate-otto please"),
            "https://www.linkedin.com/in/renate-otto",
        )
        self.assertIn("keywords=David+Parry", search_profile_url("David Parry"))
        self.assertTrue(public_profile_href("Ada").startswith("https://www.linkedin.com/search/"))
        self.assertEqual(
            public_profile_href("Ada", "https://www.linkedin.com/in/ada-lovelace"),
            "https://www.linkedin.com/in/ada-lovelace",
        )


class ChannelStoreTests(unittest.TestCase):
    def test_reconnect_keeps_linkedin_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            try:
                with phone_scope("toby"):
                    upsert_chat(conn, "Ada", preview="li", last_text="li", channel="linkedin")
                    conn.commit()
            finally:
                conn.close()
            conn = connect(path)
            try:
                with phone_scope("toby"):
                    rows = list_people(conn, channel="linkedin")
                    bff = list_people(conn, channel="bumble")
                self.assertEqual([r["name"] for r in rows], ["Ada"])
                self.assertEqual(bff, [])
            finally:
                conn.close()

    def test_same_name_different_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                with phone_scope("toby"):
                    upsert_chat(conn, "Ada", preview="bff", last_text="bff", channel="bumble")
                    upsert_chat(conn, "Ada", preview="li", last_text="li", channel="linkedin")
                    conn.commit()
                    bff = list_people(conn, channel="bumble")
                    li = list_people(conn, channel="linkedin")
                self.assertEqual([r["name"] for r in bff], ["Ada"])
                self.assertEqual([r["preview"] for r in bff], ["bff"])
                self.assertEqual([r["name"] for r in li], ["Ada"])
                self.assertEqual([r["preview"] for r in li], ["li"])
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM people WHERE IFNULL(channel,'bumble')='linkedin'").fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("SELECT count(*) FROM li_people").fetchone()[0], 1)
            finally:
                conn.close()

    def test_bumble_refuses_linkedin_chrome_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                with phone_scope("toby"):
                    self.assertEqual(upsert_chat(conn, "Claim", last_text="5:49"), 0)
                    self.assertEqual(list_people(conn, channel="bumble"), [])
            finally:
                conn.close()

    def test_purge_moves_real_thread_and_drops_ads(self):
        from src.linkedin_store import list_people as list_li, purge_linkedin_bleed_from_bumble
        from src.store import replace_thread

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                with phone_scope("archie"):
                    pid = upsert_chat(conn, "Scratch", last_text="17:49")
                    conn.execute("UPDATE people SET name = ? WHERE id = ?", ("Mobile  •  31m ago", pid))
                    replace_thread(
                        conn,
                        pid,
                        [
                            ("them", "Graham Wilsdon SYS_ICN_VERIFIED_SMALL+ICON"),
                            ("them", "Hi Graham, Founder of GrantGunner"),
                            ("them", "Write a message…"),
                            ("them", "17:49"),
                        ],
                    )
                    ad = upsert_chat(conn, "KeepMe", last_text="hey from bumble")
                    replace_thread(conn, ad, [("them", "hey from bumble")])
                    conn.execute(
                        """
                        INSERT INTO people (name, phone_id, channel, first_seen_at, last_seen_at)
                        VALUES ('Claim', 'toby', 'bumble', '2026-09-12T16:49:17Z', '2026-09-12T16:49:17Z')
                        """
                    )
                    claim_id = conn.execute("SELECT id FROM people WHERE name='Claim'").fetchone()[0]
                    conn.execute(
                        "INSERT INTO chats (person_id, status, last_text, preview, updated_at) VALUES (?,?,?,?,?)",
                        (claim_id, "needs_reply", "5:49", "offer", "2026-09-12T16:49:17Z"),
                    )
                    conn.execute(
                        "INSERT INTO messages (person_id, side, body, captured_at) VALUES (?,?,?,?)",
                        (claim_id, "them", "Get Your LinkedIn Ad Credit | LinkedIn Ads", "2026-09-12T16:49:17Z"),
                    )
                    conn.commit()
                    notes = purge_linkedin_bleed_from_bumble(conn)
                    conn.commit()
                    bff = [r["name"] for r in list_people(conn, channel="bumble")]
                    li = [r["name"] for r in list_li(conn)]
                self.assertIn("KeepMe", bff)
                self.assertNotIn("Claim", bff)
                self.assertNotIn("Mobile  •  31m ago", bff)
                self.assertIn("Graham Wilsdon", li)
                self.assertTrue(any("Graham" in n for n in notes))
            finally:
                conn.close()


class LinkedInFeedSessionTests(unittest.TestCase):
    def test_parse_galaxy_feed_person_and_skip_company(self):
        xml = (FIXTURES / "linkedin_galaxy_feed.xml").read_text(encoding="utf-8")
        posts = parse_feed_posts(xml)
        peter = next(p for p in posts if p.actor == "Peter Gamble")
        self.assertEqual(peter.kind, "person")
        self.assertFalse(peter.already)
        self.assertIn("Down Royal", peter.text)
        self.assertTrue(any(p.kind == "company" or p.actor == "Optro" for p in posts) or peter.like_y)
        from src.linkedin_session import _eligible

        self.assertTrue(_eligible(peter, people_only=True))
        company = next((p for p in posts if p.kind == "company"), None)
        if company:
            self.assertFalse(_eligible(company, people_only=True))

    def test_reaction_tray_and_reply_parse(self):
        from src.linkedin_session import parse_reaction_reply

        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node content-desc="Celebrate" clickable="true" bounds="[200,800][320,920]"/>
          <node content-desc="Insightful" clickable="true" bounds="[340,800][460,920]"/>
          <node text="Like" bounds="[80,800][180,920]"/>
        </hierarchy>"""
        tray = find_reaction_tray(xml)
        self.assertIn("celebrate", tray)
        self.assertIn("insightful", tray)
        self.assertEqual(parse_reaction_reply("Insightful please"), "insightful")
        self.assertEqual(parse_reaction_reply("nah"), "skip")
        from src.linkedin_session import in_thumb_zone, read_pause_seconds, soften_reaction

        self.assertGreaterEqual(read_pause_seconds("hi", random.Random(0)), 1.4)
        self.assertGreater(read_pause_seconds("word " * 40, random.Random(1)), read_pause_seconds("ok", random.Random(1)))
        self.assertEqual(soften_reaction("skip"), "skip")
        self.assertEqual(soften_reaction("like", random.Random(0)), "like")
        self.assertTrue(in_thumb_zone(1200, 2400))
        self.assertFalse(in_thumb_zone(80, 2400))

    def test_session_prefs_and_due(self):
        from src.jobs.linkedin_session_cron import ensure_due, mark_phone_ran, phones_due
        from src.linkedin_session import normalize_prefs

        prefs = normalize_prefs({"phone_id": "both", "daily_auto": "off", "people_only": "no"})
        self.assertEqual(prefs["phone_id"], "all")
        self.assertFalse(prefs["daily_auto"])
        self.assertFalse(prefs["people_only"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "due.json"
            with patch("src.jobs.linkedin_session_cron._state_path", return_value=path):
                due = ensure_due("toby", now=1_700_000_000.0)
                self.assertGreater(due, 1_700_000_000.0)
                self.assertEqual(phones_due(["toby"], now=1_700_000_000.0), [])
                mark_phone_ran("toby", now=1_700_000_000.0)
                self.assertEqual(phones_due(["toby"], now=1_700_000_000.0), [])


class LinkedInReplyParseTests(unittest.TestCase):
    def test_find_send_and_visible(self):
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node resource-id="com.linkedin.android:id/messaging_send_receipt_indicator" content-desc="Message sent" bounds="[10,10][20,20]"/>
          <node resource-id="com.linkedin.android:id/messaging_keyboard_send" content-desc="Send" clickable="true" bounds="[900,2200][1080,2330]"/>
          <node resource-id="com.linkedin.android:id/body" text="Coffee next Tuesday?"/>
        </hierarchy>"""
        self.assertEqual(find_send_point(xml), (990, 2265))
        self.assertTrue(message_visible(xml, "coffee next tuesday?"))
        self.assertFalse(message_visible(xml, "not this text"))

    def test_add_message_marks_waiting(self):
        from src.linkedin_store import add_message, ensure_schema, get_person, list_thread, set_archived, set_spam
        from src.linkedin_store import upsert_chat as li_upsert

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                ensure_schema(conn)
                with phone_scope("toby"):
                    pid = li_upsert(conn, "David Parry", last_from="them", last_text="Hi")
                    add_message(conn, pid, "you", "Sound — Tuesday works")
                    set_spam(conn, "David Parry", "pitch", "book a demo", phone_id="toby")
                    set_archived(conn, "David Parry", True, phone_id="toby")
                    conn.commit()
                    rows = list_thread(conn, "David Parry", phone_id="toby")
                    person = get_person(conn, "David Parry", "toby")
                self.assertEqual(rows[-1]["side"], "you")
                self.assertEqual(rows[-1]["body"], "Sound — Tuesday works")
                self.assertEqual(person["spam"], "pitch")
                self.assertEqual(person["spam_reason"], "book a demo")
                self.assertEqual(int(person["archived"]), 1)
            finally:
                conn.close()


class QueueBoardTests(unittest.TestCase):
    def test_holding_bumble_makes_linkedin_cron_wait(self):
        from src.phone_queue import job_channel, queue_board

        self.assertEqual(job_channel("fast_scan"), "bumble")
        self.assertEqual(job_channel("linkedin_backfill"), "linkedin")
        jobs = [
            {"id": 1, "kind": "fast_scan", "phone_id": "toby", "status": "running", "name": "", "text": ""},
            {"id": 2, "kind": "linkedin_scan", "phone_id": "toby", "status": "queued", "name": "", "text": ""},
        ]
        phones = [
            {"id": "toby", "label": "Toby", "device": "pixel", "online": True, "ready": True},
            {"id": "archie", "label": "Archie", "device": "galaxy", "online": False, "ready": True},
        ]
        with patch("src.phone_queue.queue_snapshot", return_value=jobs), patch(
            "src.phones.public_phones", return_value=phones
        ):
            board = queue_board()
        toby = next(p for p in board["phones"] if p["id"] == "toby")
        self.assertEqual(toby["holding"], "bumble")
        self.assertTrue(toby["linkedin_cron_wait"])
        self.assertTrue(toby["bumble_cron_wait"])
        self.assertEqual(toby["queued"][0]["kind"], "linkedin_scan")
        self.assertEqual(toby["running"]["title"], "Fast reply scan")


class LinkedInCronSkipTests(unittest.TestCase):
    def test_skips_when_other_channel_occupies(self):
        busy = [{"phone_id": "toby", "kind": "fast_scan", "status": "running"}]
        with patch("src.phone_queue.queue_snapshot", return_value=busy):
            self.assertEqual(cron_skip_reason("toby", "linkedin"), "waiting for Bumble job")
            self.assertIsNone(cron_skip_reason("archie", "linkedin"))
        li = [{"phone_id": "archie", "kind": "linkedin_scan", "status": "queued"}]
        with patch("src.phone_queue.queue_snapshot", return_value=li):
            self.assertEqual(cron_skip_reason("archie", "bumble"), "waiting for LinkedIn job")
            self.assertIsNone(cron_skip_reason("toby", "bumble"))
        reply = [{"phone_id": "toby", "kind": "linkedin_reply", "status": "running"}]
        with patch("src.phone_queue.queue_snapshot", return_value=reply):
            self.assertEqual(cron_skip_reason("toby", "bumble"), "waiting for LinkedIn job")


class LinkedInBackfillTests(unittest.TestCase):
    def test_tracked_needs_messages(self):
        from src.linkedin_store import add_message, ensure_schema, upsert_chat
        from src.linkedin_sync import tracked_thread_names

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                ensure_schema(conn)
                with phone_scope("toby"):
                    upsert_chat(conn, "List Only", last_from="them", last_text="hi")
                    pid = upsert_chat(conn, "David Parry", last_from="them", last_text="hi")
                    add_message(conn, pid, "them", "Coffee?")
                    conn.commit()
                names = tracked_thread_names(conn, "toby")
                self.assertIn("david parry", names)
                self.assertNotIn("list only", names)
            finally:
                conn.close()

    def test_evening_slot(self):
        from datetime import datetime

        from src.jobs.linkedin_backfill_cron import _LONDON, _random_due

        after = datetime(2026, 9, 12, 18, 0, tzinfo=_LONDON)
        due = datetime.fromtimestamp(_random_due(after, random.Random(1)), _LONDON)
        self.assertEqual(due.date(), after.date())
        self.assertGreaterEqual(due.hour, 21)
        self.assertLess(due.hour, 23)

    def test_busy_phone_blocks(self):
        from src.phone_queue import phone_busy_reason

        busy = [{"phone_id": "toby", "kind": "fast_scan", "status": "queued"}]
        with patch("src.phone_queue.queue_snapshot", return_value=busy):
            self.assertTrue(phone_busy_reason("toby"))
            self.assertIsNone(phone_busy_reason("archie"))


class LinkedInCronGapTests(unittest.TestCase):
    def test_four_hour_gap(self):
        from src.jobs.linkedin_cron import mark_ran, should_run

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last.txt"
            with patch("src.jobs.linkedin_cron._state_path", return_value=path):
                now = 1_700_000_000.0
                self.assertTrue(should_run(now=now))
                mark_ran(now=now)
                self.assertFalse(should_run(now=now + 60))
                self.assertTrue(should_run(now=now + 4 * 60 * 60))


class LinkedInProfileChromeTests(unittest.TestCase):
    def test_grahame_card_is_not_messages(self):
        from src.linkedin_screen import is_thread_chrome, parse_thread_profile, repair_thread_messages
        from src.linkedin_store import ensure_schema, get_person, list_thread, replace_thread, upsert_chat

        partner = "Grahame Wilsdon"
        pitch = (
            "Hi Grahame,\n\nFounder of GrantGunner – funding on autopilot. "
            "Worth a 20-minute call?\n\nArchie"
        )
        raw = [
            ("them", "Grahame Wilsdon SYSTEM_VERIFIED_SMALL+ICON"),
            (
                "them",
                "Co-founder of golingo.ai, Founder of dafty.ai, Exited founder, angel investor and AI engineer",
            ),
            ("them", "JUN 11"),
            ("them", "Archie Wilding x  •  2:01 PM"),
            ("them", pitch),
        ]
        self.assertTrue(is_thread_chrome(raw[0][1], partner))
        self.assertTrue(is_thread_chrome(raw[1][1], partner))
        self.assertTrue(is_thread_chrome("JUN 11", partner))
        self.assertFalse(is_thread_chrome(pitch, partner))
        cleaned = repair_thread_messages(raw, partner)
        self.assertEqual(cleaned, [("you", pitch)])
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                ensure_schema(conn)
                with phone_scope("archie"):
                    pid = upsert_chat(conn, partner, last_from="them", last_text=raw[1][1])
                    replace_thread(conn, pid, raw)
                    conn.commit()
                    person = get_person(conn, partner, "archie")
                    thread = list_thread(conn, partner, phone_id="archie")
                self.assertEqual([row["body"] for row in thread], [pitch])
                self.assertEqual(thread[0]["side"], "you")
                self.assertIn("golingo.ai", person["headline"])
                self.assertEqual(int(person["verified"]), 1)
            finally:
                conn.close()
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node resource-id="com.linkedin.android:id/messaging_toolbar_title" text="Grahame Wilsdon"/>
          <node resource-id="com.linkedin.android:id/participant_name" text="Grahame Wilsdon SYSTEM_VERIFIED_SMALL+ICON"/>
          <node resource-id="com.linkedin.android:id/one_on_one_occupation" text="Co-founder of golingo.ai, Founder of dafty.ai"/>
        </hierarchy>"""
        card = parse_thread_profile(xml)
        self.assertTrue(card["verified"])
        self.assertIn("golingo.ai", card["headline"])


class LinkedInSpamTests(unittest.TestCase):
    def test_parse_and_heuristic(self):
        from src.linkedin_spam import heuristic_spam, parse_spam_reply

        self.assertEqual(parse_spam_reply('{"label":"pitch","reason":"SEO"}'), ("pitch", "SEO"))
        self.assertEqual(parse_spam_reply("ok thanks")[0], "ok")
        inbound = heuristic_spam(
            [
                ("you", "Hi Mike, I am the founder of GrantGunner"),
                ("them", "We are an award-winning agency — book a demo of our platform"),
            ]
        )
        self.assertEqual(inbound[0], "pitch")
        self.assertIsNone(
            heuristic_spam(
                [
                    ("you", "Hi Neil, happy to donate a week of GrantGunner"),
                    ("them", "Next Friday is good. KR"),
                ]
            )
        )
        from src.linkedin_spam import needs_spam_check

        stamped = [
            ("them", "We are an award-winning agency — book a demo of our platform", "2026-02-06T13:26:00Z"),
            ("you", "Hi Francesco, founder of GrantGunner"),
        ]
        self.assertEqual(heuristic_spam(stamped)[0], "pitch")
        self.assertTrue(needs_spam_check("", stamped))

    def test_pixel_thread_has_more_options(self):
        xml = (FIXTURES / "linkedin_pixel_thread.xml").read_text(encoding="utf-8")
        self.assertEqual(find_more_options(xml), (891, 210))
        menu = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy><node bounds="[0,0][1080,2400]"><node text="Archive" clickable="true" bounds="[40,800][400,900]"/></node></hierarchy>"""
        self.assertEqual(find_archive_action(menu), (220, 850))

    def test_only_when_new_or_first_open(self):
        from src.linkedin_spam import heuristic_spam, inbound_from_stranger, message_spam_fp, needs_spam_check

        same = [("them", "Book a demo of our SEO platform")]
        fp = message_spam_fp(same)
        self.assertTrue(inbound_from_stranger(same))
        self.assertTrue(needs_spam_check("", same))
        self.assertFalse(needs_spam_check(fp, same))
        self.assertTrue(needs_spam_check(fp, same + [("them", "Limited time offer")]))
        self.assertFalse(needs_spam_check(fp, same + [("you", "Thanks")]))
        self.assertFalse(needs_spam_check("", [("you", "Hi, founder of GrantGunner")]))
        self.assertFalse(inbound_from_stranger([("you", "Hi, founder of GrantGunner")]))
        we_opened = [
            ("you", "Hi, founder of GrantGunner"),
            ("them", "Sounds good"),
        ]
        self.assertFalse(inbound_from_stranger(we_opened))
        self.assertFalse(needs_spam_check("", we_opened))
        they_opened = [
            ("them", "Book a demo of our SEO platform"),
            ("you", "No thanks"),
        ]
        self.assertTrue(inbound_from_stranger(they_opened))
        self.assertTrue(needs_spam_check("", they_opened))
        inmail = [("them", "InMail • Grow Your Hiring Pipeline, Expand Your BD Reach")]
        self.assertEqual(heuristic_spam(inmail)[0], "pitch")
        self.assertTrue(needs_spam_check("", inmail))
        self.assertFalse(needs_spam_check("", [("you", "Hi, founder of GrantGunner"), ("them", inmail[0][1])]))
        opened_fp = message_spam_fp(they_opened)
        self.assertFalse(needs_spam_check(opened_fp, they_opened))
        self.assertTrue(needs_spam_check(opened_fp, they_opened + [("them", "Limited time offer")]))


class LinkedInInMailTests(unittest.TestCase):
    def test_promo_kinds(self):
        self.assertEqual(inmail_kind("Sponsored • Join the INSEAD Sustainability Leadership"), "sponsored")
        self.assertTrue(looks_like_inmail_promo("LinkedIn Member • Try LinkedIn Recruiter"))
        self.assertFalse(looks_like_inmail_promo("Happy to look at a call next week. KR"))
        self.assertFalse(
            looks_like_inmail_promo(
                "At Expounder, we work with staffing and LinkedIn outreach to support hiring and business growth."
            )
        )

    def test_preview_fills_empty_thread(self):
        from src.dashboard import _thread_payload
        from src.linkedin_store import upsert_chat
        from src.store import connect

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                with phone_scope("archie"):
                    upsert_chat(
                        conn,
                        "Patricia Mae Fregil",
                        preview="InMail • Grow Your Hiring Pipeline, Expand Your BD Reach",
                        last_from="them",
                        last_text=None,
                        phone_id="archie",
                    )
                    conn.commit()
                    payload = _thread_payload(conn, "Patricia Mae Fregil", "archie", channel="linkedin")
                self.assertEqual(payload["inmail_kind"], "inmail")
                self.assertEqual(len(payload["messages"]), 1)
                self.assertTrue(payload["messages"][0]["from_preview"])
                self.assertIn("Grow Your Hiring", payload["messages"][0]["body"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
