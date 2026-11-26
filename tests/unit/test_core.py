import tempfile
import unittest
from pathlib import Path
from literature_radar.models import Paper
from literature_radar.candidates.processing import deduplicate
from literature_radar.evaluation.engine import score_evidence
from literature_radar.feedback.store import add_feedback

class CoreTests(unittest.TestCase):
    def test_duplicate_identity_prefers_more_complete_abstract(self):
        a = Paper("one", "Synthetic Paper", "short", "fixture", {"doi": "10.0000/synthetic"})
        b = Paper("two", "Synthetic Paper", "longer abstract", "other", {"doi": "10.0000/synthetic"})
        self.assertEqual(deduplicate([a, b]), [b])

    def test_negative_evidence_reduces_score(self):
        paper = Paper("x", "Synthetic coating", "unrelated demonstration", "fixture")
        score, evidence = score_evidence(paper, {"positive": [{"term":"coating","dimension":"general","weight":2}],"negative":[{"term":"unrelated","dimension":"general","weight":3}]})
        self.assertEqual(score, -1)
        self.assertEqual(len(evidence), 2)

    def test_feedback_is_local_and_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback.json"
            result = add_feedback(path, "synthetic", "source:fixture:one", "keep", "Synthetic test decision")
            self.assertEqual(result["decision"], "keep")
            self.assertTrue(path.exists())
            with self.assertRaisesRegex(ValueError, "invalid feedback decision"):
                add_feedback(path, "synthetic", "source:fixture:one", "invalid", "reason")
