"""A broad backcheck processes pending events once without duplicate alerts."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from literature_radar.database.academic import AcademicStore


class EventBatchTests(unittest.TestCase):
    def test_large_pending_set_is_processed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "academic.sqlite3"
            store = AcademicStore(path)
            topic = "fictional-large-topic"
            now = "2026-11-06T12:00:00+00:00"
            with closing(sqlite3.connect(path)) as db, db:
                for number in range(1000):
                    paper_id = f"fictional-paper-{number:04d}"
                    db.execute("INSERT INTO papers_v2 VALUES (?,?,?,?)",
                               (paper_id, "{}", now, now))
                    db.execute("INSERT INTO paper_events VALUES (?,?,?,?,?,?,NULL)",
                               (topic, paper_id, "version-1", "NEW_PAPER",
                                json.dumps({"eligible": True}), now))
            self.assertEqual(store.queue_pending(topic, ["desktop"],
                             allowed_paper_ids={"fictional-paper-0000"},
                             once_per_topic_paper=True), 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_events WHERE processed_at IS NULL").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            self.assertEqual(store.queue_pending(topic, ["desktop"],
                             allowed_paper_ids={"fictional-paper-0000"},
                             once_per_topic_paper=True), 0)


if __name__ == "__main__":
    unittest.main()
