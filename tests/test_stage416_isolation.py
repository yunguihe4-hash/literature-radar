"""Fictional Stage 4.1.6 evidence gates and Weekly journal isolation."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from literature_radar.attention.recommendation import recommend
from literature_radar.bootstrap30 import (allocate_candidate_pool, allocate_review,
                                          scientific_candidate_issues)
from literature_radar.config import load_bundle
from literature_radar.delivery.notifiers import render_alert
from literature_radar.models import Paper
from literature_radar.workflow.weekly import _rank_weekly_rows, activate_weekly, run_weekly
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import RichProvider, intake, record, NOW

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TARGET = "Imaginaryus alpha"


def candidate(i, group="B", *, species="Fictivus beta", method="method-1"):
    return {"paper_id": f"SYN-{i}", "group": group, "title": f"Fictional evidence {i}",
            "doi": f"10.0000/study.{i}", "species": TARGET if group == "A" else species,
            "source_species": species, "source_question": "Resolve synthetic repeats",
            "method": "Fictional long-read assay", "result": "Resolved a repeat",
            "borrowable_detail": "Compare repeat-spanning reads",
            "technical_step": "Align independent synthetic reads",
            "limitations": "Needs target-specific validation",
            "original_evidence": "Synthetic abstract states the result",
            "target_question": "Resolve target synthetic repeats",
            "evidence_matched": True, "relevance_verified": True,
            "relevance_evidence": "Synthetic question and method are explicit",
            "scientific_relevance": 0.9, "methodological_value": 0.8,
            "evidence_completeness": 1.0, "method_category": method,
            "journal": "Fictional Journal", "jcr": {"status": "UNVERIFIED"},
            "identity_status": "CLEAR", "publication_status": "FORMAL"}


class WeeklyJournalIsolationTests(unittest.TestCase):
    def test_same_article_jcr_or_impact_factor_does_not_change_score_for_both_topics(self):
        for directory, title, abstract in (
            ("synthetic-topic", "coating durability with heat cycling and roof film",
             "Coating durability was measured using heat cycling on roof film."),
            ("synthetic-acoustic", "sound attenuation by resonance measurement",
             "Sound attenuation used resonance measurement for an interior panel.")):
            bundle = load_bundle(EXAMPLES / directory)
            base = Paper("SYN-1", title, abstract, "fixture", journal="Fictional Low Journal")
            high = Paper("SYN-1", title, abstract, "fixture", journal="Cell")
            first, second = recommend(base, bundle), recommend(high, bundle)
            self.assertEqual((first["base_score"], first["priority"], first["eligible"],
                              first["attention_ranking"]),
                             (second["base_score"], second["priority"], second["eligible"],
                              second["attention_ranking"]))

    def test_weekly_order_ignores_jcr_impact_and_flagship(self):
        rows = [{"paper_id": "SYN-A", "title": "Alpha", "priority": "HIGH",
                 "attention_rank_sort_key": (-1, -1), "attention_ranking": {"ordering": [0, -2]},
                 "journal": "Fictional Low", "jcr_quartile": "Q4", "impact_factor": 0.1},
                {"paper_id": "SYN-B", "title": "Beta", "priority": "HIGH",
                 "attention_rank_sort_key": (-1, 0), "attention_ranking": {"ordering": [0, -1]},
                 "journal": "Cell", "jcr_quartile": "Q1", "impact_factor": 200.0}]
        expected = [row["paper_id"] for row in _rank_weekly_rows(rows)]
        self.assertEqual(expected, ["SYN-A", "SYN-B"])
        rows[0].update(journal="Nature", jcr_quartile="Q1", impact_factor=900.0)
        rows[1].update(journal="Science", jcr_quartile="Q4", impact_factor=0.0)
        self.assertEqual([row["paper_id"] for row in _rank_weekly_rows(rows)], expected)

    def test_unrelated_high_impact_never_outranks_relevant_low_impact(self):
        bundle = load_bundle(EXAMPLES / "synthetic-topic")
        relevant = Paper("SYN-low", "coating durability heat cycling",
                         "Coating durability uses heat cycling and roof film.", "fixture",
                         journal="Fictional Low")
        irrelevant = Paper("SYN-high", "unrelated demonstration",
                           "An unrelated demonstration with no coating method.", "fixture",
                           journal="Nature")
        good, bad = recommend(relevant, bundle), recommend(irrelevant, bundle)
        self.assertTrue(good["eligible"])
        self.assertFalse(bad["eligible"])
        self.assertGreater(good["base_score"], bad["base_score"])

    def test_weekly_alert_does_not_print_journal_quality(self):
        rendered = render_alert({"paper": {"paper_id": "SYN-1", "title": "Fictional paper", "authors": []},
                                 "priority": "HIGH", "reason": {"evidence": []},
                                 "topic_id": "fictional-topic"})
        self.assertNotIn("Journal quality:", rendered)
        self.assertNotIn("JCR:", rendered)

    def test_weekly_run_ignores_registry_and_emits_no_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "private", intake())
            activate_weekly(config, skip_bootstrap=True, reason="Fictional test")
            report = run_weekly(config, root / "state.sqlite3", root / "report",
                                registry_path=root / "nonexistent-jcr.csv", now=NOW,
                                provider_names=["openalex"],
                                providers={"openalex": RichProvider("openalex", [[record(1)]])})
            self.assertEqual(report["status"], "SUCCESS")
            self.assertEqual(report["alerted"], 1)
            self.assertNotIn("journal_quality", report["events"][0])


class ScientificPoolTests(unittest.TestCase):
    def test_a_exact_species_and_historical_alias_only(self):
        row = candidate(1, "A")
        self.assertEqual(scientific_candidate_issues(row, target_species=TARGET), [])
        row["species"] = "Oldgenus alpha"
        self.assertEqual(scientific_candidate_issues(row, target_species=TARGET,
                            target_aliases=("Oldgenus alpha",)), [])
        row["species"] = "Imaginaryus beta"
        self.assertIn("NOT_EXACT_TARGET_SPECIES", scientific_candidate_issues(row,
                      target_species=TARGET, target_aliases=("Oldgenus alpha",)))

    def test_b_species_distance_has_no_direct_bonus_or_penalty(self):
        near = candidate(1, species="Imaginaryus beta")
        far = candidate(2, species="Remoteus gamma")
        result = allocate_candidate_pool([far, near], target_species=TARGET)
        self.assertEqual([row["paper_id"] for row in result["selected_B"]],
                         ["SYN-1", "SYN-2"])

    def test_b_requires_specific_evidence_technical_step_and_limits(self):
        row = candidate(1)
        for field in ("original_evidence", "technical_step", "limitations"):
            bad = dict(row, **{field: ""})
            self.assertIn("TRANSFER_EVIDENCE_INCOMPLETE",
                          scientific_candidate_issues(bad, target_species=TARGET))

    def test_candidate_pool_without_jcr_is_reviewable_not_low_quality(self):
        rows = [candidate(i, "A") for i in range(10)]
        rows += [candidate(i + 10, method=f"method-{i % 5}") for i in range(20)]
        result = allocate_candidate_pool(rows, target_species=TARGET)
        self.assertEqual(result["status"], "COMPLETE_REVIEW_POOL")
        self.assertEqual((len(result["selected_A"]), len(result["selected_B"])), (10, 20))
        self.assertTrue(all(row["jcr"]["status"] == "UNVERIFIED" for row in rows))
        strict = allocate_review(rows, target_species=TARGET)
        self.assertEqual(strict["status"], "BLOCKED")

    def test_study_key_preprint_and_formal_do_not_count_twice(self):
        rows = [candidate(1), candidate(2)]
        rows[0]["study_key"] = "SYN-shared"
        rows[1]["study_key"] = "SYN-shared"
        result = allocate_candidate_pool(rows, target_species=TARGET)
        self.assertEqual(len(result["selected_B"]), 1)
        self.assertTrue(any("DUPLICATE_STUDY" in row["issues"] for row in result["rejected"]))

    def test_identity_conflict_not_auto_merged_or_strictly_qualified(self):
        row = candidate(1)
        row["identity_status"] = "REVIEW_CONFLICT"
        pool = allocate_candidate_pool([row], target_species=TARGET)
        self.assertEqual(len(pool["selected_B"]), 1)
        self.assertEqual(row["identity_status"], "REVIEW_CONFLICT")
        strict = allocate_review([row], target_species=TARGET)
        self.assertEqual(len(strict["selected_B"]), 0)

    def test_a_b_quotas_independent_and_no_padding(self):
        rows = [candidate(i, "A") for i in range(9)]
        rows += [candidate(i + 10, method=f"method-{i % 5}") for i in range(21)]
        result = allocate_candidate_pool(rows, target_species=TARGET)
        self.assertEqual((len(result["selected_A"]), len(result["selected_B"])), (9, 20))
        self.assertEqual(result["gaps"], {"A": 1, "B": 0})
        self.assertEqual(result["status"], "PARTIAL_REVIEW_POOL")


if __name__ == "__main__":
    unittest.main()
