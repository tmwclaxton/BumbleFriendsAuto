import tempfile
import unittest
from pathlib import Path

from src.store import (
    connect,
    dismiss_needs_reply,
    enqueue_auto_draft_if_needed,
    list_needs_reply,
    list_people,
    replace_thread,
    set_archived,
    upsert_chat,
)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_archive_survives_a_new_reply(self) -> None:
        upsert_chat(self.conn, "Max", last_from="them", last_text="Hey", badge="Your turn")
        self.assertTrue(set_archived(self.conn, "Max", True))
        pid = upsert_chat(
            self.conn,
            "Max",
            last_from="them",
            last_text="Can I still come?",
            badge="Your turn",
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT archived, status FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(int(row["archived"]), 1)
        self.assertEqual(row["status"], "needs_reply")
        people = {p["name"]: p for p in list_people(self.conn)}
        self.assertTrue(people["Max"]["archived"])
        self.assertEqual(list_needs_reply(self.conn), [])
        self.assertFalse(enqueue_auto_draft_if_needed(self.conn, pid, [("them", "Can I still come?")]))

    def test_dismiss_resurfaces_on_a_new_reply(self) -> None:
        upsert_chat(self.conn, "Pete", last_from="them", last_text="Cool", badge="Your turn")
        replace_thread(self.conn, upsert_chat(self.conn, "Pete"), [("them", "Cool")])
        self.assertTrue(dismiss_needs_reply(self.conn, "Pete"))
        upsert_chat(self.conn, "Pete", last_from="them", last_text="New plan?", badge="Your turn")
        row = next(p for p in list_people(self.conn) if p["name"] == "Pete")
        self.assertEqual(row["status"], "needs_reply")
        self.assertFalse(row["archived"])
        self.assertEqual(len(list_needs_reply(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
