"""Offline production-event and notification-loop regressions."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.engine import dispatch_outbox
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.feedback.store import add_feedback
from literature_radar.config import load_bundle
from tests.synthetic_provider import SyntheticProvider

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"
ABSTRACT = "A spacecraft film uses heat cycling to assess coating durability in a fictional trial."


def row(source, abstract=ABSTRACT, doi="10.0000/imaginary-film"):
    return {"source_id": source, "doi": doi, "title": "Fictional coating durability study",
            "abstract": abstract, "authors": ["Fictional Author"], "publication_date": "2026-01-01"}


class Recorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, payload, key):
        if self.fail:
            raise RuntimeError("fictional delivery failure")
        self.sent.append((payload, key))


class ProductionEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "events.sqlite3"

    def run_sources(self, providers, *, since="2026-01-01", until="2026-01-31",
                    recorder=None, feedback=None):
        return discover_incremental(CONFIG, list(providers), database_path=self.database,
                                    since=since, until=until, providers=providers,
                                    channels=["obsidian"],
                                    notifiers={"obsidian": recorder} if recorder else {},
                                    feedback_path=feedback, strict_records=True)

    def test_filling_document_type_alone_does_not_create_material_update(self):
        class TypeProvider(SyntheticProvider):
            def normalize(self, raw):
                record = super().normalize(raw)
                return replace(record, normalized_metadata={
                    **record.normalized_metadata,
                    "document_type": raw.payload.get("document_type")})

        first = row("source-a")
        self.run_sources({"openalex": TypeProvider("openalex", [[first]])})
        second = {**row("source-b"), "document_type": "journal-article"}
        result = self.run_sources({"crossref": TypeProvider("crossref", [[second]])},
                                  since="2026-02-01", until="2026-02-28")
        self.assertNotIn("MATERIAL_UPDATE", result["paper_event_counts"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)
        third = row("source-a", ABSTRACT + " Revised method.")
        changed = self.run_sources({"openalex": TypeProvider("openalex", [[third]])},
                                   since="2026-03-01", until="2026-03-31")
        self.assertEqual(changed["paper_event_counts"].get("MATERIAL_UPDATE"), 1)

    def test_five_sources_create_one_new_paper_and_one_notification(self):
        names = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")
        providers = {name: SyntheticProvider(name, [[row(name)]]) for name in names}
        recorder = Recorder()
        result = self.run_sources(providers, recorder=recorder)
        self.assertEqual(result["paper_event_counts"]["NEW_PAPER"], 1)
        self.assertEqual(result["notifications"], {"queued": 1, "sent": 1, "failed": 0})
        self.assertEqual(len(recorder.sent), 1)
        self.assertEqual(AcademicStore(self.database).count(), 1)
        self.assertEqual(len(recorder.sent[0][0]["paper"]["sources"]), 5)
        second = self.run_sources(providers, recorder=recorder)
        self.assertEqual(second["notifications"]["queued"], 0)
        self.assertEqual(len(recorder.sent), 1)

    def test_material_update_realerts_and_dynamic_metric_does_not(self):
        recorder = Recorder()
        self.run_sources({"openalex": SyntheticProvider("openalex", [[row("same")]])}, recorder=recorder)
        changed = row("same", ABSTRACT + " The method was revised.")
        result = self.run_sources({"openalex": SyntheticProvider("openalex", [[changed]])},
                                  since="2026-02-01", until="2026-02-28", recorder=recorder)
        self.assertEqual(result["paper_event_counts"]["MATERIAL_UPDATE"], 1)
        self.assertEqual(len(recorder.sent), 2)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox WHERE status='SENT'").fetchone()[0], 2)

    def test_notification_failure_does_not_rollback_checkpoint(self):
        failed = Recorder(fail=True)
        result = self.run_sources({"openalex": SyntheticProvider("openalex", [[row("one")]])}, recorder=failed)
        self.assertEqual(result["providers"]["openalex"]["status"], "SUCCESS")
        self.assertEqual(result["notifications"]["failed"], 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status,attempts FROM notification_outbox").fetchone(), ("FAILED", 1))
            self.assertEqual(db.execute("SELECT status FROM provider_checkpoints").fetchone()[0], "SUCCESS")
        recovered = Recorder()
        store = AcademicStore(self.database)
        self.assertEqual(store.retry_failed_notifications(channel="obsidian"), 1)
        self.assertEqual(dispatch_outbox(store, {"obsidian": recovered})["sent"], 1)
        self.assertEqual(len(recovered.sent), 1)

    def test_feedback_discard_suppresses_alert_without_changing_evidence(self):
        feedback = Path(self.tmp.name) / "feedback.json"
        add_feedback(feedback, "coating-demo", "doi:10.0000/imaginary-film", "discard", "Fictional choice")
        result = self.run_sources({"openalex": SyntheticProvider("openalex", [[row("one")]])},
                                  feedback=feedback, recorder=Recorder())
        self.assertEqual(result["notifications"]["queued"], 0)
        self.assertEqual(result["recommendations"][0]["priority"], "HIGH")
        self.assertFalse(result["recommendations"][0]["eligible"])

    def test_malformed_record_blocks_page_in_strict_production_mode(self):
        provider = SyntheticProvider("openalex", [[row("one"), {"malformed": True}]])
        result = self.run_sources({"openalex": provider}, recorder=Recorder())
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(AcademicStore(self.database).count(), 0)
        malformed = result["providers"]["openalex"]["malformed_records"]
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0]["category"], "ValueError")
        self.assertEqual(len(malformed[0]["payload_sha256"]), 64)
        self.assertNotIn("abstract", str(malformed))

    def test_openalex_missing_title_is_quarantined_with_page_commit(self):
        class MissingTitleProvider(SyntheticProvider):
            def normalize(self, raw):
                if raw.payload.get("id"):
                    raise ValueError("missing title")
                return super().normalize(raw)

        provider = MissingTitleProvider("openalex", [[
            row("valid"), {"id": "https://openalex.org/W0000", "abstract": "Fictional hidden text"}]])
        result = self.run_sources({"openalex": provider})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["providers"]["openalex"]["quarantined"], 1)
        self.assertEqual(AcademicStore(self.database).count(), 1)
        with closing(sqlite3.connect(self.database)) as db:
            audit = db.execute("SELECT identifier,category,page_number FROM record_quarantine").fetchone()
            self.assertEqual(audit, ("https://openalex.org/W0000", "missing_title", 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)
            self.assertNotIn("Fictional hidden text", db.execute("SELECT summary_json FROM sync_runs").fetchone()[0])

    def test_five_provider_two_page_partial_failure_resumes_without_duplicates(self):
        names = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")
        providers = {name: SyntheticProvider(name, [
            [row(name + "-first")], [row(name + "-second", doi="10.0000/second-film")]])
                     for name in names}
        providers["europepmc"].fail_at = 1
        first = self.run_sources(providers, recorder=Recorder())
        self.assertEqual(first["status"], "PARTIAL_SUCCESS")
        self.assertEqual(AcademicStore(self.database).count(), 2)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 9)
            compact = db.execute("SELECT summary_json FROM sync_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            self.assertNotIn("Fictional coating durability study", compact)
            self.assertNotIn("recommendations", compact)
        providers["europepmc"].fail_at = None
        second = self.run_sources(providers, recorder=Recorder())
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(providers["europepmc"].calls[-1], 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_paper_state").fetchone()[0], 2)

    def test_strong_identifier_conflict_creates_review_event(self):
        first = row("one") | {"pmid": "123"}
        second = row("two") | {"pmid": "456"}
        providers = {"openalex": SyntheticProvider("openalex", [[first]]),
                     "pubmed": SyntheticProvider("pubmed", [[second]])}
        summary = self.run_sources(providers, recorder=Recorder())
        self.assertEqual(summary["paper_event_counts"]["REVIEW_CONFLICT"], 1)
        self.assertEqual(AcademicStore(self.database).count(), 1)

    def test_uncached_abstract_preserves_source_priority_and_content_version(self):
        bundle = load_bundle(CONFIG)
        bundle["topic"]["extensions"] = {"field_priority": {"abstract": ["openalex", "crossref"]}}
        first = Recorder()
        with patch("literature_radar.discovery.incremental.load_bundle", return_value=bundle):
            self.run_sources({"openalex": SyntheticProvider("openalex", [[row("first", ABSTRACT)]])},
                             recorder=first)
            second = self.run_sources({"crossref": SyntheticProvider("crossref", [[row("second", "A different fictional abstract")]])},
                                      since="2026-02-01", until="2026-02-28", recorder=first)
        self.assertEqual(second["paper_event_counts"]["DUPLICATE_SOURCE"], 1)
        self.assertNotIn("MATERIAL_UPDATE", second["paper_event_counts"])
        self.assertEqual(len(first.sent), 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_field_hashes").fetchone()[0], 1)
