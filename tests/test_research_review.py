"""Offline strict evaluation exchange; all files and DB are disposable."""
import json
import sqlite3
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.research.review import (DIMENSIONS, EvaluationValidationError,
                                               calculate_score, import_review,
                                               prepare_review, prepare_reevaluation,
                                               scoring_weights)
from literature_radar.research.specialty import run_specialty
from literature_radar.research.knowledge import export_knowledge
from literature_radar.research.report import render_review_report
from literature_radar.topics.manager import initialize
from literature_radar.cli import main as cli_main
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class ResearchReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        self.database = self.root / "academic.sqlite3"
        topic = json.loads((self.config / "topic.json").read_text())
        run_specialty(self.config, self.database, mode="targeted",
                      term=topic["search_terms"][0], since="2026-01-01", until="2026-01-07",
                      provider_names=["openalex"],
                      providers={"openalex": SyntheticProvider("openalex", [[fictional("review")]])})
        self.paper_id = AcademicStore(self.database).topic_papers("fictional-alpha")[0].paper_id

    def _item(self, request):
        return {"paper_id": self.paper_id, "topic_id": request["topic_id"],
                "focus_version": request["focus_version"],
                **{name: 80 for name in DIMENSIONS},
                "recommendation": "recommended", "recommendation_reason": "Fictional relevant method",
                "chinese_summary": "虚构方法摘要", "key_methods": ["fictional assay"],
                "key_findings": ["fictional finding"], "direct_relevance": "method transfer",
                "limitations": "fictional only", "reading_priority": "high"}

    def test_original_six_dimensions_and_immutable_human_exchange(self):
        run_dir = self.root / "research-run"
        result = prepare_review(self.config, self.database, run_dir,
                                paper_ids=[self.paper_id], mode="targeted")
        self.assertEqual(result["candidate_count"], 1)
        request = json.loads((run_dir / "evaluation-request.json").read_text())
        self.assertEqual(request["weights"], scoring_weights({}, "targeted"))
        self.assertEqual(calculate_score(self._item(request), request["weights"]), 80)
        self.assertFalse((run_dir / "evaluations.json").exists())
        reply = self.root / "human.json"
        reply.write_text(json.dumps([self._item(request)]))
        imported = import_review(run_dir, reply)
        self.assertEqual(imported["selected_count"], 1)
        artifact = json.loads((run_dir / "evaluations.json").read_text())
        self.assertEqual(artifact["status"], "EVALUATED_AWAITING_FINAL_HUMAN_REVIEW")
        self.assertFalse(artifact["knowledge_exported"])
        self.assertEqual(import_review(run_dir, reply), imported)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM research_evaluations WHERE topic_id=? AND run_id=?",
                ("fictional-alpha", run_dir.name)).fetchone()[0], 1)
        report_path = self.root / "report.md"
        self.assertEqual(render_review_report(run_dir, report_path)["selected_count"], 1)
        self.assertEqual(render_review_report(run_dir, report_path)["status"], "REPORT_READY")
        self.assertIn("虚构方法摘要", report_path.read_text())
        self.assertEqual(AcademicStore(self.database).count(), 1)

    def test_invalid_identity_score_and_duplicate_are_rejected_before_write(self):
        run_dir = self.root / "research-run"
        prepare_review(self.config, self.database, run_dir, paper_ids=[self.paper_id])
        request = json.loads((run_dir / "evaluation-request.json").read_text())
        reply = self.root / "human.json"
        for replacement in ({"focus_version": "v999"}, {"topic_relevance": True},
                            {"paper_id": "foreign"}):
            reply.write_text(json.dumps([{**self._item(request), **replacement}]))
            with self.assertRaises(EvaluationValidationError):
                import_review(run_dir, reply)
            self.assertFalse((run_dir / "evaluations.json").exists())
        reply.write_text(json.dumps([self._item(request), self._item(request)]))
        with self.assertRaises(EvaluationValidationError):
            import_review(run_dir, reply)

    def test_knowledge_export_requires_confirmation_and_preserves_notes(self):
        run_dir = self.root / "research-run"
        prepare_review(self.config, self.database, run_dir, paper_ids=[self.paper_id])
        request = json.loads((run_dir / "evaluation-request.json").read_text())
        reply = self.root / "human.json"
        reply.write_text(json.dumps([self._item(request)]))
        import_review(run_dir, reply)
        output = self.root / "knowledge"
        with self.assertRaisesRegex(ValueError, "confirmation"):
            export_knowledge(run_dir, output)
        self.assertFalse(output.exists())
        first = export_knowledge(run_dir, output, confirmed=True, build_graph=True)
        self.assertEqual(len(first["written"]), 1)
        self.assertTrue(Path(first["graph"]).is_file())
        note = Path(first["written"][0])
        note.write_text(note.read_text() + "\nHuman annotation remains.\n")
        again = export_knowledge(run_dir, output, confirmed=True, build_graph=True)
        self.assertEqual(again["written"], [])
        self.assertEqual(again["preserved"], [str(note)])
        self.assertIn("Human annotation remains.", note.read_text())

    def test_metadata_recovery_creates_independent_citation_revision(self):
        from literature_radar.discovery.incremental import discover_incremental
        from literature_radar.research.metadata import enrich_metadata
        from literature_radar.sources.crossref import CrossrefProvider
        root = self.root / "reevaluation"
        config = initialize(root / "config", intake("fictional-beta"))
        database = root / "academic.sqlite3"
        discover_incremental(config, ["openalex"], database_path=database,
                             since="2026-01-01", until="2026-01-07",
                             providers={"openalex": SyntheticProvider("openalex", [[
                                 fictional("metadata-only", doi="10.0000/revision", abstract=None)]])})
        paper_id = AcademicStore(database).topic_papers("fictional-beta")[0].paper_id
        old_dir = root / "old-citation"
        prepare_review(config, database, old_dir, paper_ids=[paper_id], mode="citation-expand")
        old_request = json.loads((old_dir / "evaluation-request.json").read_text())
        old_reply = root / "old-human.json"
        old_item = {"paper_id": paper_id, "topic_id": old_request["topic_id"],
                    "focus_version": old_request["focus_version"],
                    **{name: 60 for name in DIMENSIONS},
                    "recommendation": "monitor", "recommendation_reason": "metadata only",
                    "chinese_summary": "元数据", "key_methods": [], "key_findings": [],
                    "direct_relevance": "uncertain", "limitations": "no abstract",
                    "reading_priority": "low"}
        old_reply.write_text(json.dumps([old_item]))
        import_review(old_dir, old_reply)
        old_bytes = (old_dir / "evaluations.json").read_bytes()

        class Client:
            def get_json(self, _url):
                return {"message": {"DOI": "10.0000/revision",
                                    "title": ["Imaginary acoustic material"],
                                    "abstract": "Fictional recovered abstract",
                                    "published": {"date-parts": [[2026, 1, 3]]}}}

        self.assertEqual(enrich_metadata(config, database, paper_ids=[paper_id],
                         provider=CrossrefProvider(client=Client()))["status"], "SUCCESS")
        new_dir = root / "new-citation"
        planned = prepare_reevaluation(old_dir, config, database, new_dir)
        self.assertEqual(planned["candidate_count"], 1)
        new_request = json.loads((new_dir / "evaluation-request.json").read_text())
        self.assertEqual(new_request["lineage"]["source_run_id"], old_dir.name)
        new_item = {**old_item, **{name: 90 for name in DIMENSIONS},
                    "recommendation": "recommended"}
        new_reply = root / "new-human.json"
        new_reply.write_text(json.dumps([new_item]))
        import_review(new_dir, new_reply)
        changes = json.loads((new_dir / "score-changes.json").read_text())
        self.assertEqual(changes["changes"][0]["score_change"], 30)
        self.assertEqual((old_dir / "evaluations.json").read_bytes(), old_bytes)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM research_evaluations WHERE topic_id=?",
                ("fictional-beta",)).fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT old_score,new_score FROM research_evaluation_revisions "
                "WHERE topic_id=? AND reevaluation_run_id=?",
                ("fictional-beta", new_dir.name)).fetchone(), (60, 90))

    def test_cli_research_review_to_confirmed_knowledge(self):
        run_dir = self.root / "cli-run"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["research", "prepare-evaluation",
                                       "--config-dir", str(self.config),
                                       "--database", str(self.database),
                                       "--run-dir", str(run_dir),
                                       "--paper-id", self.paper_id]), 0)
        request = json.loads((run_dir / "evaluation-request.json").read_text())
        reply = self.root / "cli-human.json"
        reply.write_text(json.dumps([self._item(request)]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["research", "import-evaluation",
                                       "--run-dir", str(run_dir),
                                       "--input", str(reply)]), 0)
            self.assertEqual(cli_main(["research", "render-report",
                                       "--run-dir", str(run_dir),
                                       "--output", str(self.root / "cli-report.md")]), 0)
            self.assertEqual(cli_main(["research", "export-knowledge",
                                       "--run-dir", str(run_dir),
                                       "--output-root", str(self.root / "cli-knowledge"),
                                       "--confirm", "--build-graph"]), 0)
        self.assertTrue((self.root / "cli-report.md").is_file())


if __name__ == "__main__":
    unittest.main()
