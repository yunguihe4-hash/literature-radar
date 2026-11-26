import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from literature_radar.database.academic import AcademicStore, CheckpointConflict
from literature_radar.models import SourceRecord
from literature_radar.models import Paper
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata

STAMP = "2026-01-01T00:00:00+00:00"
def fixture(provider, source, doi="10.0000/fictional"):
    return SourceRecord(provider, source, STAMP, {"doi": doi},
                        {"title": "Imaginary paper", "abstract": "Fictional abstract", "authors": []})
def page(provider, count=1, cursor=None, more=False):
    return PageResult(tuple(RawRecord(provider, {}, STAMP) for _ in range(count)), cursor, more,
                      RequestMetadata(provider, STAMP))
def plan(provider="openalex", topic="synthetic-topic", term="imaginary"):
    return QueryPlan(provider, f"{topic}:{provider}", {"term": term}, DateWindow("2026-01-01", "2026-01-31"))

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "papers.sqlite3"
        self.store = AcademicStore(self.path)

    def test_page_transaction_and_resume(self):
        query = plan()
        self.store.commit_page("synthetic-topic", query, page("openalex", cursor="next", more=True),
                               [fixture("openalex", "a")])
        saved = self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)
        self.assertEqual(json.loads(saved["cursor_json"]), "next")
        self.assertEqual(saved["status"], "IN_PROGRESS")
        self.store.commit_page("synthetic-topic", query, page("openalex"), [fixture("openalex", "a")])
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)["status"], "SUCCESS")

    def test_failure_does_not_advance_or_write(self):
        query = plan()
        self.store.commit_page("synthetic-topic", query, page("openalex", cursor="next", more=True),
                               [fixture("openalex", "a")])
        before = self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)
        invalid = SourceRecord("openalex", "broken", STAMP, {}, {"abstract": "No title"})
        with self.assertRaises(ValueError):
            self.store.commit_page("synthetic-topic", query, page("openalex"), [invalid])
        after = self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)
        self.assertEqual(after["page_number"], before["page_number"])
        self.assertEqual(self.store.count(), 1)
        self.store.mark_failed("synthetic-topic", query)
        self.assertEqual(self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)["status"], "FAILED")

    def test_quarantine_and_checkpoint_share_transaction(self):
        query = plan()
        audit = [{"payload_sha256": "a" * 64, "identifier": "W0000", "category": "missing_title"}]
        invalid = SourceRecord("openalex", "broken", STAMP, {}, {"abstract": "Fictional only"})
        with self.assertRaises(ValueError):
            self.store.commit_page("synthetic-topic", query, page("openalex"), [invalid],
                                   malformed_records=audit)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM record_quarantine").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints").fetchone()[0], 0)
        self.store.commit_page("synthetic-topic", query, page("openalex"), [], malformed_records=audit)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM record_quarantine").fetchone()[0], 1)

    def test_topic_provider_and_signature_isolation(self):
        for topic, provider in (("synthetic-topic", "openalex"),
                                ("synthetic-acoustic", "openalex"),
                                ("synthetic-topic", "crossref")):
            query = plan(provider, topic)
            self.store.commit_page(topic, query, page(provider), [fixture(provider, topic + provider)])
            self.assertEqual(self.store.checkpoint(provider, topic, query.query_signature)["page_number"], 1)
        changed = plan(term="changed")
        self.assertIsNone(self.store.checkpoint("openalex", "synthetic-topic", changed.query_signature))
        self.assertEqual(self.store.count(), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_identifier_conflict_is_recorded_without_forced_merge(self):
        first = fixture("crossref", "c", "10.0000/one")
        first = SourceRecord(first.provider_id, first.source_id, STAMP,
                             {"doi": "10.0000/one", "pmid": "123"}, first.normalized_metadata)
        second = fixture("pubmed", "p", "10.0000/two")
        second = SourceRecord(second.provider_id, second.source_id, STAMP,
                              {"doi": "10.0000/two", "pmid": "123"}, second.normalized_metadata)
        self.store.commit_page("synthetic-topic", plan("crossref"), page("crossref"), [first])
        _, conflicts = self.store.commit_page("synthetic-topic", plan("pubmed"), page("pubmed"), [second])
        self.assertEqual(conflicts, 1)
        self.assertEqual(self.store.count(), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0], 1)

    def test_abstract_opt_in_and_preprint_relation(self):
        preprint = fixture("biorxiv", "preprint", "10.0000/preprint")
        preprint = SourceRecord(preprint.provider_id, preprint.source_id, STAMP,
                                preprint.identifiers,
                                {**preprint.normalized_metadata, "published_doi": "10.0000/published"})
        self.store.commit_page("synthetic-topic", plan("biorxiv"), page("biorxiv"), [preprint])
        self.store.commit_page("synthetic-topic", plan("crossref"), page("crossref"),
                               [fixture("crossref", "published", "10.0000/published")])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.relation_count(), 2)
        with closing(sqlite3.connect(self.path)) as db:
            payload = json.loads(db.execute("SELECT payload FROM papers_v2 WHERE paper_id=(SELECT paper_id FROM identifiers WHERE kind='doi' AND value='10.0000/preprint')").fetchone()[0])
            self.assertIsNone(payload["abstract"])
            source = json.loads(db.execute("SELECT payload FROM source_records WHERE provider_id='biorxiv'").fetchone()[0])
            self.assertIsNone(source["normalized_metadata"]["abstract"])
            self.assertEqual({row[0] for row in db.execute("SELECT relation_type FROM paper_relations")},
                             {"published_as", "has_preprint"})

    def test_fingerprint_classifies_new_unchanged_and_update(self):
        query = plan()
        original = fixture("openalex", "one")
        changed = SourceRecord(original.provider_id, original.source_id, STAMP,
                               original.identifiers,
                               {**original.normalized_metadata, "abstract": "Revised fictional abstract"})
        counts = {}
        self.store.commit_page("synthetic-topic", query, page("openalex"), [original], event_counts=counts)
        self.assertEqual(counts, {"new": 1, "updated": 0, "unchanged": 0})
        self.store.commit_page("synthetic-topic", query, page("openalex"), [original], event_counts=counts)
        self.assertEqual(counts, {"new": 0, "updated": 0, "unchanged": 1})
        self.store.commit_page("synthetic-topic", query, page("openalex"), [changed], event_counts=counts)
        self.assertEqual(counts, {"new": 0, "updated": 1, "unchanged": 0})
        self.assertEqual(self.store.count(), 1)

    def test_scientific_content_changes_but_citation_count_does_not(self):
        store = AcademicStore(self.path, cache_abstracts=True)
        query = plan()
        base = fixture("openalex", "one")
        fields = {**base.normalized_metadata, "authors": ["Fictional Author"],
                  "journal": "Imaginary Journal", "publication_date": "2026-01-01",
                  "citation_count": 1}
        first = SourceRecord(base.provider_id, base.source_id, STAMP, base.identifiers, fields)
        citation_only = SourceRecord(base.provider_id, base.source_id, STAMP, base.identifiers,
                                     {**fields, "citation_count": 99})
        changed = SourceRecord(base.provider_id, base.source_id, STAMP, base.identifiers,
                               {**fields, "abstract": "Revised fictional abstract",
                                "authors": ["Revised Fictional Author"],
                                "journal": "Revised Imaginary Journal",
                                "online_date": "2026-01-02"})
        counts = {}
        store.commit_page("synthetic-topic", query, page("openalex"), [first], event_counts=counts)
        store.commit_page("synthetic-topic", query, page("openalex"), [citation_only], event_counts=counts)
        self.assertEqual(counts["unchanged"], 1)
        store.commit_page("synthetic-topic", query, page("openalex"), [changed], event_counts=counts)
        self.assertEqual(counts["updated"], 1)
        with closing(sqlite3.connect(self.path)) as db:
            paper = Paper.from_dict(json.loads(db.execute("SELECT payload FROM papers_v2").fetchone()[0]))
        self.assertEqual(paper.abstract, "Revised fictional abstract")
        self.assertEqual(paper.authors, ("Revised Fictional Author",))
        self.assertEqual(paper.journal, "Revised Imaginary Journal")
        self.assertEqual(paper.online_date, "2026-01-02")

    def test_concurrent_same_checkpoint_only_one_runner_advances(self):
        query = plan()
        gate = Barrier(2)
        def attempt(source):
            separate_store = AcademicStore(self.path)
            gate.wait(timeout=5)
            try:
                separate_store.commit_page("synthetic-topic", query,
                                           page("openalex", cursor=1, more=True),
                                           [fixture("openalex", source, f"10.0000/{source}")],
                                           expected_page_number=0, expected_cursor=None)
                return "committed"
            except CheckpointConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("runner-a", "runner-b")))
        self.assertEqual(sorted(results), ["committed", "conflict"])
        checkpoint = self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)
        self.assertEqual(checkpoint["page_number"], 1)
        self.assertEqual(checkpoint["status"], "IN_PROGRESS")
        self.store.mark_failed("synthetic-topic", query, expected_page_number=0)
        self.assertEqual(self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)["status"],
                         "IN_PROGRESS")
        self.assertEqual(self.store.count(), 1)

    def test_stale_cursor_rejected_before_source_write(self):
        query = plan()
        self.store.commit_page("synthetic-topic", query,
                               page("openalex", cursor="next", more=True),
                               [fixture("openalex", "first")],
                               expected_page_number=0, expected_cursor=None)
        with self.assertRaises(CheckpointConflict):
            self.store.commit_page("synthetic-topic", query, page("openalex"),
                                   [fixture("openalex", "stale", "10.0000/stale")],
                                   expected_page_number=1, expected_cursor="wrong")
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.checkpoint("openalex", "synthetic-topic", query.query_signature)["page_number"], 1)
