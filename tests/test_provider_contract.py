import unittest
from pathlib import Path

from literature_radar.providers import (DateWindow, PageResult, Provider,
                                        ProviderRegistry, QueryPlan, RequestMetadata)
from literature_radar.sources.fixture import FixtureProvider

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/synthetic_papers.json"


class ProviderContractTests(unittest.TestCase):
    def test_fixture_satisfies_contract(self):
        self.assertIsInstance(FixtureProvider(FIXTURE), Provider)

    def test_stable_signature_and_change_detection(self):
        a = QueryPlan("fixture", "synthetic", {"terms": ["imaginary"]}, DateWindow("2026-01-01", "2026-01-02", "posted"))
        b = QueryPlan("fixture", "synthetic", {"terms": ["imaginary"]}, DateWindow("2026-01-01", "2026-01-02", "posted"))
        c = QueryPlan("fixture", "synthetic", {"terms": ["different"]}, DateWindow("2026-01-01", "2026-01-02", "posted"))
        self.assertEqual(a.query_signature, b.query_signature)
        self.assertNotEqual(a.query_signature, c.query_signature)

    def test_cursor_accepts_structured_json(self):
        page = PageResult((), {"offset": 2, "opaque": "next"}, True,
                          RequestMetadata("fixture", "2026-01-01T00:00:00+00:00"))
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor["offset"], 2)
        with self.assertRaisesRegex(ValueError, "next_cursor"):
            PageResult((), None, True, page.request_metadata)

    def test_independent_registration(self):
        first = FixtureProvider(FIXTURE, provider_id="synthetic-one")
        second = FixtureProvider(FIXTURE, provider_id="synthetic-two")
        registry = ProviderRegistry()
        registry.register(first)
        registry.register(second)
        self.assertEqual(registry.ids(), ("synthetic-one", "synthetic-two"))
        self.assertIs(registry.get("synthetic-two"), second)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.register(first)

    def test_invalid_fixture_config_and_credentials(self):
        fixture = FixtureProvider(FIXTURE)
        with self.assertRaisesRegex(ValueError, "source"):
            fixture.validate_config({"source": "", "terms": ["imaginary"]})
        with self.assertRaisesRegex(ValueError, "terms"):
            fixture.validate_config({"source": "synthetic-index", "terms": []})
        with self.assertRaisesRegex(ValueError, "credentials"):
            QueryPlan("fixture", "synthetic", {"api_key": "fictional-placeholder"})

    def test_validation_does_not_call_health_check(self):
        fixture = FixtureProvider(FIXTURE)
        fixture.health_check = lambda: self.fail("health check was called")
        fixture.validate_config({"source": "synthetic-index", "terms": ["synthetic"]})
