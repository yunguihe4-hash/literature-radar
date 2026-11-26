"""Offline notification, schedule and backup acceptance tests."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.database.backup import backup_database, restore_database
from literature_radar.delivery.engine import dispatch_outbox
from literature_radar.delivery.notifiers import DesktopNotifier, NotificationUnavailable, ObsidianNotifier
from literature_radar.models import Paper
from literature_radar.scheduler.runner import run_due
from tests.synthetic_provider import SyntheticProvider

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "synthetic-topic"
ABSTRACT = "A spacecraft film uses heat cycling to assess coating durability in a fictional trial."


def row(source):
    return {"source_id": source, "title": "Fictional coating durability",
            "doi": f"10.0000/{source}", "abstract": ABSTRACT}


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_obsidian_note_is_idempotent_and_updates_only_inbox(self):
        vault = self.root / "vault"
        vault.mkdir()
        notifier = ObsidianNotifier(vault)
        payload = {"topic_id": "coating-demo", "content_version": "fictional-v1",
                   "priority": "HIGH", "paper": {"paper_id": "fictional-id", "title": "Imaginary material",
                   "authors": ["Fictional Author"], "publication_date": "2026-01-01",
                   "sources": ["fixture"], "doi": "10.0000/imaginary",
                   "landing_url": "https://example.invalid/imaginary"},
                   "reason": {"questions": ["durability"], "methods": ["heat-cycle"],
                              "applications": [], "transfers": [{"type": "method_transfer"}],
                              "evidence": [{"phrase": "heat cycling"}]}}
        notifier.send(payload, "fictional-key")
        notifier.send(payload, "fictional-key")
        notes = list((vault / "Inbox" / "Literature").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text().count("literature-radar-version:"), 1)
        notifier.send({**payload, "content_version": "fictional-v2"}, "fictional-key-2")
        self.assertEqual(notes[0].read_text().count("literature-radar-version:"), 2)
        self.assertFalse((vault / "Knowledge").exists())

    def test_delivery_crash_replay_keeps_one_obsidian_note(self):
        from literature_radar.discovery.incremental import discover_incremental
        shutil.copytree(CONFIG, self.root / "topic")
        database = self.root / "outbox.sqlite3"
        discover_incremental(self.root / "topic", ["openalex"], database_path=database,
                             since="2026-01-01", until="2026-01-31",
                             providers={"openalex": SyntheticProvider("openalex", [[row("one")]])},
                             channels=["obsidian"])
        store = AcademicStore(database)
        vault = self.root / "vault"
        vault.mkdir()
        notifier = ObsidianNotifier(vault)
        claimed = store.claim_notification(channels=("obsidian",))
        self.assertIsNotNone(claimed)
        notifier.send(json.loads(claimed["payload_json"]), claimed["idempotency_key"])
        with closing(sqlite3.connect(database)) as db:
            with db:
                db.execute("UPDATE notification_outbox SET updated_at='2020-01-01T00:00:00+00:00'")
        self.assertEqual(dispatch_outbox(store, {"obsidian": notifier})["sent"], 1)
        note = next((vault / "Inbox" / "Literature").glob("*.md"))
        self.assertEqual(note.read_text().count("literature-radar-version:"), 1)

    def test_desktop_unavailable_is_explicit(self):
        with patch("literature_radar.delivery.notifiers.shutil.which", return_value=None):
            with self.assertRaises(NotificationUnavailable):
                DesktopNotifier().send({"paper": {"title": "Fictional"},
                                        "reason": {"questions": []}}, "fictional")

    def test_attention_gate_rejects_negation_keyword_only_and_missing_abstract(self):
        bundle = load_bundle(CONFIG)
        good = Paper("fictional", "Fictional coating durability", ABSTRACT, "fixture")
        self.assertTrue(recommend(good, bundle)["eligible"])
        negative = Paper("fictional", "Fictional coating durability",
                         "No heat cycling was used; coating durability was not measured.", "fixture")
        self.assertFalse(recommend(negative, bundle)["eligible"])
        coincidental = Paper("fictional", "Fictional cycling note",
                             "Heat cycling appears as an unrelated metaphor.", "fixture")
        self.assertFalse(recommend(coincidental, bundle)["eligible"])
        self.assertFalse(recommend(Paper("fictional", good.title, None, "fixture"), bundle)["eligible"])
        self.assertTrue(recommend(good, bundle, decision="must_read")["eligible"])
        self.assertFalse(recommend(good, bundle, decision="later")["eligible"])
        after = Paper("fictional", "Fictional coating durability",
                      "Heat cycling was not used to assess coating durability.", "fixture")
        self.assertFalse(recommend(after, bundle)["eligible"])

    def test_fictional_cross_species_relation_is_configuration_only(self):
        bundle = load_bundle(CONFIG)
        bundle["profile"] = {"questions": [{"id": "imaginary-growth", "label": "fictional growth"}],
                             "methods": [{"id": "pattern-count", "label": "pattern counting"}],
                             "applications": []}
        bundle["ontology"] = {"concepts": [{"id": "organism-a-map", "label": "organism A map"}],
                              "relations": [{"from": "organism-a-map", "to": "pattern-count",
                                             "type": "method_transfer", "scope": "cross_species",
                                             "from_domain": "fictional organism A",
                                             "to_domain": "fictional organism B"}]}
        bundle["rules"] = {"positive": [{"term": "organism A map", "dimension": "method", "weight": 3},
                                        {"term": "fictional growth", "dimension": "question", "weight": 3}],
                           "negative": [], "priority_thresholds": {"high": 7, "medium": 3}}
        bundle["phase"] = {"priority_question_ids": ["imaginary-growth"],
                           "priority_method_ids": ["pattern-count"]}
        paper = Paper("fictional", "Invented organism A map",
                      "An organism A map may inform fictional growth in organism B.", "fixture")
        result = recommend(paper, bundle)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"]["transfers"][0]["scope"], "cross_species")

    def test_thirty_day_accelerated_schedule_and_provider_isolation(self):
        shutil.copytree(CONFIG, self.root / "topic")
        schedule = self.root / "schedule.json"
        names = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")
        schedule.write_text(json.dumps({"schema_version": 1, "topics": [{
            "config_dir": "topic", "providers": {name: (48 if name == "crossref" else None)
                                                   for name in names}}]}))
        database = self.root / "state.sqlite3"
        providers = {name: SyntheticProvider(name, [[row(name)]]) for name in names}
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        provider_failure_seen = False
        for day in range(30):
            providers["crossref"].fail_at = 0 if day == 4 else None
            result = run_due(schedule, database, now=start + timedelta(days=day), providers=providers)
            provider_failure_seen |= result["status"] == "PARTIAL_SUCCESS"
        self.assertTrue(provider_failure_seen)
        store = AcademicStore(database)
        self.assertEqual(store.count(), 5)
        with closing(sqlite3.connect(database)) as db:
            rows = db.execute("SELECT provider_id,status FROM provider_checkpoints").fetchall()
            self.assertGreaterEqual(len(rows), 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_paper_state").fetchone()[0], 5)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_capped_backlog_continues_without_skipping_pages(self):
        shutil.copytree(CONFIG, self.root / "topic")
        schedule = self.root / "schedule.json"
        schedule.write_text(json.dumps({"schema_version": 1, "topics": [{
            "config_dir": "topic", "providers": {"openalex": 24}, "max_pages": 2}]}))
        provider = SyntheticProvider("openalex", [[row(f"page-{n}")] for n in range(5)])
        database = self.root / "backlog.sqlite3"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        states = [run_due(schedule, database, now=start + timedelta(minutes=31 * n),
                          providers={"openalex": provider})["jobs"][0]["status"] for n in range(3)]
        self.assertEqual(states, ["INCOMPLETE", "INCOMPLETE", "SUCCESS"])
        self.assertEqual(provider.calls, [0, 1, 2, 3, 4])
        self.assertEqual(AcademicStore(database).count(), 5)

    def test_two_topics_keep_independent_schedule_state(self):
        shutil.copytree(CONFIG, self.root / "synthetic-topic")
        shutil.copytree(ROOT / "examples" / "synthetic-acoustic", self.root / "synthetic-acoustic")
        schedule = self.root / "schedule.json"
        schedule.write_text(json.dumps({"schema_version": 1, "topics": [
            {"config_dir": "synthetic-topic", "providers": {"openalex": None}},
            {"config_dir": "synthetic-acoustic", "providers": {"openalex": None}}]}))
        provider = SyntheticProvider("openalex", [[row("shared")]])
        database = self.root / "two-topics.sqlite3"
        result = run_due(schedule, database, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                         providers={"openalex": provider})
        self.assertEqual(result["status"], "SUCCESS")
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schedule_state").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_paper_state").fetchone()[0], 2)

    def test_concurrent_schedule_claim_allows_one_runner(self):
        store = AcademicStore(self.root / "claims.sqlite3")
        now = "2026-01-01T00:00:00+00:00"
        lease = "2026-01-01T01:00:00+00:00"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: store.claim_due("fictional", "openalex", now=now,
                                                               lease_until=lease), range(2)))
        self.assertEqual(sorted(results), [False, True])
        store.finish_due("fictional", "openalex", next_due_at="2026-01-02T00:00:00+00:00",
                         status="SUCCESS", lease_until=lease)
        self.assertFalse(store.claim_due("fictional", "openalex", now="2026-01-01T02:00:00+00:00",
                                         lease_until="2026-01-01T03:00:00+00:00"))

    def test_schedule_rejects_path_traversal_without_creating_database(self):
        schedule = self.root / "schedule.json"
        schedule.write_text(json.dumps({"schema_version": 1, "topics": [
            {"config_dir": "../outside", "providers": {"openalex": None}}]}))
        database = self.root / "must-not-exist.sqlite3"
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            run_due(schedule, database, dry_run=True)
        self.assertFalse(database.exists())

    def test_backup_restore_preserves_checkpoint_and_rejects_overwrite(self):
        database = self.root / "state.sqlite3"
        from literature_radar.discovery.incremental import discover_incremental
        discover_incremental(CONFIG, ["openalex"], database_path=database,
                             since="2026-01-01", until="2026-01-31",
                             providers={"openalex": SyntheticProvider("openalex", [[row("one")]])})
        backup = backup_database(database, self.root / "backup.sqlite3")
        restored = restore_database(backup, self.root / "restored.sqlite3")
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT version FROM schema_features WHERE feature='production_events'").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers_v2").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM provider_checkpoints").fetchone()[0], "SUCCESS")
        with self.assertRaises(FileExistsError):
            restore_database(backup, restored)
