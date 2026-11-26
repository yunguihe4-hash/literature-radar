"""Stage 5.2 specialty report contract; synthetic discovery and disposable data."""
import json
import tempfile
import unittest
from pathlib import Path

from literature_radar.research.report import render_review_report
from literature_radar.research.review import DIMENSIONS, import_review
from literature_radar.research.specialty import run_specialty
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class SpecialtyContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        self.database = self.root / "academic.sqlite3"

    def _review(self, run_dir: Path) -> None:
        request = json.loads((run_dir / "evaluation-request.json").read_text())
        paper_id = request["candidates"][0]["paper_id"]
        evaluation = {"paper_id": paper_id, "topic_id": request["topic_id"],
                      "focus_version": request["focus_version"],
                      **{name: 80 for name in DIMENSIONS},
                      "recommendation": "recommended", "recommendation_reason": "Fictional method transfer",
                      "chinese_summary": "虚构摘要", "key_methods": ["fictional assay"],
                      "key_findings": ["fictional finding"], "direct_relevance": "method transfer",
                      "limitations": "synthetic only", "reading_priority": "high"}
        answer = self.root / "human.json"
        answer.write_text(json.dumps([evaluation]))
        import_review(run_dir, answer)

    def test_targeted_report_keeps_executed_query_and_window(self):
        term = json.loads((self.config / "topic.json").read_text())["search_terms"][0]
        run_dir = self.root / "targeted-run"
        result = run_specialty(self.config, self.database, mode="targeted", term=term,
                               since="2026-01-01", until="2026-01-07",
                               provider_names=["openalex"],
                               providers={"openalex": SyntheticProvider("openalex", [[fictional("report")]])},
                               evaluation_run_dir=run_dir)
        self.assertIsNotNone(result["evaluation"])
        snapshot = json.loads((run_dir / "snapshot.json").read_text())
        self.assertEqual(snapshot["mode_context"]["window"],
                         {"start": "2026-01-01", "end": "2026-01-07"})
        self.assertEqual(snapshot["mode_context"]["sources"], ["openalex"])
        self.assertEqual(snapshot["mode_context"]["query_variants"][0]["terms"], [term])
        self._review(run_dir)
        output = self.root / "report.md"
        render_review_report(run_dir, output)
        rendered = output.read_text()
        self.assertIn("检索窗口：2026-01-01 至 2026-01-07", rendered)
        self.assertIn("实际查询组", rendered)
        self.assertIn("不能据此推断新发表论文数", rendered)
        self.assertEqual(render_review_report(run_dir, output)["status"], "REPORT_READY")

    def test_report_rejects_mismatched_selected_identity(self):
        run_dir = self.root / "run"
        run_dir.mkdir()
        snapshot = {"run_id": "run", "sha256": "s", "mode": "targeted",
                    "topic": {"display_name": "Fictional"}, "topic_id": "fictional",
                    "focus_version": "v1", "candidates": [{"paper_id": "p1"}]}
        result = {"run_id": "run", "source_snapshot_sha256": "s",
                  "evaluations": [{"paper_id": "p1"}], "selected_paper_ids": ["foreign"]}
        (run_dir / "snapshot.json").write_text(json.dumps(snapshot))
        (run_dir / "evaluations.json").write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "mismatched"):
            render_review_report(run_dir, self.root / "report.md")


if __name__ == "__main__":
    unittest.main()
