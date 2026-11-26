import unittest
from urllib.parse import parse_qs, urlsplit
from literature_radar.providers.models import DateWindow
from literature_radar.sources.crossref import CrossrefProvider
from literature_radar.sources.http import HTTPResponse, RequestFailure
from tests.mock_api import client

TOPIC = {"id": "synthetic-topic", "search_terms": ["imaginary"], "sources": ["crossref"]}
FOCUS = {"version": "v001", "search_terms": []}
WORK = {"DOI": "10.0000/fictional", "title": ["Imaginary paper"],
        "author": [{"given": "Fictional", "family": "Author"}],
        "container-title": ["Synthetic Journal"], "abstract": "<p>Fictional abstract</p>",
        "published": {"date-parts": [[2026, 1, 2]]},
        "published-online": {"date-parts": [[2026, 1, 1]]},
        "created": {"date-time": "2026-01-03T00:00:00Z"},
        "deposited": {"date-time": "2026-01-04T00:00:00Z"},
        "indexed": {"date-time": "2026-01-05T00:00:00Z"},
        "type": "journal-article", "ISSN": ["0000-0000"], "publisher": "Fictional Press",
        "license": [{"URL": "https://example.invalid/fictional-license"}]}


class CrossrefTests(unittest.TestCase):
    def test_fields_cursor_and_window(self):
        http, fake = client("crossref", [{"message": {"items": [WORK], "next-cursor": "next"}},
                                         {"message": {"items": [], "next-cursor": None}}])
        provider = CrossrefProvider(http, page_size=1)
        query = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))[0]
        self.assertNotIn("pagination_mode", query.query_parameters)
        first = provider.fetch_page(query)
        second = provider.fetch_page(query, first.next_cursor)
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        self.assertIn("from-index-date", fake.calls[0][0])
        self.assertIn("from-index-date", fake.calls[1][0])
        first_params = parse_qs(urlsplit(fake.calls[0][0]).query)
        second_params = parse_qs(urlsplit(fake.calls[1][0]).query)
        self.assertNotEqual(first_params.pop("cursor"), second_params.pop("cursor"))
        self.assertEqual(first_params, second_params)
        record = provider.normalize(first.records[0])
        self.assertEqual(record.normalized_metadata["abstract"], "Fictional abstract")
        self.assertEqual(record.normalized_metadata["online_date"], "2026-01-01")
        self.assertEqual(len(record.normalized_metadata["license"]), 1)
        self.assertEqual(record.provenance["updated_date"].source_field, "deposited.date-time")

    def test_missing_and_error_cases(self):
        provider = CrossrefProvider()
        raw = type("R", (), {"provider_id": "crossref", "payload": dict(WORK, abstract=None, author=[]),
                             "retrieved_at": "2026-01-01T00:00:00+00:00"})()
        self.assertIsNone(provider.normalize(raw).normalized_metadata["abstract"])
        query = provider.plan_queries(TOPIC, FOCUS)[0]
        for response, category in ((HTTPResponse(429, {}, b""), "rate_limited"),
                                   (HTTPResponse(500, {}, b""), "server_error"),
                                   (HTTPResponse(200, {}, b"bad"), "invalid_json"),
                                   (TimeoutError(), "timeout")):
            with self.subTest(category=category):
                http, _ = client("crossref", [response])
                with self.assertRaises(RequestFailure) as raised:
                    CrossrefProvider(http).fetch_page(query)
                self.assertEqual(raised.exception.category, category)

    def test_repeated_cursor_fails_instead_of_replaying_page(self):
        http, _ = client("crossref", [{"message": {"items": [WORK], "next-cursor": "same"}}])
        query = CrossrefProvider(page_size=1).plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            CrossrefProvider(http, page_size=1).fetch_page(query, "same")
        self.assertEqual(raised.exception.category, "stalled_cursor")

    def test_bounded_offset_pagination_is_complete_and_resumable(self):
        topic = {**TOPIC, "extensions": {"provider_options": {
            "crossref": {"pagination_mode": "offset", "date_chunk_days": 1}}}}
        http, fake = client("crossref", [
            {"message": {"items": [WORK, WORK], "total-results": 3}},
            {"message": {"items": [WORK], "total-results": 3}},
        ])
        provider = CrossrefProvider(http, page_size=2)
        query = provider.plan_queries(topic, FOCUS, DateWindow("2026-01-01", "2026-01-01"))[0]
        self.assertEqual(query.query_parameters["pagination_mode"], "offset")
        first = provider.fetch_page(query)
        second = provider.fetch_page(query, first.next_cursor)
        self.assertEqual((first.next_cursor, first.has_more), (2, True))
        self.assertEqual((second.next_cursor, second.has_more), (None, False))
        params = [parse_qs(urlsplit(call[0]).query) for call in fake.calls]
        self.assertEqual([p["offset"] for p in params], [["0"], ["2"]])
        self.assertTrue(all("sort" not in p and "order" not in p for p in params))
        self.assertTrue(all("cursor" not in p for p in params))

    def test_offset_limit_fails_explicitly(self):
        topic = {**TOPIC, "extensions": {"provider_options": {
            "crossref": {"pagination_mode": "offset"}}}}
        http, _ = client("crossref", [{"message": {"items": [WORK], "total-results": 10001}}])
        provider = CrossrefProvider(http, page_size=1)
        query = provider.plan_queries(topic, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            provider.fetch_page(query)
        self.assertEqual(raised.exception.category, "offset_limit_exceeded")

    def test_completed_cursor_plan_signature_can_be_matched_without_replaying(self):
        window = DateWindow("2026-10-24", "2026-10-24")
        cursor_topic = {**TOPIC, "extensions": {"provider_options": {
            "crossref": {"date_chunk_days": 1}}}}
        offset_topic = {**TOPIC, "extensions": {"provider_options": {
            "crossref": {"date_chunk_days": 1, "pagination_mode": "offset"}}}}
        provider = CrossrefProvider(page_size=25)
        legacy = provider.plan_queries(cursor_topic, FOCUS, window)[0]
        offset = provider.plan_queries(offset_topic, FOCUS, window)[0]
        reconstructed = provider.equivalent_cursor_plan(offset)
        self.assertEqual(reconstructed.query_signature, legacy.query_signature)
        self.assertEqual(reconstructed.query_id, offset.query_id)
        self.assertEqual(reconstructed.date_window, offset.date_window)
        self.assertNotEqual(offset.query_signature, legacy.query_signature)
        with self.assertRaises(ValueError):
            provider.equivalent_cursor_plan(legacy)
