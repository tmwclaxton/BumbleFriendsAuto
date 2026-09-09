"""Expired-match detection and Unmatch sheet targeting."""

from __future__ import annotations

import unittest

from src.store import person_is_expired, person_is_expired_new, person_is_rematchable
from src.unmatch import find_label_contains, find_labeled_point
from src.chats import list_new_friends

OVERFLOW_XML = """<?xml version="1.0"?>
<hierarchy>
  <node clickable="true" content-desc="Unmatch" bounds="[42,2040][1038,2166]">
    <node resource-id="com.bumblebff.app:id/actionSheet_button_title" text="Unmatch" clickable="false" bounds="[84,2074][996,2131]"/>
  </node>
  <node clickable="true" content-desc="Unmatch and report" bounds="[42,2169][1038,2295]">
    <node resource-id="com.bumblebff.app:id/actionSheet_button_title" text="Unmatch and report" clickable="false" bounds="[84,2203][996,2260]"/>
  </node>
</hierarchy>
"""

TOOLBAR_XML = """<?xml version="1.0"?>
<hierarchy>
  <node resource-id="com.bumblebff.app:id/chatToolbar_overflow" content-desc="Chat options" clickable="true" bounds="[953,146][1080,272]"/>
</hierarchy>
"""


class ExpiredDetectTests(unittest.TestCase):
    def test_status(self):
        self.assertTrue(person_is_expired({"status": "expired"}))
        self.assertFalse(person_is_expired({"status": "needs_reply", "last_text": "hey"}))

    def test_preview_phrase(self):
        self.assertTrue(person_is_expired({"status": "unknown", "last_text": "Conversation expired yesterday"}))
        self.assertFalse(person_is_expired({"status": "waiting", "last_text": "Sorry I expired my gym pass"}))

    def test_expired_new(self):
        self.assertTrue(person_is_expired_new({"status": "expired", "message_until": "2020-01-01T00:00:00Z"}))
        self.assertFalse(
            person_is_expired_new({"status": "expired", "last_text": "Conversation expired yesterday"})
        )

    def test_rematchable_skips_stale_conversation_expired(self):
        self.assertTrue(person_is_rematchable({"status": "expired", "last_text": "Conversation expired yesterday"}))
        self.assertTrue(person_is_rematchable({"status": "expired", "last_text": "Conversation expired 2 hours ago"}))
        self.assertFalse(person_is_rematchable({"status": "expired", "last_text": "Conversation expired 3 days ago"}))
        self.assertFalse(person_is_rematchable({"status": "expired", "preview": "Conversation expired 2 weeks ago"}))


REMATCH_XML = """<?xml version="1.0"?>
<hierarchy>
  <node clickable="true" content-desc="Rematch" bounds="[120,1800][960,1940]">
    <node resource-id="com.bumblebff.app:id/actionSheet_button_title" text="Rematch" clickable="false" bounds="[160,1840][920,1900]"/>
  </node>
</hierarchy>
"""

EXPIRED_STRIP_XML = """<?xml version="1.0"?>
<hierarchy>
  <node resource-id="com.bumblebff.app:id/connectionItem_ringView" clickable="true"
        content-desc="Callum, BFF, expired match" bounds="[40,400][200,560]"/>
  <node resource-id="com.bumblebff.app:id/connectionItem_ringView" clickable="true"
        content-desc="Abhinav, BFF, match" bounds="[220,400][380,560]"/>
</hierarchy>
"""


class SheetTests(unittest.TestCase):
    def test_unmatch_not_report(self):
        point = find_labeled_point(OVERFLOW_XML, texts=("unmatch",), descs=("unmatch",))
        self.assertEqual(point, ((42 + 1038) // 2, (2040 + 2166) // 2))

    def test_overflow(self):
        point = find_labeled_point(TOOLBAR_XML, descs=("chat options",), rids=("chatToolbar_overflow",))
        self.assertIsNotNone(point)

    def test_rematch_label(self):
        point = find_label_contains(REMATCH_XML, ("rematch", "extend match"))
        self.assertEqual(point, ((120 + 960) // 2, (1800 + 1940) // 2))

    def test_expired_strip_circles(self):
        friends = list_new_friends(EXPIRED_STRIP_XML)
        self.assertEqual([f.name for f in friends], ["Callum", "Abhinav"])
        self.assertTrue(friends[0].expired)
        self.assertFalse(friends[1].expired)

    def test_expired_overlay(self):
        from src.chats import is_expired_rematch_overlay

        xml = """<?xml version="1.0"?>
<hierarchy>
  <node text="Fauzan, 27"/>
  <node text="This match has expired"/>
  <node resource-id="com.bumblebff.app:id/myProfilePreview_leftButton" text="Remove" clickable="true" bounds="[42,2169][535,2295]"/>
  <node resource-id="com.bumblebff.app:id/myProfilePreview_rightButton" text="Rematch" clickable="true" bounds="[545,2169][1038,2295]"/>
</hierarchy>
"""
        self.assertTrue(is_expired_rematch_overlay(xml))
        point = find_labeled_point(
            xml, texts=("rematch",), rids=("myProfilePreview_rightButton",)
        )
        self.assertEqual(point, ((545 + 1038) // 2, (2169 + 2295) // 2))


class RematchPhotoTests(unittest.TestCase):
    def test_force_overwrites_existing_avatar(self):
        from unittest.mock import patch

        from src.sync_chats import _maybe_grab_profile_photo

        device = object()
        with patch("src.photos.photo_exists", return_value=True), patch(
            "src.photos.capture_profile_photo", return_value=True
        ) as capture:
            _maybe_grab_profile_photo(device, "Sam")
            capture.assert_not_called()
            _maybe_grab_profile_photo(device, "Sam", force=True)
            capture.assert_called_once_with(device, "Sam", force=True)


if __name__ == "__main__":
    unittest.main()
