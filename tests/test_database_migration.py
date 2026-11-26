import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from literature_radar.database.academic import AcademicStore
from literature_radar.database.store import PaperStore
from literature_radar.candidates.processing import identity
from literature_radar.feedback.store import add_feedback, load_feedback
from literature_radar.models import Paper
from literature_radar.models import SourceRecord
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "legacy.sqlite3"

    def test_backup_and_incremental_migration_preserve_legacy_key(self):
        legacy = Paper("old-1", "Imaginary paper", "Fictional abstract", "fixture",
                       {"doi": "10.0000/fictional"})
        PaperStore(self.path).save("synthetic-topic", [legacy])
        backup = Path(self.tmp.name) / "backup.sqlite3"
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        store = AcademicStore(self.path)
        self.assertEqual(store.count(), 1)
        self.assertEqual(PaperStore(self.path).count("synthetic-topic"), 1)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT paper_key,paper_id FROM legacy_paper_mapping").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(PaperStore(backup).count("synthetic-topic"), 1)
        self.assertEqual(AcademicStore(self.path).count(), 1)

    def test_migration_failure_rolls_back(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("CREATE TABLE papers (topic_id TEXT, paper_key TEXT, payload TEXT)")
                db.execute("INSERT INTO papers VALUES (?,?,?)", ("synthetic-topic", "old", "bad json"))
        with self.assertRaises(json.JSONDecodeError):
            AcademicStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='papers_v2'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)

    def test_legacy_feedback_key_survives_new_source_merge(self):
        legacy = Paper("old-fictional", "Imaginary paper", "Fictional abstract", "fixture",
                       {"doi": "10.0000/legacy"})
        old_key = identity(legacy)
        PaperStore(self.path).save("synthetic-topic", [legacy])
        feedback_path = Path(self.tmp.name) / "feedback.json"
        add_feedback(feedback_path, "synthetic-topic", old_key, "keep", "Fictional review")
        store = AcademicStore(self.path)
        query = QueryPlan("crossref", "synthetic-topic:crossref", {"term": "fictional"},
                          DateWindow("2026-01-01", "2026-01-31"))
        stamp = "2026-01-01T00:00:00+00:00"
        response = PageResult((RawRecord("crossref", {}, stamp),), None, False,
                              RequestMetadata("crossref", stamp))
        record = SourceRecord("crossref", "10.0000/legacy", stamp,
                              {"doi": "https://doi.org/10.0000/legacy"},
                              {"title": "Imaginary paper", "abstract": None})
        store.commit_page("synthetic-topic", query, response, [record])
        self.assertEqual(store.count(), 1)
        self.assertIsNotNone(PaperStore(self.path).load("synthetic-topic", old_key))
        self.assertEqual(load_feedback(feedback_path)["entries"][0]["paper_key"], old_key)
        with closing(sqlite3.connect(self.path)) as db:
            mapped = db.execute("SELECT paper_id FROM legacy_paper_mapping WHERE paper_key=?", (old_key,)).fetchone()[0]
            identified = db.execute("SELECT paper_id FROM identifiers WHERE kind='doi' AND value='10.0000/legacy'").fetchone()[0]
        self.assertEqual(mapped, identified)
