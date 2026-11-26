import tempfile
import unittest
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.research.metadata import enrich_metadata
from literature_radar.sources.crossref import CrossrefProvider
from literature_radar.sources.europepmc import EuropePMCProvider
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.models import SourceRecord
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class MetadataTests(unittest.TestCase):
    def test_europepmc_identifier_lookup_uses_original_identity_query(self):
        calls = []

        class Client:
            def get_json(self, endpoint, params):
                calls.append((endpoint, params))
                return {"resultList": {"result": [{"source": "MED", "id": "12345",
                                                   "pmid": "12345", "title": "Fictional title"}]}}

        record = EuropePMCProvider(client=Client()).fetch_by_identifier("12345", kind="pmid")
        self.assertEqual(calls[0][1]["query"], "EXT_ID:12345 AND SRC:MED")
        self.assertEqual(record.identifiers["pmid"], "12345")

    def test_openalex_doi_lookup_is_identifier_filtered(self):
        calls = []

        class Client:
            def get_json(self, endpoint, params=None, headers=None):
                calls.append((endpoint, params))
                return {"results": [{"id": "https://openalex.org/W12345",
                                     "doi": "https://doi.org/10.0000/example",
                                     "title": "Fictional title"}]}

        record = OpenAlexProvider(client=Client()).fetch_by_identifier("10.0000/example")
        self.assertEqual(calls[0][1]["filter"], "doi:10.0000/example")
        self.assertEqual(record.identifiers["doi"], "https://doi.org/10.0000/example")

    def test_identifier_recovery_is_idempotent_and_does_not_discover_new_paper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            database = root / "academic.sqlite3"
            source = SyntheticProvider("openalex", [[fictional("seed")]])
            discover_incremental(config, ["openalex"], database_path=database,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": source})
            paper = AcademicStore(database).topic_papers("fictional-alpha")[0]

            class Client:
                def __init__(self):
                    self.calls = 0
                def get_json(self, url):
                    self.calls += 1
                    return {"message": {"DOI": "10.0000/fictional",
                                        "title": ["Imaginary acoustic material"],
                                        "abstract": "Fictional enriched abstract",
                                        "published": {"date-parts": [[2026, 1, 3]]}}}

            client = Client()
            adapter = CrossrefProvider(client=client)
            first = enrich_metadata(config, database, paper_ids=[paper.paper_id], provider=adapter)
            self.assertEqual(first["status"], "SUCCESS")
            self.assertEqual(AcademicStore(database).count(), 1)
            second = enrich_metadata(config, database, paper_ids=[paper.paper_id], provider=adapter)
            self.assertEqual(second["results"][0]["status"], "REUSED")
            self.assertEqual(client.calls, 1)

    def test_four_source_lookup_isolated_and_checkpointed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-alpha"))
            database = root / "academic.sqlite3"
            seed = fictional("seed", doi="10.0000/multi", pmid="12345",
                             pmcid="PMC12345", openalex_id="W12345", abstract=None)
            discover_incremental(config, ["openalex"], database_path=database,
                                 since="2026-01-01", until="2026-01-07",
                                 providers={"openalex": SyntheticProvider("openalex", [[seed]])})
            paper = AcademicStore(database).topic_papers("fictional-alpha")[0]

            class Lookup:
                def __init__(self, name, fail=False):
                    self.name, self.fail, self.calls = name, fail, 0

                def fetch_by_identifier(self, _identifier, **_kwargs):
                    self.calls += 1
                    if self.fail:
                        raise TimeoutError("fictional lookup timeout")
                    key = {"crossref": "doi", "pubmed": "pmid",
                           "openalex": "openalex_id", "europepmc": "pmid"}[self.name]
                    value = {"doi": "10.0000/multi", "pmid": "12345",
                             "openalex_id": "W12345"}[key]
                    return SourceRecord(self.name, self.name + "-record",
                                        "2026-01-02T00:00:00+00:00", {key: value},
                                        {"title": "Imaginary acoustic material",
                                         "abstract": "Fictional recovered abstract"})

            providers = {name: Lookup(name, name == "crossref") for name in
                         ("crossref", "pubmed", "openalex", "europepmc")}
            first = enrich_metadata(config, database, paper_ids=[paper.paper_id],
                                    providers=providers)
            self.assertEqual(first["status"], "PARTIAL_SUCCESS")
            self.assertEqual([item["status"] for item in first["results"]],
                             ["FAILED", "SUCCESS", "SUCCESS", "SUCCESS"])
            self.assertEqual(AcademicStore(database).count(), 1)
            providers["crossref"].fail = False
            second = enrich_metadata(config, database, paper_ids=[paper.paper_id],
                                     providers=providers)
            self.assertEqual([item["status"] for item in second["results"]],
                             ["SUCCESS", "REUSED", "REUSED", "REUSED"])
            self.assertEqual([providers[name].calls for name in providers], [2, 1, 1, 1])
            self.assertEqual(AcademicStore(database).count(), 1)
