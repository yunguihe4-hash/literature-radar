"""Offline stress and transaction checks using only fictional records."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.models import SourceRecord
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata

STAMP = "2026-01-01T00:00:00+00:00"


def plan(topic):
    return QueryPlan("fixture", f"{topic}:fixture", {"term": "fictional"},
                     DateWindow("2026-01-01", "2026-01-31"))


def page(source, *, cursor=None, more=False):
    return PageResult((RawRecord("fixture", {"source_id": source}, STAMP),), cursor, more,
                      RequestMetadata("fixture", STAMP))


def record(source):
    return SourceRecord("fixture", source, STAMP, {"doi": f"10.0000/{source}"},
                        {"title": f"Imaginary paper {source}", "abstract": "Fictional abstract"})


class SQLiteReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reliability.sqlite3"
        self.store = AcademicStore(self.path)

    def test_two_topics_concurrent_writes_keep_independent_checkpoints(self):
        def worker(topic):
            store = AcademicStore(self.path)
            query = plan(topic)
            for number in range(100):
                source = f"{topic}-{number}"
                store.commit_page(topic, query,
                                  page(source, cursor=number + 1 if number < 99 else None,
                                       more=number < 99),
                                  [record(source)],
                                  expected_page_number=number,
                                  expected_cursor=number if number else None)
            return store.checkpoint("fixture", topic, query.query_signature)["page_number"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ("fictional-a", "fictional-b")))
        self.assertEqual(results, [100, 100])
        self.assertEqual(self.store.count(), 200)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_mid_page_database_error_rolls_back_records_and_checkpoint(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON source_records "
                           "WHEN NEW.source_id='bad' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        query = plan("fictional-a")
        response = PageResult((RawRecord("fixture", {}, STAMP), RawRecord("fixture", {}, STAMP)),
                              2, True, RequestMetadata("fixture", STAMP))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_page("fictional-a", query, response,
                                   [record("good"), record("bad")],
                                   expected_page_number=0, expected_cursor=None)
        self.assertEqual(self.store.count(), 0)
        self.assertIsNone(self.store.checkpoint("fixture", "fictional-a", query.query_signature))

    def test_short_database_lock_waits_then_commits_and_closes(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        started = threading.Event()
        try:
            connection.execute("BEGIN IMMEDIATE")
            def writer():
                started.set()
                self.store.commit_page("fictional-a", plan("fictional-a"), page("one"),
                                       [record("one")], expected_page_number=0,
                                       expected_cursor=None)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(writer)
                self.assertTrue(started.wait(timeout=5))
                time.sleep(0.1)
                self.assertFalse(future.done())
                connection.rollback()
                future.result(timeout=5)
        finally:
            connection.close()
        self.assertEqual(self.store.count(), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
