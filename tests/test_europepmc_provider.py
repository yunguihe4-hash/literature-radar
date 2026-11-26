import unittest
from urllib.parse import parse_qs, urlsplit
from literature_radar.providers.models import DateWindow
from literature_radar.sources.europepmc import EuropePMCProvider
from literature_radar.sources.http import HTTPResponse, RequestFailure
from tests.mock_api import client

TOPIC = {"id": "synthetic-topic", "search_terms": ["imaginary"], "sources": ["europepmc"]}
FOCUS = {"version": "v001", "search_terms": []}
RECORD = {"source": "MED", "id": "123", "title": "Imaginary paper",
          "abstractText": "Fictional abstract", "doi": "10.0000/fictional",
          "pmid": "123", "pmcid": "PMC123", "authorList": {"author": [{"fullName": "Imaginary Author"}]},
          "journalTitle": "Synthetic Journal", "firstPublicationDate": "2026-01-01",
          "creationDate": "2026-01-02", "updateDate": "2026-01-03", "pubType": "Article"}


class EuropePMCTests(unittest.TestCase):
    def test_core_mapping_and_cursor(self):
        http, fake = client("europepmc", [{"resultList": {"result": [RECORD]}, "nextCursorMark": "next"},
                                           {"resultList": {"result": []}, "nextCursorMark": "next"}])
        provider = EuropePMCProvider(http, page_size=1)
        query = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))[0]
        first = provider.fetch_page(query)
        self.assertTrue(first.has_more)
        self.assertFalse(provider.fetch_page(query, first.next_cursor).has_more)
        self.assertIn("UPDATE_DATE", fake.calls[0][0])
        first_params = parse_qs(urlsplit(fake.calls[0][0]).query)
        second_params = parse_qs(urlsplit(fake.calls[1][0]).query)
        self.assertNotEqual(first_params.pop("cursorMark"), second_params.pop("cursorMark"))
        self.assertEqual(first_params, second_params)
        record = provider.normalize(first.records[0])
        self.assertEqual(record.identifiers["pmcid"], "PMC123")
        self.assertEqual(record.provenance["abstract"].source_field, "abstract")

    def test_lite_and_missing_metadata(self):
        topic = dict(TOPIC, extensions={"provider_options": {"europepmc": {"result_type": "lite"}}})
        query = EuropePMCProvider().plan_queries(topic, FOCUS)[0]
        self.assertEqual(query.query_parameters["result_type"], "lite")
        raw = type("R", (), {"provider_id": "europepmc", "payload": dict(RECORD, abstractText=None, doi=None, authorList={}),
                             "retrieved_at": "2026-01-01T00:00:00+00:00"})()
        record = EuropePMCProvider().normalize(raw)
        self.assertIsNone(record.normalized_metadata["abstract"])
        self.assertNotIn("doi", record.identifiers)

    def test_errors(self):
        query = EuropePMCProvider().plan_queries(TOPIC, FOCUS)[0]
        for response, category in ((HTTPResponse(429, {}, b""), "rate_limited"),
                                   (HTTPResponse(500, {}, b""), "server_error"),
                                   (HTTPResponse(200, {}, b"{"), "invalid_json"),
                                   (TimeoutError(), "timeout")):
            with self.subTest(category=category):
                http, _ = client("europepmc", [response])
                with self.assertRaises(RequestFailure) as raised:
                    EuropePMCProvider(http).fetch_page(query)
                self.assertEqual(raised.exception.category, category)

    def test_short_page_with_advancing_cursor_is_not_complete(self):
        http, _ = client("europepmc", [{"resultList": {"result": [RECORD]},
                                         "nextCursorMark": "next"}])
        provider = EuropePMCProvider(http, page_size=25)
        query = provider.plan_queries(TOPIC, FOCUS)[0]
        page = provider.fetch_page(query)
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, "next")
