"""Original four-file benchmark contract on an isolated Topic database."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature_radar.discovery.incremental import discover_incremental
from literature_radar.research.recall_benchmark import (
    CATEGORIES, evaluate_benchmark, load_benchmark_file)
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


def _seed(category, title, doi=None, pmid=None):
    quoted = lambda value: "null" if value is None else json.dumps(value)
    return ("seed_papers:\n"
            f"  - title: {quoted(title)}\n"
            f"    doi: {quoted(doi)}\n"
            f"    pmid: {quoted(pmid)}\n"
            f"    category: {quoted(category)}\n"
            '    reason: "fictional benchmark"\n'
            '    importance: "fictional priority"\n'
            '    keywords:\n      - "fictional"\n')


class RecallBenchmarkTests(unittest.TestCase):
    def test_four_category_report_and_topic_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = initialize(root / "config", intake("fictional-alpha"))
            second = initialize(root / "config", intake("fictional-beta"))
            db = root / "academic.sqlite3"
            discover_incremental(first, ["openalex"], database_path=db,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": SyntheticProvider("openalex", [[
                                     fictional("first", doi="10.0000/one", pmid="123")]])})
            discover_incremental(second, ["openalex"], database_path=db,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": SyntheticProvider("openalex", [[
                                     fictional("second", doi="10.0000/two",
                                               title="Unrelated second Topic")]])})
            benchmark = root / "benchmark"
            benchmark.mkdir()
            cases = {
                "methods": ("Imaginary acoustic material", "https://doi.org/10.0000/ONE", None),
                "resources": ("Imaginary acoustic material", None, None),
                "species": ("Unrelated second Topic", "10.0000/two", None),
                "applications": ("Imaginary acoustic material", None, "123")}
            for category in CATEGORIES:
                (benchmark / f"{category}.yaml").write_text(_seed(category, *cases[category]))
            output = root / "audit/report.json"
            report = evaluate_benchmark(first, db, benchmark, output)
            self.assertEqual((report["total_papers"], report["found"], report["missing"]),
                             (4, 3, 1))
            self.assertEqual(report["scope"], "topic")
            self.assertEqual([row["match_method"] for row in report["papers"]],
                             ["doi", "normalized_title", None, "pmid"])
            self.assertEqual(json.loads(output.read_text())["by_category"]["species"]["missing"], 1)
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                evaluate_benchmark(first, db, benchmark, output)

    def test_strict_parser_rejects_unquoted_and_wrong_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "methods.yaml"
            path.write_text(_seed("methods", "Fictional paper").replace(
                'title: "Fictional paper"', 'title: Fictional paper'))
            with self.assertRaisesRegex(ValueError, "quoted strings"):
                load_benchmark_file(path, "methods")
            path.write_text(_seed("species", "Fictional paper"))
            with self.assertRaisesRegex(ValueError, "category mismatch"):
                load_benchmark_file(path, "methods")

    def test_read_only_benchmark_accepts_pre_specialty_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            db = root / "legacy.sqlite3"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE papers_v2 (paper_id TEXT PRIMARY KEY, payload TEXT)")
                connection.execute("CREATE TABLE topic_paper_state (paper_id TEXT, topic_id TEXT)")
                connection.execute("INSERT INTO papers_v2 VALUES (?,?)",
                                   ("paper-one", json.dumps({"title": "Imaginary legacy paper",
                                                               "identifiers": {"doi": "10.0000/legacy"}})))
                connection.execute("INSERT INTO topic_paper_state VALUES (?,?)",
                                   ("paper-one", "fictional-alpha"))
            benchmark = root / "benchmark"
            benchmark.mkdir()
            (benchmark / "methods.yaml").write_text(_seed(
                "methods", "Imaginary legacy paper", "10.0000/legacy"))
            for category in CATEGORIES[1:]:
                (benchmark / f"{category}.yaml").write_text("seed_papers:\n")
            result = evaluate_benchmark(config, db, benchmark, root / "result.json")
            self.assertEqual((result["total_papers"], result["found"]), (1, 1))
            with sqlite3.connect(db) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='specialty_topic_papers'").fetchone())
