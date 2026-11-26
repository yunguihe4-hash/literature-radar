import tempfile
import unittest
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.research.citation import expand_citations
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.research.opencitations import OpenCitationsProvider
from literature_radar.research.metadata import enrich_metadata
from literature_radar.research.review import DIMENSIONS, import_review
from literature_radar.research.report import render_review_report
from literature_radar.sources.crossref import CrossrefProvider
import sqlite3
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class CitationTests(unittest.TestCase):
    def test_opencitations_succeeds_when_openalex_fails_and_metadata_replaces_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            database = root / "academic.sqlite3"
            discover_incremental(config, ["openalex"], database_path=database,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": SyntheticProvider("openalex", [[fictional(
                                     "seed", openalex_id="https://openalex.org/W123")]])})
            seed = AcademicStore(database).topic_papers("fictional-alpha")[0]

            class FailClient:
                def get_json(self, *_args, **_kwargs):
                    raise TimeoutError("fictional OpenAlex outage")

            class CitationClient:
                def __init__(self):
                    self.calls = 0

                def get_json(self, _url):
                    self.calls += 1
                    return [{"cited": "omid:br/1 doi:10.0000/linked"}]

            cite_client = CitationClient()
            kwargs = {"paper_ids": [seed.paper_id], "direction": "references",
                      "provider_names": ["openalex", "opencitations"],
                      "evaluation_run_dir": root / "citation-review",
                      "provider": OpenAlexProvider(client=FailClient()),
                      "opencitations_provider": OpenCitationsProvider(client=cite_client)}
            first = expand_citations(config, database, **kwargs)
            self.assertEqual(first["status"], "PARTIAL_SUCCESS")
            self.assertEqual([item["status"] for item in first["operations"]],
                             ["FAILED", "SUCCESS"])
            self.assertEqual(len(first["candidate_paper_ids"]), 1)
            self.assertTrue((root / "citation-review/evaluation-request.json").is_file())
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM citation_edges").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM topic_paper_state").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_events").fetchone()[0], 1)
            second = expand_citations(config, database,
                                      **{key: value for key, value in kwargs.items()
                                         if key != "evaluation_run_dir"})
            self.assertEqual(second["operations"][1]["status"], "REUSED")
            self.assertEqual(cite_client.calls, 1)
            linked = next(paper for paper in AcademicStore(database).topic_papers("fictional-alpha")
                          if paper.doi == "10.0000/linked")
            self.assertTrue(linked.title.startswith("[Metadata unavailable;"))

            class MetadataClient:
                def get_json(self, _url):
                    return {"message": {"DOI": "10.0000/linked",
                                        "title": ["Fictional linked method"],
                                        "abstract": "Fictional linked abstract"}}

            self.assertEqual(enrich_metadata(config, database, paper_ids=[linked.paper_id],
                             provider=CrossrefProvider(client=MetadataClient()))["status"], "SUCCESS")
            refreshed = next(paper for paper in AcademicStore(database).topic_papers("fictional-alpha")
                             if paper.paper_id == linked.paper_id)
            self.assertEqual(refreshed.title, "Fictional linked method")

    def test_one_hop_checkpoint_and_identity_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            database = root / "academic.sqlite3"
            discovery = SyntheticProvider("openalex", [[fictional(
                "seed", openalex_id="https://openalex.org/W123")]])
            discover_incremental(config, ["openalex"], database_path=database,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": discovery})
            seed = AcademicStore(database).topic_papers("fictional-alpha")[0]

            class Client:
                def __init__(self):
                    self.calls = []
                def get_json(self, url, params):
                    self.calls.append((url, params))
                    if url.endswith("/W123"):
                        return {"referenced_works": ["https://openalex.org/W456"]}
                    return {"results": [{"id": "https://openalex.org/W456",
                                         "title": "Fictional cited method",
                                         "doi": "https://doi.org/10.0000/cited",
                                         "publication_date": "2026-01-03"}]}

            client = Client()
            adapter = OpenAlexProvider(client=client)
            first = expand_citations(config, database, paper_ids=[seed.paper_id],
                                     direction="references", provider=adapter)
            self.assertEqual(first["status"], "SUCCESS")
            self.assertEqual(first["operations"][0]["records"], 1)
            self.assertEqual(AcademicStore(database).count(), 2)
            calls = len(client.calls)
            second = expand_citations(config, database, paper_ids=[seed.paper_id],
                                      direction="references", provider=adapter)
            self.assertEqual(second["operations"][0]["status"], "REUSED")
            self.assertEqual(len(client.calls), calls)
            self.assertEqual(AcademicStore(database).count(), 2)

    def test_two_seeds_shared_related_paper_report_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            database = root / "academic.sqlite3"
            seeds = [fictional("seed-a", doi="10.0000/seed-a", title="Fictional seed A",
                              openalex_id="https://openalex.org/W123"),
                     fictional("seed-b", doi="10.0000/seed-b", title="Fictional seed B",
                              openalex_id="https://openalex.org/W124")]
            discover_incremental(config, ["openalex"], database_path=database,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": SyntheticProvider("openalex", [seeds])})
            seed_ids = [paper.paper_id for paper in AcademicStore(database).topic_papers("fictional-alpha")]

            class Client:
                def __init__(self):
                    self.calls = 0

                def get_json(self, url, params):
                    self.calls += 1
                    if url.endswith(("/W123", "/W124")):
                        return {"referenced_works": ["https://openalex.org/W456"]}
                    return {"results": [{"id": "https://openalex.org/W456",
                                         "title": "Fictional shared method",
                                         "doi": "https://doi.org/10.0000/shared",
                                         "publication_date": "2026-01-03"}]}

            client = Client()
            kwargs = dict(paper_ids=seed_ids, direction="references",
                          provider=OpenAlexProvider(client=client),
                          evaluation_run_dir=root / "citation-review")
            first = expand_citations(config, database, **kwargs)
            self.assertEqual(first["status"], "SUCCESS")
            self.assertEqual(len(first["candidate_paper_ids"]), 1)
            self.assertEqual(len(first["multi_seed"]), 1)
            self.assertEqual(first["multi_seed"][0]["seed_paper_ids"], sorted(seed_ids))
            self.assertEqual(first["multi_seed"][0]["seed_count"], 2)
            request = __import__("json").loads((root / "citation-review/evaluation-request.json").read_text())
            self.assertEqual(request["mode_context"]["multi_seed"], first["multi_seed"])
            linked_id = first["candidate_paper_ids"][0]
            response = {"paper_id": linked_id, "topic_id": "fictional-alpha",
                        "focus_version": request["focus_version"],
                        **{name: 80 for name in DIMENSIONS},
                        "recommendation": "recommended", "recommendation_reason": "Fictional method",
                        "chinese_summary": "虚构共享方法", "key_methods": ["fictional"],
                        "key_findings": ["fictional"], "direct_relevance": "method transfer",
                        "limitations": "fictional only", "reading_priority": "high"}
            reply = root / "human.json"
            reply.write_text(__import__("json").dumps([response]))
            import_review(root / "citation-review", reply)
            report = root / "citation-report.md"
            render_review_report(root / "citation-review", report)
            self.assertIn("多Seed共同连接论文", report.read_text())
            self.assertIn("seed_count=2", report.read_text())
            calls = client.calls
            second = expand_citations(config, database,
                                      **{key: value for key, value in kwargs.items()
                                         if key != "evaluation_run_dir"})
            self.assertEqual(second["operations"][0]["status"], "REUSED")
            self.assertEqual(second["multi_seed"], first["multi_seed"])
            self.assertEqual(client.calls, calls)
