"""Cross-source identity checks with invented metadata only."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.models import FieldProvenance, Paper, SourceRecord
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata

STAMP = "2026-01-01T00:00:00+00:00"


def record(provider, source, identifiers, *, title="Imaginary material", abstract=None,
           version=None, published_doi=None):
    fields = {"title": title, "abstract": abstract, "authors": ["Fictional Author"],
              "publication_date": "2026-01-01", "version": version,
              "published_doi": published_doi}
    provenance = {key: FieldProvenance(provider, source, STAMP, key)
                  for key, value in fields.items() if value is not None}
    return SourceRecord(provider, source, STAMP, identifiers, fields, STAMP, provenance)


def commit(store, item, *, priorities=None):
    provider = item.provider_id
    query = QueryPlan(provider, provider + ":fictional", {"term": "imaginary", "source": item.source_id},
                      DateWindow("2026-01-01", "2026-01-31"))
    page = PageResult((RawRecord(provider, {}, STAMP),), None, False,
                      RequestMetadata(provider, STAMP))
    return store.commit_page("synthetic-topic", query, page, [item], priorities=priorities)


class CrossSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "identity.sqlite3"
        self.store = AcademicStore(self.path, cache_abstracts=True)

    def test_five_sources_same_paper_with_missing_dois_and_provenance(self):
        items = [
            record("crossref", "c-1", {"doi": "DOI:10.0000/FICTIONAL"}, abstract="Crossref fictional"),
            record("openalex", "o-1", {"doi": "https://doi.org/10.0000/fictional",
                                           "openalex_id": "https://openalex.org/W42", "pmid": "123"},
                   abstract="OpenAlex fictional"),
            record("europepmc", "e-1", {"pmid": "123", "pmcid": "PMC123"},
                   abstract="Europe PMC fictional"),
            record("pubmed", "p-1", {"pmid": "123", "pmcid": "PMC123"}),
            record("biorxiv", "b-1", {"doi": "10.0000/fictional"}),
        ]
        priority = {"abstract": ["europepmc", "crossref", "openalex", "pubmed", "biorxiv"]}
        for item in items:
            _, conflicts = commit(self.store, item, priorities=priority)
            self.assertEqual(conflicts, 0)
        self.assertEqual(self.store.count(), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM identifiers").fetchone()[0], 4)
            paper = Paper.from_dict(json.loads(db.execute("SELECT payload FROM papers_v2").fetchone()[0]))
            provenance = json.loads(db.execute("SELECT payload FROM field_provenance WHERE field_name='abstract'").fetchone()[0])
        self.assertEqual(paper.abstract, "Europe PMC fictional")
        self.assertEqual(paper.pmid, "123")
        self.assertEqual(paper.pmcid, "PMC123")
        self.assertEqual(paper.openalex_id, "W42")
        self.assertEqual(len(paper.source_records), 5)
        self.assertEqual(provenance["provider_id"], "europepmc")

    def test_similar_title_different_doi_remains_separate(self):
        commit(self.store, record("crossref", "c-1", {"doi": "10.0000/one"},
                                  title="Imaginary material alpha"))
        commit(self.store, record("openalex", "o-1", {"doi": "10.0000/two"},
                                  title="Imaginary material alphas"))
        self.assertEqual(self.store.count(), 2)

    def test_source_specific_id_is_stable_without_global_identifiers(self):
        commit(self.store, record("europepmc", "local-1", {}, abstract="Fictional first"))
        commit(self.store, record("europepmc", "local-1", {}, abstract="Fictional revised"))
        self.assertEqual(self.store.count(), 1)
        commit(self.store, record("pubmed", "local-1", {}, abstract="Fictional separate"))
        self.assertEqual(self.store.count(), 2)

    def test_conflicting_identifier_is_auditable(self):
        commit(self.store, record("crossref", "c-1", {"doi": "10.0000/one", "pmid": "123"}))
        _, conflicts = commit(self.store, record("europepmc", "e-1",
                                                 {"doi": "10.0000/one", "pmid": "456"}))
        self.assertEqual(conflicts, 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT reason FROM identity_conflicts").fetchone()[0],
                             "identifier_conflict")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)

    def test_preprint_versions_link_published_paper_without_merging(self):
        commit(self.store, record("biorxiv", "preprint#v1", {"doi": "10.0000/preprint"},
                                  abstract="Fictional version one", version="1"))
        commit(self.store, record("biorxiv", "preprint#v2", {"doi": "10.0000/preprint"},
                                  abstract="Fictional version two", version="2",
                                  published_doi="10.0000/published"))
        commit(self.store, record("crossref", "published", {"doi": "10.0000/published"},
                                  abstract="Fictional journal version"))
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.relation_count(), 2)
        with closing(sqlite3.connect(self.path)) as db:
            preprint_id = db.execute("SELECT paper_id FROM identifiers WHERE kind='doi' AND value='10.0000/preprint'").fetchone()[0]
            payload = db.execute("SELECT payload FROM papers_v2 WHERE paper_id=?", (preprint_id,)).fetchone()[0]
            relations = {row[0] for row in db.execute("SELECT relation_type FROM paper_relations")}
        self.assertEqual(Paper.from_dict(json.loads(payload)).abstract, "Fictional version two")
        self.assertEqual(relations, {"published_as", "has_preprint"})
