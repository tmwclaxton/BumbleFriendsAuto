import unittest
from pathlib import Path

from src.instagram_feed import (
    _session_payload,
    decide_post,
    default_prefs,
    looks_like_page_handle,
    parse_feed_vision,
)
from src.instagram_screen import (
    find_back_button,
    find_following_chip,
    find_home_tab,
    looks_like_following_feed,
    looks_like_nested_viewer,
    parse_age_hours,
    parse_feed_posts,
    parse_unseen_post,
    parse_visible_post,
)


FIXTURE = Path(__file__).with_name("fixtures") / "instagram_following_feed.xml"
SCROLLED = Path(__file__).with_name("fixtures") / "instagram_following_scrolled.xml"
REEL = Path(__file__).with_name("fixtures") / "instagram_following_reel.xml"


class InstagramFeedParseTests(unittest.TestCase):
    def test_age_under_a_week(self):
        self.assertEqual(parse_age_hours("just now"), 0.0)
        self.assertAlmostEqual(parse_age_hours("45m") or 0, 0.75)
        self.assertEqual(parse_age_hours("3h"), 3.0)
        self.assertEqual(parse_age_hours("6d"), 144.0)
        self.assertLess(parse_age_hours("6d") or 0, 168)
        self.assertGreaterEqual(parse_age_hours("1w") or 0, 168)
        self.assertGreaterEqual(parse_age_hours("2w") or 0, 168)

    def test_fixture_following_post(self):
        xml = FIXTURE.read_text(encoding="utf-8")
        self.assertTrue(looks_like_following_feed(xml))
        self.assertIsNotNone(find_following_chip(xml))
        post = parse_visible_post(xml)
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.handle, "maya.lane")
        self.assertEqual(post.age_hours, 3.0)
        self.assertTrue(post.recent)
        self.assertFalse(post.already_liked)
        self.assertIsNotNone(post.like_xy)
        self.assertEqual(len(parse_feed_posts(xml)), 1)

    def test_scrolled_following_header_and_like(self):
        xml = SCROLLED.read_text(encoding="utf-8")
        self.assertTrue(looks_like_following_feed(xml))
        post = parse_visible_post(xml)
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.handle, "thebabylonbee")
        self.assertAlmostEqual(post.age_hours or 0, 10 / 60)
        self.assertTrue(post.recent)
        self.assertFalse(post.already_liked)

    def test_vision_and_decision(self):
        parsed = parse_feed_vision("kind=person; person=yes")
        self.assertEqual(parsed, {"kind": "person", "person": True})
        xml = FIXTURE.read_text(encoding="utf-8")
        post = parse_visible_post(xml)
        assert post is not None
        action, reason = decide_post(post, parsed, {"require_person": True, "skip_memes": True, "max_age_hours": 168})
        self.assertEqual(action, "like")
        self.assertIn("person", reason)
        meme_action, _ = decide_post(post, {"kind": "meme", "person": False}, {"require_person": True, "skip_memes": True, "max_age_hours": 168})
        self.assertEqual(meme_action, "skip")
        old = post
        old.age_hours = 200
        old.age_label = "2w"
        old_action, _ = decide_post(old, parsed, {"require_person": True, "max_age_hours": 168})
        self.assertEqual(old_action, "skip")
        reel = parse_visible_post(REEL.read_text(encoding="utf-8"))
        assert reel is not None
        reel_action, reel_reason = decide_post(reel, {"kind": "person", "person": True}, {"require_person": True, "max_age_hours": 168})
        self.assertEqual(reel_action, "like")
        self.assertIn("person", reel_reason)
        reel.age_hours = None
        reel.age_label = ""
        missing_age, why = decide_post(reel, {"kind": "person", "person": True}, {"require_person": True, "max_age_hours": 168})
        self.assertEqual(missing_age, "like")
        self.assertNotIn("unknown", why)

    def test_back_button_on_trending_reel(self):
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node bounds="[0,80][120,200]" content-desc="Back" resource-id="com.instagram.android:id/action_bar_button_back" class="android.widget.ImageView"/>
          <node bounds="[200,80][500,200]" content-desc="Trending" text="Trending"/>
        </hierarchy>"""
        self.assertEqual(find_back_button(xml), (60, 140))
        self.assertTrue(looks_like_nested_viewer(xml))
        self.assertIsNone(find_home_tab(
            """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
            <hierarchy rotation="0">
              <node bounds="[200,300][900,400]" text="Hey Daddy (Daddy's Home) (feat. Plies)"/>
            </hierarchy>"""
        ))

    def test_following_reel_is_a_post(self):
        xml = REEL.read_text(encoding="utf-8")
        self.assertTrue(looks_like_following_feed(xml))
        post = parse_visible_post(xml)
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.handle, "saveloughneagh")
        self.assertFalse(post.sponsored)
        self.assertIsNotNone(post.like_xy)

    def test_unseen_prefers_next_handle(self):
        xml = FIXTURE.read_text(encoding="utf-8")
        first = parse_visible_post(xml)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNone(parse_unseen_post(xml, {first.fingerprint}))
        nxt = parse_unseen_post(xml, set())
        self.assertEqual(nxt.handle, first.handle)

    def test_session_lists_accounts(self):
        payload = _session_payload(
            "2026-09-13T12:00:00Z",
            True,
            "",
            1,
            1,
            [
                {"handle": "maya.lane", "action": "like", "kind": "person", "age": "3h", "reason": "person · recent"},
                {"handle": "thebabylonbee", "action": "skip", "kind": "meme", "age": "10m", "reason": "meme"},
            ],
            80,
        )
        self.assertEqual(payload["accounts"], 2)
        self.assertEqual(payload["liked_handles"], ["maya.lane"])
        self.assertEqual(payload["friend_handles"], ["maya.lane"])
        self.assertIn("2 accounts", payload["summary"])
        self.assertEqual(default_prefs()["max_posts"], 80)
        self.assertTrue(looks_like_page_handle("thebabylonbee"))
        self.assertTrue(looks_like_page_handle("base44.app"))
        self.assertFalse(looks_like_page_handle("maya.lane"))

    def test_home_tab_must_be_bottom_chrome(self):
        mid_home = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node bounds="[40,280][220,360]" content-desc="Home" resource-id="com.instagram.android:id/feed_tab"/>
          <node bounds="[200,300][900,400]" text="Home on the range"/>
        </hierarchy>"""
        self.assertIsNone(find_home_tab(mid_home))
        bottom = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node bounds="[20,1720][200,1880]" content-desc="Home" resource-id="com.instagram.android:id/feed_tab"/>
        </hierarchy>"""
        self.assertEqual(find_home_tab(bottom), (110, 1800))
        song = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy rotation="0">
          <node bounds="[80,320][980,420]" text="Hey Daddy (Daddy's Home) (feat. Plies)"/>
          <node bounds="[40,80][180,160]" content-desc="Back"/>
        </hierarchy>"""
        self.assertIsNone(find_home_tab(song))

    def test_nested_viewer_vs_following_feed(self):
        self.assertTrue(
            looks_like_nested_viewer(
                """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
                <hierarchy><node text="Use audio"/><node text="Trending"/></hierarchy>"""
            )
        )
        self.assertTrue(
            looks_like_nested_viewer(
                """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
                <hierarchy><node content-desc="Save audio"/></hierarchy>"""
            )
        )
        self.assertFalse(looks_like_nested_viewer(FIXTURE.read_text(encoding="utf-8")))
        self.assertFalse(looks_like_nested_viewer(SCROLLED.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
