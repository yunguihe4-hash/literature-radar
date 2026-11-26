import json
import tempfile
import unittest
from pathlib import Path

from literature_radar.sources.fixture import FixturePageError, FixtureProvider


class FixtureProviderV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.json"
        self.path.write_text(json.dumps([
            {"source_id": f"SYN-{number}", "title": f"Imaginary sample {number}",
             "abstract": None if number == 1 else "synthetic material",
             "source": "synthetic-index", "identifiers": {"doi": "10.0000/fictional-fixture"} if number == 0 else {}}
            for number in range(3)
        ]), encoding="utf-8")
        self.topic = {"id": "synthetic-topic", "search_terms": ["Imaginary"],
                      "sources": ["synthetic-index"]}
        self.focus = {"version": "v001", "search_terms": []}

    def test_multiple_pages_and_last_page(self):
        provider = FixtureProvider(self.path, page_size=2)
        plan = provider.plan_queries(self.topic, self.focus)[0]
        first = provider.fetch_page(plan)
        second = provider.fetch_page(plan, first.next_cursor)
        self.assertEqual(len(first.records), 2)
        self.assertTrue(first.has_more)
        self.assertEqual(first.next_cursor, 2)
        self.assertEqual(len(second.records), 1)
        self.assertFalse(second.has_more)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(first.request_metadata.provider_id, "fixture")

    def test_page_failure_is_isolated_and_recoverable(self):
        plan = FixtureProvider(self.path).plan_queries(self.topic, self.focus)[0]
        failing = FixtureProvider(self.path, page_size=1, fail_on_pages={1})
        first = failing.fetch_page(plan)
        with self.assertRaises(FixturePageError):
            failing.fetch_page(plan, first.next_cursor)
        recovered = FixtureProvider(self.path, page_size=1).fetch_page(plan, first.next_cursor)
        self.assertEqual(recovered.records[0].payload["source_id"], "SYN-1")

    def test_missing_abstract_and_doi_and_provenance(self):
        provider = FixtureProvider(self.path, page_size=3)
        page = provider.fetch_page(provider.plan_queries(self.topic, self.focus)[0])
        record = provider.normalize(page.records[1])
        self.assertIsNone(record.normalized_metadata["abstract"])
        self.assertEqual(record.identifiers, {})
        self.assertEqual(record.provenance["abstract"].provider_id, "fixture")
        first = provider.normalize(page.records[0])
        self.assertEqual(first.provenance["identifiers.doi"].source_field, "identifiers.doi")

    def test_legacy_search_unchanged(self):
        provider = FixtureProvider(self.path)
        self.assertEqual(len(provider.search(["Imaginary"], "synthetic-index", 2)), 2)

    def test_cross_provider_fixture_records(self):
        one = FixtureProvider(self.path, provider_id="synthetic-one")
        two = FixtureProvider(self.path, provider_id="synthetic-two")
        first = one.normalize(one.fetch_page(one.plan_queries(self.topic, self.focus)[0]).records[0])
        second = two.normalize(two.fetch_page(two.plan_queries(self.topic, self.focus)[0]).records[0])
        self.assertEqual(first.source_id, second.source_id)
        self.assertNotEqual(first.provider_id, second.provider_id)
