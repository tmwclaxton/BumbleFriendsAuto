import unittest
from pathlib import Path
from unittest.mock import patch

from src.instagram_prune import (
    classify_account,
    find_following_stat,
    looks_like_following_list,
    looks_like_own_profile,
    parse_follower_count,
    parse_following_rows,
)
from src.phone_queue import cron_skip_reason, job_channel, job_title

FIXTURES = Path(__file__).parent / "fixtures"


FOLLOWING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" package="com.instagram.android">
    <node text="tobyc1laxton" bounds="[200,40][500,90]"/>
    <node text="Following" bounds="[400,80][680,160]"/>
    <node text="Search" bounds="[80,180][1000,260]"/>
    <node text="letsgosocialuk" resource-id="com.instagram.android:id/row_search_user_username" bounds="[160,400][620,460]"/>
    <node text="Let's Go Social" resource-id="com.instagram.android:id/row_search_user_fullname" bounds="[160,460][620,510]"/>
    <node text="Following" bounds="[800,410][1020,500]"/>
    <node text="dailymemesuk" resource-id="com.instagram.android:id/row_search_user_username" bounds="[160,560][620,620]"/>
    <node text="Daily Memes" resource-id="com.instagram.android:id/row_search_user_fullname" bounds="[160,620][620,670]"/>
    <node text="Following" bounds="[800,570][1020,660]"/>
    <node text="Profile" bounds="[860,2280][1040,2380]"/>
  </node>
</hierarchy>
"""


class ClassifyTests(unittest.TestCase):
    def test_keep_lgs_and_people(self):
        self.assertEqual(classify_account("letsgosocialuk", "LGS")[0], "keep")
        self.assertEqual(classify_account("grantgunner")[0], "keep")
        action, _ = classify_account("jane.perry", "Jane Perry")
        self.assertEqual(action, "keep")

    def test_propose_meme_and_brand(self):
        self.assertEqual(classify_account("dailymemesuk", "Daily Memes")[0], "propose")
        self.assertEqual(classify_account("arsenal", "Arsenal FC", bio="Official")[0], "propose")
        self.assertEqual(
            classify_account("bbcnews", "BBC News", followers=12_000_000)[0],
            "propose",
        )
        self.assertEqual(classify_account("thefeverdreamsuk", "The Fever Dreams")[0], "propose")

    def test_skip_when_unsure(self):
        action, reason = classify_account("xxy99zz", "ok")
        self.assertEqual(action, "skip")
        self.assertIn("unsure", reason)

    def test_crm_keep(self):
        action, _ = classify_account(
            "somehandle",
            "Alex Friend",
            known_handles={"somehandle"},
        )
        self.assertEqual(action, "keep")


class ParseTests(unittest.TestCase):
    def test_following_rows(self):
        rows = parse_following_rows(FOLLOWING_XML)
        handles = [r.handle for r in rows]
        self.assertIn("letsgosocialuk", handles)
        self.assertIn("dailymemesuk", handles)
        self.assertTrue(looks_like_following_list(FOLLOWING_XML))

    def test_follower_count(self):
        self.assertEqual(parse_follower_count("128K followers"), 128000)
        self.assertEqual(parse_follower_count("1.2M Followers"), 1_200_000)

    def test_pixel_profile_is_not_following_list(self):
        xml = (FIXTURES / "instagram_pixel_profile.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_own_profile(xml))
        self.assertFalse(looks_like_following_list(xml))
        self.assertEqual(parse_following_rows(xml), [])
        point = find_following_stat(xml)
        self.assertIsNotNone(point)
        self.assertGreater(point[0], 700)
        self.assertLess(point[1], 600)


class QueueOccupyTests(unittest.TestCase):
    def test_instagram_occupies_both_crons(self):
        self.assertEqual(job_channel("instagram_prune"), "bumble")
        self.assertEqual(job_title({"kind": "instagram_prune"}), "Instagram prune")
        busy = [{"phone_id": "toby", "kind": "instagram_prune", "status": "queued"}]
        with patch("src.phone_queue.queue_snapshot", return_value=busy):
            self.assertEqual(cron_skip_reason("toby", "linkedin"), "waiting for Instagram job")
            self.assertEqual(cron_skip_reason("toby", "bumble"), "waiting for Instagram job")
            self.assertIsNone(cron_skip_reason("archie", "linkedin"))


if __name__ == "__main__":
    unittest.main()
