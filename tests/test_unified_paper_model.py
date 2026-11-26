import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from literature_radar.candidates.processing import identity
from literature_radar.database.store import PaperStore
from literature_radar.models import FieldProvenance, Paper


class UnifiedPaperModelTests(unittest.TestCase):
    def test_legacy_positional_shape_and_key_unchanged(self):
        paper = Paper("SYN-1", "Synthetic paper", "Synthetic abstract", "synthetic-index",
                      {"doi": "10.0000/fictional"})
        self.assertEqual(paper.as_dict(), {
            "source_id": "SYN-1", "title": "Synthetic paper",
            "abstract": "Synthetic abstract", "source": "synthetic-index",
            "identifiers": {"doi": "10.0000/fictional"}, "published": None})
        self.assertEqual(identity(paper), "doi:10.0000/fictional")
        self.assertEqual(Paper.from_dict(paper.as_dict()), paper)

    def test_new_fields_round_trip_and_dates_stay_distinct(self):
        provenance = FieldProvenance("synthetic-index", "SYN-2", "2026-01-04T00:00:00+00:00", "abstract")
        paper = Paper("SYN-2", "Imaginary acoustic sample", None, "synthetic-index",
                      paper_id="local-synthetic-2", authors=("Fictional Author",),
                      publication_date="2026-01-01", online_date="2026-01-02",
                      indexed_date="2026-01-03", created_date="2026-01-04",
                      updated_date="2026-01-05", first_seen_at="2026-01-06T00:00:00+00:00",
                      last_seen_at="2026-01-07T00:00:00+00:00",
                      doi="10.0000/fictional-2", pmid="SYN-PMID",
                      pmcid="SYN-PMCID", openalex_id="SYN-OA",
                      provenance={"abstract": provenance})
        encoded = json.loads(json.dumps(paper.as_dict()))
        self.assertEqual(encoded["model_version"], 2)
        self.assertEqual(Paper.from_dict(encoded), paper)
        self.assertNotEqual(encoded["publication_date"], encoded["indexed_date"])
        self.assertIsNone(encoded["published"])
        self.assertIsNone(encoded["abstract"])

    def test_missing_optional_fields_default(self):
        paper = Paper.from_dict({"source_id": "SYN-3", "title": "Synthetic title",
                                 "source": "synthetic-index"})
        self.assertIsNone(paper.abstract)
        self.assertIsNone(paper.doi)
        self.assertEqual(paper.authors, ())

    def test_new_paper_store_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "new.sqlite3"
            paper = Paper("SYN-NEW", "Fictional record", None, "synthetic-index",
                          paper_id="local-fictional", authors=("Imaginary Author",),
                          publication_date="2026-02-01")
            store = PaperStore(path)
            store.save("synthetic-topic", [paper])
            self.assertEqual(store.load("synthetic-topic", identity(paper)), paper)

    def test_invalid_date_and_unknown_version_rejected(self):
        with self.assertRaisesRegex(ValueError, "publication_date"):
            Paper("SYN-4", "Synthetic", None, "synthetic-index",
                  publication_date="2026-99-99")
        with self.assertRaisesRegex(ValueError, "model_version"):
            Paper.from_dict({"model_version": 3, "source_id": "SYN-4",
                             "title": "Synthetic", "source": "synthetic-index"})

    def test_legacy_sqlite_payload_loads_without_schema_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            payload = {"source_id": "SYN-5", "title": "Legacy fictional paper",
                       "abstract": "Fictional abstract", "source": "synthetic-index",
                       "identifiers": {}, "published": "2025"}
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute("CREATE TABLE papers (topic_id TEXT NOT NULL, paper_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(topic_id,paper_key))")
                    db.execute("INSERT INTO papers VALUES (?,?,?)",
                               ("synthetic-topic", "source:synthetic-index:SYN-5", json.dumps(payload)))
            paper = PaperStore(path).load("synthetic-topic", "source:synthetic-index:SYN-5")
            self.assertEqual(paper.published, "2025")
            self.assertIsNone(paper.publication_date)
            self.assertEqual(PaperStore(path).count("synthetic-topic"), 1)
