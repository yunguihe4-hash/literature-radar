import unittest
from literature_radar.providers.models import DateWindow
from literature_radar.sources.biorxiv import BioRxivProvider
from literature_radar.sources.http import HTTPResponse, RequestFailure
from tests.mock_api import client

TOPIC = {"id": "synthetic-topic", "search_terms": ["imaginary"]}
FOCUS = {"version": "v001", "search_terms": []}
DETAIL = {"doi": "10.0000/preprint", "title": "Imaginary acoustics", "abstract": "Fictional sound",
          "authors": "A Author; B Author", "date": "2026-01-02", "version": "2",
          "license": "CC-BY", "category": "fictional", "published": "10.0000/published"}

class BioRxivTests(unittest.TestCase):
    def test_details_pubs_filter_versions_and_offset(self):
        http, fake = client("biorxiv", [{"collection": [DETAIL, {"doi": "10.0000/other", "title": "Unrelated"}],
                                        "messages": [{"count": 2, "total": "3"}]},
                                       {"collection": [{"doi": "10.0000/last", "title": "Unrelated"}],
                                        "messages": [{"count": 1, "total": "3"}]},
                                       {"collection": [{"biorxiv_doi": "10.0000/preprint", "published_doi": "10.0000/published",
                                                        "preprint_title": "Imaginary acoustics", "preprint_date": "2026-01-02"}],
                                        "messages": [{"count": 1, "total": "1"}]}])
        provider = BioRxivProvider(http)
        details, pubs = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))
        page = provider.fetch_page(details)
        self.assertEqual(len(page.records), 1)
        self.assertTrue(page.has_more)
        record = provider.normalize(page.records[0])
        self.assertEqual(record.normalized_metadata["version"], "2")
        self.assertEqual(record.normalized_metadata["published_doi"], "10.0000/published")
        self.assertEqual(record.provenance["abstract"].provider_id, "biorxiv")
        self.assertFalse(provider.fetch_page(details, page.next_cursor).has_more)
        self.assertIn("/2", fake.calls[1][0])
        self.assertEqual(fake.calls[0][0].rsplit("/", 2)[0], fake.calls[1][0].rsplit("/", 2)[0])
        pub = provider.normalize(provider.fetch_page(pubs).records[0])
        self.assertEqual(pub.normalized_metadata["published_doi"], "10.0000/published")

    def test_real_response_pagination_uses_total_not_page_count(self):
        first = [dict(DETAIL, doi=f"10.0000/fictional-{index}") for index in range(30)]
        second = [dict(DETAIL, doi="10.0000/fictional-30")]
        http, fake = client("biorxiv", [
            {"collection": first, "messages": [{"cursor": 0, "count": 30, "total": "31"}]},
            {"collection": second, "messages": [{"cursor": 30, "count": 1, "total": "31"}]},
        ])
        provider = BioRxivProvider(http)
        query = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))[0]
        page1 = provider.fetch_page(query)
        self.assertTrue(page1.has_more)
        self.assertEqual(page1.next_cursor, 30)
        page2 = provider.fetch_page(query, page1.next_cursor)
        self.assertFalse(page2.has_more)
        self.assertEqual(len(page2.records), 1)
        self.assertIn("/30/json", fake.calls[1][0])

    def test_pubs_live_shape_uses_preprint_doi(self):
        http, _ = client("biorxiv", [{"collection": [{
            "preprint_doi": "10.0000/fictional-preprint",
            "published_doi": "10.0000/fictional-publication",
            "preprint_title": "Imaginary acoustics",
            "preprint_abstract": "Fictional sound", "preprint_authors": "A Author",
            "preprint_date": "2026-01-02", "published_journal": "Imaginary Journal"
        }], "messages": [{"count": 1, "total": "1"}]}])
        provider = BioRxivProvider(http)
        query = provider.plan_queries(TOPIC, FOCUS,
                                      DateWindow("2026-01-01", "2026-01-31"))[1]
        record = provider.normalize(provider.fetch_page(query).records[0])
        self.assertEqual(record.identifiers["doi"], "10.0000/fictional-preprint")
        self.assertEqual(record.normalized_metadata["published_doi"],
                         "10.0000/fictional-publication")
        self.assertEqual(record.normalized_metadata["published_journal"], "Imaginary Journal")
        self.assertEqual(record.provenance["identifiers.doi"].source_field, "preprint_doi")

    def test_empty_page_before_total_fails(self):
        http, _ = client("biorxiv", [{"collection": [],
                                      "messages": [{"cursor": 0, "count": 0, "total": "31"}]}])
        query = BioRxivProvider().plan_queries(TOPIC, FOCUS,
                                               DateWindow("2026-01-01", "2026-01-31"))[0]
        with self.assertRaises(RequestFailure) as raised:
            BioRxivProvider(http).fetch_page(query)
        self.assertEqual(raised.exception.category, "incomplete_page")

    def test_missing_total_does_not_mark_short_page_complete(self):
        http, _ = client("biorxiv", [{"collection": [DETAIL], "messages": [{"count": 1}]}])
        query = BioRxivProvider().plan_queries(TOPIC, FOCUS,
                                               DateWindow("2026-01-01", "2026-01-31"))[0]
        with self.assertRaises(RequestFailure) as raised:
            BioRxivProvider(http).fetch_page(query)
        self.assertEqual(raised.exception.category, "malformed_response")

    def test_missing_and_errors(self):
        provider = BioRxivProvider()
        details, _ = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))
        raw = type("R", (), {"provider_id": "biorxiv", "payload": dict(DETAIL, abstract=None, authors="", doi="10.0000/preprint"),
                             "retrieved_at": "2026-01-01T00:00:00+00:00"})()
        self.assertIsNone(provider.normalize(raw).normalized_metadata["abstract"])
        for response, category in ((HTTPResponse(429, {}, b""), "rate_limited"),
                                   (HTTPResponse(500, {}, b""), "server_error"),
                                   (HTTPResponse(200, {}, b"bad"), "invalid_json"),
                                   (TimeoutError(), "timeout")):
            with self.subTest(category=category):
                http, _ = client("biorxiv", [response])
                with self.assertRaises(RequestFailure) as raised:
                    BioRxivProvider(http).fetch_page(details)
                self.assertEqual(raised.exception.category, category)
