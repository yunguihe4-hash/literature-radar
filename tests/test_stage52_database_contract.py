"""Stage 5.2 additive migration against disposable v2-shaped SQLite data."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature_radar.database.academic import AcademicStore


class DatabaseContractTests(unittest.TestCase):
    def test_additive_specialty_schema_preserves_existing_v2_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "academic.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE papers_v2 (paper_id TEXT PRIMARY KEY, "
                                   "payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, "
                                   "last_seen_at TEXT NOT NULL)")
                connection.execute("INSERT INTO papers_v2 VALUES ('existing','{}','2026-01-01','2026-01-01')")
                connection.execute("PRAGMA user_version=2")
            AcademicStore(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT payload FROM papers_v2 "
                                                    "WHERE paper_id='existing'").fetchone()[0], "{}")
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"specialty_topic_papers", "research_evaluations",
                                 "research_evaluation_revisions"}.issubset(tables))
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
