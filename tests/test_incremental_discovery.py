import tempfile
import unittest
import os
import multiprocessing
from unittest.mock import patch
from pathlib import Path
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.database.academic import AcademicStore
from literature_radar.config import load_bundle
from literature_radar.sources.http import RequestFailure
from tests.synthetic_provider import SyntheticProvider, fictional

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"


def _commit_then_exit(path):
    """Simulate process loss immediately after a committed first page."""
    from literature_radar.providers.models import DateWindow
    bundle = load_bundle(CONFIG)
    provider = SyntheticProvider("openalex", [
        [fictional("page-one", doi="10.0000/page-one")],
        [fictional("page-two", doi="10.0000/page-two")],
    ])
    plan = provider.plan_queries(bundle["topic"], bundle["focus"],
                                 DateWindow("2026-01-01", "2026-01-31"))[0]
    page = provider.fetch_page(plan)
    store = AcademicStore(Path(path))
    store.commit_page(bundle["topic"]["id"], plan, page,
                      [provider.normalize(raw) for raw in page.records],
                      expected_page_number=0, expected_cursor=None)
    os._exit(0)

class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "papers.sqlite3"

    def run_discovery(self, provider, **kwargs):
        return discover_incremental(CONFIG, [provider.provider_id], database_path=self.database,
                                    since="2026-01-01", until="2026-01-31", max_pages=2,
                                    providers={provider.provider_id: provider}, **kwargs)

    def test_malformed_page_does_not_disguise_incomplete_pagination(self):
        provider = SyntheticProvider("openalex", [[{"malformed": True}], [fictional("later")]])
        result = discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                                      since="2026-01-01", until="2026-01-31", max_pages=1,
                                      providers={"openalex": provider})
        self.assertEqual(result["providers"]["openalex"]["malformed"], 1)
        self.assertEqual(result["providers"]["openalex"]["status"], "INCOMPLETE")

    def test_page_failure_resume_and_idempotent_replay(self):
        provider = SyntheticProvider("openalex", [[fictional("a")], [fictional("a")]], fail_at=1)
        first = self.run_discovery(provider)
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["providers"]["openalex"]["new_records"], 1)
        self.assertEqual(AcademicStore(self.database).count(), 1)
        provider.fail_at = None
        second = self.run_discovery(provider)
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(second["providers"]["openalex"]["unchanged_records"], 1)
        self.assertEqual(provider.calls, [0, 1, 1])
        self.assertEqual(AcademicStore(self.database).count(), 1)

    def test_dry_run_no_persist_no_database(self):
        provider = SyntheticProvider("openalex", [[fictional("a")]])
        result = self.run_discovery(provider, dry_run=True)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(provider.calls, [])
        self.assertFalse(self.database.exists())
        result = self.run_discovery(provider, no_persist=True)
        self.assertEqual(result["papers_seen"], 1)
        self.assertFalse(self.database.exists())

    def test_query_signature_changes_and_malformed_record(self):
        provider = SyntheticProvider("openalex", [[fictional("a"), {"malformed": True}]])
        first = self.run_discovery(provider)
        self.assertEqual(first["providers"]["openalex"]["malformed"], 1)
        self.assertEqual(AcademicStore(self.database).count(), 1)
        other = SyntheticProvider("openalex", [[fictional("b", doi="10.0000/second")]])
        second = discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                                      since="2026-02-01", until="2026-02-28",
                                      providers={"openalex": other})
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(AcademicStore(self.database).count(), 2)

    def test_changed_query_uses_initial_lookback_not_old_checkpoint(self):
        first_provider = SyntheticProvider("openalex", [[fictional("a")]])
        discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                             until="2026-01-31", lookback_days=30,
                             providers={"openalex": first_provider})
        modified = load_bundle(CONFIG)
        modified["topic"] = {**modified["topic"], "search_terms": ["new-fictional-term"]}
        second_provider = SyntheticProvider("openalex", [[fictional("b", doi="10.0000/second")]])
        with patch("literature_radar.discovery.incremental.load_bundle", return_value=modified):
            discover_incremental(CONFIG, ["openalex"], database_path=self.database,
                                 until="2026-02-02", lookback_days=30,
                                 providers={"openalex": second_provider})
        self.assertEqual(second_provider.fetched_windows[0].start, "2026-01-03")
        self.assertEqual(AcademicStore(self.database).count(), 2)

    def test_process_exit_after_commit_resumes_second_page_only(self):
        child = multiprocessing.get_context("spawn").Process(target=_commit_then_exit,
                                                           args=(str(self.database),))
        child.start()
        child.join(timeout=10)
        self.assertEqual(child.exitcode, 0)
        provider = SyntheticProvider("openalex", [
            [fictional("page-one", doi="10.0000/page-one")],
            [fictional("page-two", doi="10.0000/page-two")],
        ])
        result = self.run_discovery(provider)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(provider.calls, [1])
        self.assertEqual(AcademicStore(self.database).count(), 2)

    def test_successful_same_window_is_idempotent(self):
        first = SyntheticProvider("openalex", [[fictional("one")]])
        self.assertEqual(self.run_discovery(first)["status"], "SUCCESS")
        second = SyntheticProvider("openalex", [[fictional("one")]])
        result = self.run_discovery(second)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["providers"]["openalex"]["pages"], 0)
        self.assertEqual(second.calls, [])
        self.assertEqual(AcademicStore(self.database).count(), 1)
        bundle = load_bundle(CONFIG)
        from literature_radar.providers.models import DateWindow
        signature = second.plan_queries(bundle["topic"], bundle["focus"],
                                        DateWindow("2026-01-01", "2026-01-31"))[0].query_signature
        self.assertEqual(AcademicStore(self.database).checkpoint("openalex", bundle["topic"]["id"],
                                                                   signature)["page_number"], 1)

    def test_invalid_saved_cursor_does_not_replay_from_start_or_advance_checkpoint(self):
        rows = [[fictional("one", doi="10.0000/one")],
                [fictional("two", doi="10.0000/two")]]
        first = SyntheticProvider("openalex", rows, fail_at=1)
        self.assertEqual(self.run_discovery(first)["status"], "FAILED")

        class CursorExpiresOnce(SyntheticProvider):
            expired = False
            def fetch_page(self, query, cursor=None):
                if cursor == 1 and not self.expired:
                    self.expired = True
                    raise RequestFailure("openalex", "invalid_query_or_cursor", 400)
                return super().fetch_page(query, cursor)

        second = CursorExpiresOnce("openalex", rows)
        result = self.run_discovery(second)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["providers"]["openalex"]["error_category"], "invalid_query_or_cursor")
        self.assertEqual(second.calls, [])
        self.assertEqual(AcademicStore(self.database).count(), 1)
        bundle = load_bundle(CONFIG)
        from literature_radar.providers.models import DateWindow
        plan = second.plan_queries(bundle["topic"], bundle["focus"],
                                   DateWindow("2026-01-01", "2026-01-31"))[0]
        checkpoint = AcademicStore(self.database).checkpoint("openalex", bundle["topic"]["id"],
                                                               plan.query_signature)
        self.assertEqual(checkpoint["page_number"], 1)
        self.assertEqual(checkpoint["cursor_json"], "1")
        self.assertEqual(self.run_discovery(second)["status"], "SUCCESS")
        self.assertEqual(second.calls, [1])
        self.assertEqual(AcademicStore(self.database).count(), 2)
