import unittest
from urllib.parse import parse_qs, urlsplit
from literature_radar.providers.models import DateWindow
from literature_radar.sources.http import HTTPResponse, RequestFailure
from literature_radar.sources.openalex import OpenAlexProvider
from tests.mock_api import client

TOPIC = {"id": "synthetic-topic", "search_terms": ["imaginary"], "sources": ["openalex"]}
FOCUS = {"version": "v001", "search_terms": ["acoustic"]}
RECORD = {"id": "https://openalex.org/W123", "title": "Imaginary acoustic paper",
          "doi": "https://doi.org/10.0000/fictional",
          "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123", "pmcid": "PMC123"},
          "abstract_inverted_index": {"Synthetic": [0], "abstract": [1]},
          "authorships": [{"author": {"display_name": "Imaginary Author"}}],
          "primary_location": {"source": {"display_name": "Fictional Journal"}},
          "publication_date": "2026-01-01", "created_date": "2026-01-02",
          "updated_date": "2026-01-03", "type": "article", "cited_by_count": 2}


class OpenAlexTests(unittest.TestCase):
    def test_mapping_pagination_and_provenance(self):
        http, fake = client("openalex", [
            {"results": [RECORD], "meta": {"count": 2}},
            {"results": [dict(RECORD, id="https://openalex.org/W124")], "meta": {"count": 2}},
        ])
        provider = OpenAlexProvider(http, page_size=1)
        query = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))[0]
        page = provider.fetch_page(query)
        record = provider.normalize(page.records[0])
        self.assertTrue(page.has_more)
        self.assertFalse(provider.fetch_page(query, page.next_cursor).has_more)
        self.assertEqual(record.identifiers["openalex_id"], RECORD["id"])
        self.assertEqual(record.normalized_metadata["abstract"], "Synthetic abstract")
        self.assertEqual(record.provenance["abstract"].source_field, "abstract_inverted_index")
        self.assertIn("from_publication_date", fake.calls[0][0])
        self.assertNotIn("from_updated_date", fake.calls[0][0])
        first_params = parse_qs(urlsplit(fake.calls[0][0]).query)
        second_params = parse_qs(urlsplit(fake.calls[1][0]).query)
        self.assertNotEqual(first_params.pop("page"), second_params.pop("page"))
        self.assertEqual(first_params, second_params)

    def test_empty_and_missing_fields(self):
        http, _ = client("openalex", [{"results": [], "meta": {"next_cursor": None}}])
        provider = OpenAlexProvider(http)
        self.assertFalse(provider.fetch_page(provider.plan_queries(TOPIC, FOCUS)[0]).has_more)
        missing = dict(RECORD, doi=None, abstract_inverted_index=None, authorships=[])
        record = provider.normalize(type("R", (), {"provider_id": "openalex", "payload": missing,
                                                 "retrieved_at": "2026-01-01T00:00:00+00:00"})())
        self.assertIsNone(record.normalized_metadata["abstract"])
        self.assertEqual(record.normalized_metadata["authors"], [])

    def test_errors_and_anonymous_cursor(self):
        query = OpenAlexProvider().plan_queries(TOPIC, FOCUS)[0]
        for response, category in ((HTTPResponse(429, {}, b""), "rate_limited"),
                                   (HTTPResponse(500, {}, b""), "server_error"),
                                   (HTTPResponse(200, {}, b"{"), "invalid_json"),
                                   (TimeoutError(), "timeout")):
            with self.subTest(category=category):
                http, _ = client("openalex", [response])
                with self.assertRaises(RequestFailure) as raised:
                    OpenAlexProvider(http).fetch_page(query)
                self.assertEqual(raised.exception.category, category)
        http, fake = client("openalex", [{"results": [RECORD], "meta": {"count": 3}}])
        provider = OpenAlexProvider(http, page_size=1)
        self.assertEqual(provider.fetch_page(provider.plan_queries(TOPIC, FOCUS)[0], 2).next_cursor, 3)
        self.assertIn("page=2", fake.calls[0][0])

    def test_empty_page_before_reported_total_fails(self):
        http, _ = client("openalex", [{"results": [], "meta": {"count": 5}}])
        query = OpenAlexProvider(page_size=1).plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            OpenAlexProvider(http, page_size=1).fetch_page(query)
        self.assertEqual(raised.exception.category, "incomplete_page")

    def test_basic_paging_limit_does_not_claim_complete(self):
        http, _ = client("openalex", [{"results": [RECORD], "meta": {"count": 10001}}])
        provider = OpenAlexProvider(http, page_size=25)
        query = provider.plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            provider.fetch_page(query, 400)
        self.assertEqual(raised.exception.category, "basic_paging_limit")
