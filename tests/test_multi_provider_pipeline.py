import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.discovery.query_planner import planned_queries
from literature_radar.config import load_bundle
from literature_radar.providers.models import DateWindow
from literature_radar.providers.models import PageResult, RequestMetadata
from literature_radar.sources.openalex import OpenAlexProvider
from tests.synthetic_provider import SyntheticProvider, fictional

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"
NAMES = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")

class MultiProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "multi.sqlite3"

    def test_five_sources_deduplicate_and_isolate_failure(self):
        providers = {name: SyntheticProvider(name, [[fictional(name)]]) for name in NAMES}
        providers["europepmc"].fail_at = 0
        summary = discover_incremental(CONFIG, list(NAMES), database_path=self.database,
                                       since="2026-01-01", until="2026-01-31", providers=providers)
        self.assertEqual(summary["status"], "PARTIAL_SUCCESS")
        self.assertEqual(summary["providers"]["europepmc"]["status"], "FAILED")
        self.assertEqual(AcademicStore(self.database).count(), 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints WHERE status='FAILED'").fetchone()[0], 1)
        providers["europepmc"].fail_at = None
        summary = discover_incremental(CONFIG, list(NAMES), database_path=self.database,
                                       since="2026-01-01", until="2026-01-31", providers=providers)
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(AcademicStore(self.database).count(), 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 5)

    def test_same_title_different_doi_and_preprint_relation(self):
        providers = {
            "biorxiv": SyntheticProvider("biorxiv", [[fictional("preprint", doi="10.0000/preprint")]]),
            "crossref": SyntheticProvider("crossref", [[fictional("published", doi="10.0000/published")]])}
        discover_incremental(CONFIG, list(providers), database_path=self.database,
                             since="2026-01-01", until="2026-01-31", providers=providers)
        self.assertEqual(AcademicStore(self.database).count(), 2)

    def test_two_source_failures_still_keep_successes(self):
        providers = {name: SyntheticProvider(name, [[fictional(name)]], fail_at=0 if name in NAMES[:2] else None)
                     for name in NAMES}
        summary = discover_incremental(CONFIG, list(NAMES), database_path=self.database,
                                       since="2026-01-01", until="2026-01-31", providers=providers)
        self.assertEqual(summary["status"], "PARTIAL_SUCCESS")
        self.assertEqual(AcademicStore(self.database).count(), 1)

    def test_each_provider_receives_own_time_budget(self):
        class SlowProvider(SyntheticProvider):
            def fetch_page(self, query, cursor=None):
                time.sleep(0.03)
                return super().fetch_page(query, cursor)

        slow = SlowProvider("openalex", [[fictional("slow-first")],
                                        [fictional("slow-second", doi="10.0000/second")]])
        healthy = SyntheticProvider("pubmed", [[fictional("healthy")]])
        result = discover_incremental(CONFIG, ["openalex", "pubmed"],
                                      database_path=self.database,
                                      since="2026-01-01", until="2026-01-31",
                                      max_seconds=0.02,
                                      providers={"openalex": slow, "pubmed": healthy})
        self.assertEqual(result["providers"]["openalex"]["status"], "INCOMPLETE")
        self.assertEqual(result["providers"]["pubmed"]["status"], "SUCCESS")
        self.assertEqual(healthy.calls, [0])

    def test_semantic_groups_keep_primary_signature_and_checkpoint_independently(self):
        bundle = load_bundle(CONFIG)
        window = DateWindow("2026-01-01", "2026-01-31")
        real_adapter = OpenAlexProvider()
        primary = real_adapter.plan_queries(bundle["topic"], bundle["focus"], window)[0]
        plans = planned_queries(real_adapter, bundle, window)
        self.assertEqual(plans[0].query_signature, primary.query_signature)
        self.assertIn("core", {plan.query_id.rsplit(":", 1)[-1] for plan in plans[1:]})
        self.assertGreaterEqual(len(plans), 3)

        class GroupedProvider(SyntheticProvider):
            supports_semantic_groups = True
            def fetch_page(self, query, cursor=None):
                self.query_calls.append(query.query_id)
                return super().fetch_page(query, cursor)

        provider = GroupedProvider("openalex", [[fictional("grouped")]])
        provider.query_calls = []
        first = discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                                     since=window.start, until=window.end,
                                     providers={"openalex": provider})
        self.assertEqual(first["providers"]["openalex"]["status"], "SUCCESS")
        self.assertGreaterEqual(len(set(provider.query_calls)), 3)
        prior_calls = len(provider.query_calls)
        second = discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                                      since=window.start, until=window.end,
                                      providers={"openalex": provider})
        self.assertEqual(second["providers"]["openalex"]["status"], "SUCCESS")
        self.assertEqual(len(provider.query_calls), prior_calls)

    def test_slow_first_group_leaves_time_and_resumable_cursor_for_later_groups(self):
        class SlowFirst(SyntheticProvider):
            supports_semantic_groups = True

            def __init__(self):
                super().__init__("openalex", [])
                self.slow = True
                self.query_calls = []

            def fetch_page(self, query, cursor=None):
                self.query_calls.append((query.query_id, cursor))
                if query.query_id.endswith(":openalex"):
                    if self.slow:
                        time.sleep(0.04)
                    return PageResult((), 1 if cursor is None else None,
                                      cursor is None,
                                      RequestMetadata("openalex", "2026-01-01T00:00:00Z"))
                return PageResult((), None, False,
                                  RequestMetadata("openalex", "2026-01-01T00:00:00Z"))

        provider = SlowFirst()
        arguments = dict(database_path=self.database, since="2026-01-01", until="2026-01-07",
                         max_seconds=0.15, providers={"openalex": provider})
        first = discover_incremental(CONFIG, ["openalex"], **arguments)
        self.assertEqual(first["providers"]["openalex"]["status"], "INCOMPLETE")
        self.assertGreaterEqual(len({query_id for query_id, _ in provider.query_calls}), 3)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertGreaterEqual(db.execute(
                "SELECT COUNT(*) FROM provider_checkpoints WHERE status='SUCCESS'").fetchone()[0], 2)
        provider.slow = False
        before = len(provider.query_calls)
        second = discover_incremental(CONFIG, ["openalex"], **arguments)
        self.assertEqual(second["providers"]["openalex"]["status"], "SUCCESS")
        self.assertEqual(provider.query_calls[before:],
                         [(load_bundle(CONFIG)["topic"]["id"] + ":openalex", 1)])

    def test_one_query_timeout_does_not_skip_other_groups_and_resumes(self):
        class OneTimeout(SyntheticProvider):
            supports_semantic_groups = True

            def __init__(self):
                super().__init__("openalex", [[]])
                self.fail_primary = True
                self.query_calls = []

            def fetch_page(self, query, cursor=None):
                self.query_calls.append(query.query_id)
                if self.fail_primary and query.query_id.endswith(":openalex"):
                    raise TimeoutError("synthetic primary timeout")
                return super().fetch_page(query, cursor)

        provider = OneTimeout()
        arguments = dict(database_path=self.database, since="2026-01-01", until="2026-01-07",
                         providers={"openalex": provider})
        plan_count = len(planned_queries(provider, load_bundle(CONFIG),
                                         DateWindow("2026-01-01", "2026-01-07")))
        first = discover_incremental(CONFIG, ["openalex"], **arguments)
        self.assertEqual(first["providers"]["openalex"]["status"], "PARTIAL_SUCCESS")
        self.assertEqual(len(provider.query_calls), plan_count)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints WHERE status='FAILED'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_checkpoints WHERE status='SUCCESS'").fetchone()[0], plan_count - 1)
        provider.fail_primary = False
        before = len(provider.query_calls)
        second = discover_incremental(CONFIG, ["openalex"], **arguments)
        self.assertEqual(second["providers"]["openalex"]["status"], "SUCCESS")
        self.assertEqual(provider.query_calls[before:],
                         [load_bundle(CONFIG)["topic"]["id"] + ":openalex"])
