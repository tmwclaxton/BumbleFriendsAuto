import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hinge_draft import contact_stage, default_prompt, load_prompt, save_prompt
from src.hinge_screen import (
    PACKAGE,
    classify_screen,
    find_like_photo,
    find_nav_point,
    find_skip,
    merge_profiles,
    parse_match_list,
    parse_open_thread,
    parse_profile,
    profile_photo_boxes,
)
from src.hinge_store import (
    ensure_schema,
    get_person,
    list_people,
    list_prompts,
    people_payload,
    queue_draft,
    replace_prompts,
    replace_thread,
    set_archived,
    set_draft,
    set_ethnicity,
    thread_payload,
    upsert_chat,
)
from src.hinge_swipe import (
    _should_like,
    default_prefs,
    live_account_id,
    normalize_prefs,
    pixel_blocked,
    prefs_payload,
    score_attractiveness,
)
from src.phones import phone_scope
from src.store import connect


FIXTURES = Path(__file__).parent / "fixtures"


class _JsonRequest:
    method = "POST"

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class HingeParseTests(unittest.TestCase):
    def test_matches_list(self):
        xml = (FIXTURES / "hinge_matches.xml").read_text(encoding="utf-8")
        self.assertEqual(classify_screen(xml), "matches_list")
        hits = parse_match_list(xml)
        names = [h.name for h in hits]
        self.assertIn("Sara", names)
        self.assertIn("Maya", names)
        self.assertIn("Priya", names)
        sara = next(h for h in hits if h.name == "Sara")
        self.assertTrue(sara.is_new)
        self.assertEqual(sara.section, "your_turn")
        priya = next(h for h in hits if h.name == "Priya")
        self.assertEqual(priya.section, "their_turn")

    def test_compose_matches_ignores_previews(self):
        xml = (FIXTURES / "hinge_matches_compose.xml").read_text(encoding="utf-8")
        self.assertEqual(classify_screen(xml), "matches_list")
        names = [h.name for h in parse_match_list(xml)]
        self.assertEqual(names, ["Tia", "rosie", "elizabeth", "Anastasia", "Soyeon"])
        tia = next(h for h in parse_match_list(xml) if h.name == "Tia")
        self.assertEqual(tia.preview, "Do it!!")
        self.assertIsNotNone(find_nav_point(xml, "Discover"))
        self.assertIsNotNone(find_nav_point(xml, "Matches"))

    def test_open_thread(self):
        xml = (FIXTURES / "hinge_thread.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "Sara")
        self.assertEqual(msgs[0][0], "you")
        self.assertEqual(msgs[-1][0], "them")
        self.assertIn("Kimchi", msgs[-1][1])

    def test_rosie_photo_comment_is_oldest_first(self):
        xml = (FIXTURES / "hinge_thread_rosie.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name.lower(), "rosie")
        bodies = [body for _side, body in msgs]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(any("Cutie" in body for body in bodies))
        self.assertTrue(any("work do you do" in body.lower() for body in bodies))
        cutie = next(i for i, (_side, body) in enumerate(msgs) if "Cutie" in body)
        reply = next(i for i, (_side, body) in enumerate(msgs) if "work do you do" in body.lower())
        self.assertLess(cutie, reply)
        self.assertEqual(msgs[cutie][0], "you")
        self.assertEqual(msgs[reply][0], "them")
        self.assertFalse(any("notification" in body.lower() for body in bodies))

    def test_compose_thread_prompt_and_reply(self):
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="co.hinge.app" bounds="[0,0][1440,2560]">
    <node text="Tia" bounds="[200,133][400,219]"/>
    <node text="Chat" bounds="[56,310][664,370]"/>
    <node text="Give me travel tips for" bounds="[301,934][807,994]"/>
    <node text="my dream winter in Northern Europe" bounds="[301,1029][1300,1432]"/>
    <node text="Only if I can drag you to Ireland as well :P" bounds="[287,1621][1314,1777]"/>
    <node content-desc="Tia: Do it!!." bounds="[203,1991][441,2143]"/>
    <node text="Send a message" resource-id="co.hinge.app:id/messageComposition" bounds="[42,2350][1202,2518]"/>
  </node>
</hierarchy>
"""
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "Tia")
        bodies = [m[1] for m in msgs]
        self.assertTrue(any("travel tips" in b.lower() for b in bodies))
        self.assertTrue(any("Ireland" in b for b in bodies))
        self.assertTrue(any("Do it" in b for b in bodies))
        ireland = next(m for m in msgs if "Ireland" in m[1])
        self.assertEqual(ireland[0], "you")

    def test_real_profile_fixture(self):
        xml = (FIXTURES / "hinge_profile.xml").read_text(encoding="utf-8")
        self.assertIn(PACKAGE, xml)
        self.assertEqual(classify_screen(xml), "match_profile")
        profile = parse_profile(xml)
        self.assertEqual(profile.name, "Sara")
        self.assertTrue(profile.verified)
        self.assertEqual(profile.age, 23)
        self.assertEqual(profile.job, "Fashion production")
        self.assertTrue(profile.prompts)
        self.assertIn("stay sane", profile.prompts[0][1].lower())

    def test_discover_like_and_skip(self):
        xml = (FIXTURES / "hinge_discover.xml").read_text(encoding="utf-8")
        self.assertEqual(classify_screen(xml), "discover")
        self.assertIsNotNone(find_like_photo(xml))
        self.assertIsNotNone(find_skip(xml))
        profile = parse_profile(xml)
        self.assertTrue(any("life goal" in q.lower() for q, _a in profile.prompts))

    def test_skip_without_trailing_space(self):
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="co.hinge.app" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node content-desc="Skip" clickable="true" bounds="[80,1900][220,2040]"/>
    <node content-desc="Like" clickable="true" bounds="[860,1900][1000,2040]"/>
    <node text="the way to win me over is" bounds="[80,1400][900,1480]"/>
    <node text="simple just to be" bounds="[80,1490][900,1560]"/>
  </node>
</hierarchy>
"""
        self.assertEqual(classify_screen(xml), "discover")
        self.assertIsNotNone(find_skip(xml))
        self.assertIsNotNone(find_like_photo(xml))
        profile = parse_profile(xml)
        self.assertTrue(profile.prompts)
        self.assertIn("win me over", profile.prompts[0][0].lower())

    def test_profile_photo_boxes_skip_stubs(self):
        xml = (FIXTURES / "hinge_profile.xml").read_text(encoding="utf-8")
        profile = parse_profile(xml)
        self.assertGreaterEqual(len(profile.prompts), 1)
        self.assertEqual(profile.extras.get("languages spoken"), "English, Persian")
        live = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="co.hinge.app" bounds="[0,0][1080,2400]">
    <node content-desc="Tia, Verified" bounds="[200,100][500,180]"/>
    <node content-desc="Prompt: The way to win me over is. Answer: be gentle" bounds="[70,400][1000,900]"/>
    <node content-desc="College or university" bounds="[80,1000][200,1080]"/>
    <node text="Nottingham Trent" bounds="[220,1008][700,1070]"/>
    <node content-desc="Home town" bounds="[80,1120][200,1200]"/>
    <node text="Vietnam" bounds="[220,1128][500,1190]"/>
    <node content-desc="Dating Intentions" bounds="[80,1240][200,1320]"/>
    <node text="Long-term relationship" bounds="[220,1248][700,1310]"/>
  </node>
</hierarchy>
"""
        live_profile = parse_profile(live)
        self.assertEqual(live_profile.school, "Nottingham Trent")
        self.assertEqual(live_profile.location, "Vietnam")
        self.assertEqual(live_profile.extras.get("looking for"), "Long-term relationship")
        self.assertFalse(profile_photo_boxes(xml))
        merged = merge_profiles(
            profile,
            parse_profile(
                """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="co.hinge.app" bounds="[0,0][1080,2400]">
    <node content-desc="Prompt: Dating me is like. Answer: a late bus" bounds="[70,600][1000,1200]"/>
    <node content-desc="Sara’s photo" bounds="[80,500][1000,1400]"/>
  </node>
</hierarchy>
"""
            ),
        )
        self.assertEqual(len(merged.prompts), 2)
        self.assertTrue(any("late bus" in a for _q, a in merged.prompts))
        xml2 = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="co.hinge.app" bounds="[0,0][1080,2400]">
    <node content-desc="Maya’s photo" bounds="[80,500][1000,1400]"/>
  </node>
</hierarchy>
"""
        boxes = profile_photo_boxes(xml2)
        self.assertEqual(len(boxes), 1)
        self.assertGreaterEqual(boxes[0].y2 - boxes[0].y1, 180)


class HingeStoreTests(unittest.TestCase):
    def test_schema_upsert_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            ensure_schema(conn)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in (
                "hinge_people",
                "hinge_photos",
                "hinge_prompts",
                "hinge_chats",
                "hinge_messages",
                "hinge_settings",
            ):
                self.assertIn(name, tables)
            with phone_scope("archie"):
                pid = upsert_chat(
                    conn,
                    "Sara",
                    preview="Start the chat",
                    last_from="them",
                    last_text="Start the chat",
                    phone_id="archie",
                    age=23,
                    job="Fashion production",
                )
                replace_prompts(conn, pid, [("You should not go out with me if", "U wanna stay sane")])
                replace_thread(conn, pid, [("you", "hey"), ("them", "kimchi sundae")])
                conn.commit()
                people = list_people(conn, "archie")
                self.assertEqual(people[0]["name"], "Sara")
                self.assertEqual(people[0]["job"], "Fashion production")
                self.assertEqual(people[0]["status"], "needs_reply")
                prompts = list_prompts(conn, pid)
                self.assertEqual(prompts[0]["answer"], "U wanna stay sane")
                payload = people_payload(conn)
                self.assertNotIn("prompts", payload[0])
                self.assertEqual(payload[0]["prompt_count"], 1)
                thread = thread_payload(conn, "Sara", "archie")
                self.assertEqual(len(thread["prompts"]), 1)
            conn.close()


class HingePrefsAndDraftTests(unittest.TestCase):
    def test_prefs_validation(self):
        raw = normalize_prefs(
            {
                "age_min": 40,
                "age_max": 22,
                "like_percent": 140,
                "dealbreakers": "kids, smoking",
                "phone_id": "all",
            }
        )
        self.assertEqual(raw["phone_id"], "toby")
        self.assertEqual(raw["age_min"], 40)
        self.assertEqual(raw["age_max"], 40)
        self.assertEqual(raw["like_percent"], 100)
        self.assertEqual(raw["dealbreakers"], ["kids", "smoking"])
        self.assertEqual(raw["attractiveness_min"], 8)
        self.assertEqual(default_prefs()["phone_id"], "toby")
        self.assertTrue(default_prefs()["human_pacing"])
        self.assertEqual(live_account_id("archie"), "toby")
        self.assertIsNone(pixel_blocked("toby"))
        self.assertIn("Pixel", pixel_blocked("toby", "29081FDH200GZ8") or "")
        payload = prefs_payload(raw)
        self.assertTrue(payload["ethnicity_choices"])
        self.assertIn("attractiveness_min", payload)

    def test_prompt_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hinge_prompt.json"
            with patch("src.hinge_draft.PROMPT_PATH", path):
                saved = save_prompt("system x", "user {name}")
                self.assertEqual(load_prompt(), saved)
                self.assertIn("warm them up", default_prompt()["system"].lower())

    def test_contact_stage(self):
        self.assertIn("too_early", contact_stage([]))
        self.assertIn("already_done", contact_stage([("them", "add me on insta @maya")]))
        thread = [
            ("you", "weird menu item?"),
            ("them", "kimchi sundae"),
            ("you", "brave"),
            ("them", "would again"),
            ("you", "what next"),
            ("them", "late night noodles"),
        ]
        self.assertTrue(contact_stage(thread).startswith("good") or contact_stage(thread).startswith("maybe"))

    def test_swipe_respects_race_and_attractiveness(self):
        prefs = normalize_prefs(
            {
                "ethnicity_exclude": ["black"],
                "attractiveness_min": 8,
                "like_percent": 100,
                "max_likes_session": 10,
            }
        )
        score, why = score_attractiveness()
        self.assertLess(score, prefs["attractiveness_min"])
        self.assertIn("stub", why)
        like, reason = _should_like(prefs, "hello", likes=0, swipes=0, extras={"ethnicity": "Black"})
        self.assertFalse(like)
        self.assertIn("excluded", reason)
        like, reason = _should_like(prefs, "hello", likes=0, swipes=0, extras={"ethnicity": "white"})
        self.assertFalse(like)
        self.assertIn("attractiveness", reason)

    def test_draft_api_smoke(self):
        from src.server import api_hinge_draft_generate

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            ensure_schema(conn)
            with phone_scope("toby"):
                pid = upsert_chat(conn, "Sara", phone_id="toby")
                conn.execute(
                    "INSERT INTO hinge_messages(person_id, side, body, captured_at) VALUES (?, 'them', 'Hello', '2026-09-12T00:00:00Z')",
                    (pid,),
                )
                conn.commit()
            conn.close()
            with patch("src.server.load_config", return_value={"db_path": str(path)}):
                response = asyncio.run(
                    api_hinge_draft_generate(_JsonRequest({"name": "Sara", "phone_id": "toby", "text": "keep it short"}))
                )
            self.assertEqual(response.status_code, 200)
            conn = connect(path)
            with phone_scope("toby"):
                row = get_person(conn, "Sara", "toby")
            self.assertEqual(row["draft_status"], "queued")
            self.assertTrue(row["draft_pending_fp"])
            conn.close()

    def test_draft_persist_and_switch(self):
        from src.server import api_hinge_draft

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            ensure_schema(conn)
            with phone_scope("toby"):
                upsert_chat(conn, "Tia", phone_id="toby")
                upsert_chat(conn, "rosie", phone_id="toby")
                set_draft(conn, "Tia", "hello tia", phone_id="toby")
                conn.commit()
            conn.close()
            with patch("src.server.load_config", return_value={"db_path": str(path)}):
                response = asyncio.run(
                    api_hinge_draft(_JsonRequest({"name": "rosie", "phone_id": "toby", "text": "hello rosie"}))
                )
            self.assertEqual(response.status_code, 200)
            conn = connect(path)
            with phone_scope("toby"):
                self.assertEqual(get_person(conn, "Tia", "toby")["draft"], "hello tia")
                self.assertEqual(get_person(conn, "rosie", "toby")["draft"], "hello rosie")
                self.assertEqual(thread_payload(conn, "Tia", "toby")["person"]["draft"], "hello tia")
                self.assertEqual(thread_payload(conn, "rosie", "toby")["person"]["draft"], "hello rosie")
                self.assertTrue(set_archived(conn, "Tia", True, phone_id="toby"))
                self.assertTrue(get_person(conn, "Tia", "toby")["archived"])
                self.assertTrue(set_ethnicity(conn, "Tia", "Southeast Asian", phone_id="toby"))
                self.assertEqual(get_person(conn, "Tia", "toby")["ethnicity"], "southeast_asian")
            conn.close()

    def test_job_channel(self):
        from src.phone_queue import cron_skip_reason, job_channel, job_title

        self.assertEqual(job_channel("hinge_swipe"), "hinge")
        self.assertEqual(job_title({"kind": "hinge_reply", "name": "Sara"}), "Hinge reply · Sara")
        with patch("src.phone_queue.queue_snapshot", return_value=[{"phone_id": "archie", "status": "running", "kind": "instagram_prune"}]):
            self.assertEqual(cron_skip_reason("archie", "hinge"), "waiting for Instagram job")


if __name__ == "__main__":
    unittest.main()
