"""Specialty discovery reuses checkpoints and does not touch Weekly state."""
import tempfile
import unittest
import sqlite3
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.research.specialty import run_specialty, recall_audit, query_variants
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class SpecialtyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        self.database = self.root / "academic.sqlite3"

    def test_targeted_uses_configured_term_and_independent_checkpoint(self):
        provider = SyntheticProvider("openalex", [[fictional("targeted")]])
        term = __import__("json").loads((self.config / "topic.json").read_text())["search_terms"][0]
        before = (self.config / "workflow-state.json").read_bytes()
        first = run_specialty(self.config, self.database, mode="targeted", term=term,
                              since="2026-01-01", until="2026-01-07",
                              provider_names=["openalex"], providers={"openalex": provider},
                              evaluation_run_dir=self.root / "targeted-review")
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(len(first["candidate_paper_ids"]), 1)
        self.assertTrue((self.root / "targeted-review/evaluation-request.json").is_file())
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_paper_state").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_events").fetchone()[0], 0)
        self.assertEqual(first["delivery_attempted"], False)
        self.assertEqual((self.config / "workflow-state.json").read_bytes(), before)
        calls = len(provider.calls)
        second = run_specialty(self.config, self.database, mode="targeted", term=term,
                               since="2026-01-01", until="2026-01-07",
                               provider_names=["openalex"], providers={"openalex": provider})
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(len(provider.calls), calls)
        self.assertEqual(AcademicStore(self.database).count(), 1)
        normal = discover_incremental(self.config, ["openalex"], database_path=self.database,
                                      since="2026-01-08", until="2026-01-14",
                                      providers={"openalex": SyntheticProvider("openalex", [[fictional("targeted")]])})
        self.assertEqual(normal["paper_event_counts"].get("NEW_PAPER"), 1)
        with self.assertRaisesRegex(ValueError, "must be configured"):
            run_specialty(self.config, self.database, mode="targeted", term="fictional unknown",
                          since="2026-01-01", until="2026-01-07",
                          provider_names=["openalex"], providers={"openalex": provider})

    def test_deep_variants_partial_provider_and_recall_audit(self):
        from literature_radar.config import load_bundle
        variants = query_variants(load_bundle(self.config), per_group=2)
        self.assertTrue(variants)
        providers = {
            "openalex": SyntheticProvider("openalex", [[fictional("deep")]]),
            "pubmed": SyntheticProvider("pubmed", [[fictional("failed")]], fail_at=0),
        }
        result = run_specialty(self.config, self.database, mode="deep-baseline",
                               since="2026-01-01", until="2026-01-07",
                               provider_names=list(providers), providers=providers,
                               variants_per_group=2)
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertGreater(len(providers["openalex"].calls), 1)
        audit = recall_audit(self.config, self.database,
                             [{"doi": "10.0000/fictional"}, {"doi": "10.0000/missing"}])
        self.assertEqual(audit["naturally_recovered"], 1)
        self.assertEqual(audit["recall_rate"], 0.5)
