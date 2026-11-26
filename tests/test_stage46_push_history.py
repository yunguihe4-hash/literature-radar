"""Recommendation history survives replay and ordinary card refreshes."""
import json
import tempfile
import unittest
from pathlib import Path

from literature_radar.delivery.cards import frontmatter, write_card


class PushHistoryTests(unittest.TestCase):
    def test_unique_runs_are_counted_and_unchanged_refresh_does_not_push(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = {"paper_id": "fictional-id", "title": "Fictional method study",
                   "doi": "10.0000/fictional", "event": "NEW_PAPER"}
            kwargs = {"topic_id": "fictional-topic", "topic_name": "Fictional Topic",
                      "seen_on": "2026-11-06", "attention": "MEDIUM"}
            path = write_card(root, row, run_id="run-1", **kwargs)
            write_card(root, row, run_id="run-1", **kwargs)
            write_card(root, row, run_id="run-2", create=False, **kwargs)
            meta = frontmatter(path.read_text())
            self.assertEqual(meta["push_count"], "1")
            self.assertEqual(meta["last_pushed"], "2026-11-06")
            row["event"] = "MATERIAL_UPDATE"
            write_card(root, row, run_id="run-3", create=True, **kwargs)
            meta = frontmatter(path.read_text())
            self.assertEqual(meta["push_count"], "2")
            self.assertEqual([item["event"] for item in json.loads(meta["push_history"])],
                             ["NEW_PAPER", "MATERIAL_UPDATE"])


if __name__ == "__main__":
    unittest.main()
