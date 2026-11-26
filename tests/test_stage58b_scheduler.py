"""Offline Stage 5.8B scheduling and checkpoint regressions."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.http import HTTPClient
from literature_radar.topics.manager import initialize
from literature_radar.workflow.state import advance, read_state
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import RichProvider, intake, record

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
OLD1 = ("2026-10-26", "2026-11-09")
OLD2 = ("2026-11-08", "2026-11-22")
CURRENT = ("2026-11-09", "2026-11-23")
NEXT_CURRENT = ("2026-11-10", "2026-11-24")
STAMP = "2026-11-24T03:00:00+00:00"


class WindowProvider(RichProvider):
    def __init__(self, provider_id="europepmc", *, pages=None, fail=None):
        super().__init__(provider_id, [])
        self.window_pages = pages or {}
        self.fail = fail or {}
        self.window_calls = []

    def fetch_page(self, query, cursor=None):
        window = (query.date_window.start, query.date_window.end)
        index = 0 if cursor is None else int(cursor)
        self.window_calls.append((window, index))
        if self.fail.get(window) == index:
            raise TimeoutError("fictional window timeout")
        pages = self.window_pages.get(window, [[]])
        rows = pages[index] if index < len(pages) else []
        more = index + 1 < len(pages)
        return PageResult(tuple(RawRecord(self.provider_id, row, STAMP) for row in rows),
                          index + 1 if more else None, more,
                          RequestMetadata(self.provider_id, STAMP))


class Stage58BSchedulerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        activate_weekly(self.config, skip_bootstrap=True, reason="Stage 5.8B offline")
        self.database = self.root / "academic.sqlite3"

    def pending(self, provider, *windows):
        bundle = load_bundle(self.config)
        items = []
        for start, end in windows:
            plan = provider.plan_queries(bundle["topic"], bundle["focus"], DateWindow(start, end))[0]
            items.append({"provider_id": provider.provider_id, "start": start,
                          "end": end, "query_signatures": [plan.query_signature]})
        advance(self.config, "WEEKLY_MONITORING", pending_provider_windows=items)

    def run_weekly_case(self, providers, *, max_pages=1, max_seconds=30, now=NOW):
        return run_weekly(self.config, self.database, self.root / "reports",
                          provider_names=list(providers), providers=providers,
                          now=now, max_pages=max_pages, max_seconds=max_seconds,
                          notification_preview=True)

    def test_current_first_and_two_old_windows_each_advance_with_bounded_share(self):
        provider = WindowProvider(pages={CURRENT: [[record(1, date="2026-11-23")]],
                                         OLD1: [[record(2)], [record(3)]],
                                         OLD2: [[record(4)], [record(5)]]})
        self.pending(provider, OLD1, OLD2)
        report = self.run_weekly_case({"europepmc": provider})
        self.assertEqual(provider.window_calls, [(CURRENT, 0), (OLD1, 0), (OLD2, 0)])
        self.assertEqual(report["providers"]["europepmc"]["status"], "SUCCESS")
        self.assertEqual([r["status"] for r in report["provider_recovery"]],
                         ["INCOMPLETE", "INCOMPLETE"])
        self.assertEqual([r["budget_seconds"] for r in report["provider_recovery"]], [15, 15])
        self.assertEqual(len(report["pending_provider_windows"]), 2)
        self.assertTrue(all(p["last_reason"] == "page_limit_exhausted"
                            for p in report["pending_provider_windows"]))
        self.assertEqual(AcademicStore(self.database).count(), 3)

    def test_completed_current_skips_http_while_old_cursors_resume(self):
        provider = WindowProvider(pages={CURRENT: [[record(1, date="2026-11-23")]],
                                         OLD1: [[record(2)], [record(3)]],
                                         OLD2: [[record(4)], [record(5)]]})
        self.pending(provider, OLD1, OLD2)
        self.run_weekly_case({"europepmc": provider})
        provider.window_calls.clear()
        report = self.run_weekly_case({"europepmc": provider})
        self.assertEqual(provider.window_calls, [(OLD1, 1), (OLD2, 1)])
        self.assertEqual(report["pending_provider_windows"], [])
        self.assertEqual([r["status"] for r in report["provider_recovery"]],
                         ["SUCCESS", "SUCCESS"])

    def test_three_prior_windows_each_receive_a_time_slice_on_new_week(self):
        provider = WindowProvider(pages={NEXT_CURRENT: [[record(1, date="2026-11-24")]],
                                         OLD1: [[record(2)]], OLD2: [[record(3)]],
                                         CURRENT: [[record(4)]]})
        self.pending(provider, OLD1, OLD2, CURRENT)
        report = self.run_weekly_case({"europepmc": provider},
                                      now=datetime(2026, 11, 24, 15, tzinfo=timezone.utc))
        self.assertEqual(provider.window_calls,
                         [(NEXT_CURRENT, 0), (OLD1, 0), (OLD2, 0), (CURRENT, 0)])
        self.assertEqual([r["budget_seconds"] for r in report["provider_recovery"]],
                         [10, 10, 10])
        self.assertEqual(report["pending_provider_windows"], [])

    def test_current_timeout_does_not_prevent_historical_recovery(self):
        provider = WindowProvider(pages={OLD1: [[record(2)]], OLD2: [[record(3)]]},
                                  fail={CURRENT: 0})
        self.pending(provider, OLD1, OLD2)
        report = self.run_weekly_case({"europepmc": provider})
        self.assertEqual(provider.window_calls, [(CURRENT, 0), (OLD1, 0), (OLD2, 0)])
        self.assertEqual(report["providers"]["europepmc"]["status"], "FAILED")
        self.assertEqual([r["status"] for r in report["provider_recovery"]],
                         ["SUCCESS", "SUCCESS"])
        pending = report["pending_provider_windows"]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["start"], pending[0]["end"]), CURRENT)
        self.assertEqual(pending[0]["last_reason"], "TimeoutError")

    def test_old_timeout_does_not_block_current_or_later_old_window(self):
        provider = WindowProvider(pages={CURRENT: [[record(1, date="2026-11-23")]],
                                         OLD2: [[record(3)]]}, fail={OLD1: 0})
        self.pending(provider, OLD1, OLD2)
        report = self.run_weekly_case({"europepmc": provider})
        self.assertEqual(provider.window_calls, [(CURRENT, 0), (OLD1, 0), (OLD2, 0)])
        self.assertEqual(report["providers"]["europepmc"]["status"], "SUCCESS")
        self.assertEqual([r["status"] for r in report["provider_recovery"]],
                         ["FAILED", "SUCCESS"])
        self.assertEqual(len(report["pending_provider_windows"]), 1)
        self.assertEqual(report["pending_provider_windows"][0]["last_reason"], "TimeoutError")

    def test_old_time_budget_exhaustion_keeps_current_covered(self):
        class SlowOldWindow(WindowProvider):
            def fetch_page(self, query, cursor=None):
                page = super().fetch_page(query, cursor)
                if (query.date_window.start, query.date_window.end) == OLD1:
                    time.sleep(0.04)
                return page

        provider = SlowOldWindow(pages={CURRENT: [[record(1, date="2026-11-23")]],
                                         OLD1: [[record(2)], [record(3)]]})
        self.pending(provider, OLD1)
        report = self.run_weekly_case({"europepmc": provider}, max_pages=10,
                                      max_seconds=0.02)
        self.assertEqual(report["providers"]["europepmc"]["status"], "SUCCESS")
        self.assertEqual(report["provider_recovery"][0]["status"], "INCOMPLETE")
        self.assertEqual(report["pending_provider_windows"][0]["last_reason"],
                         "time_budget_exhausted")
        self.assertEqual(provider.window_calls, [(CURRENT, 0), (OLD1, 0)])

    def test_one_provider_failure_does_not_stop_other_current_provider(self):
        slow = WindowProvider(fail={CURRENT: 0})
        healthy = WindowProvider("pubmed", pages={CURRENT: [[record(8, date="2026-11-23")]]})
        report = self.run_weekly_case({"europepmc": slow, "pubmed": healthy})
        self.assertEqual(report["providers"]["europepmc"]["status"], "FAILED")
        self.assertEqual(report["providers"]["pubmed"]["status"], "SUCCESS")
        self.assertEqual(healthy.window_calls, [(CURRENT, 0)])

    def test_existing_page_68_cursor_is_used_without_replay(self):
        provider = WindowProvider(pages={OLD1: [[] for _ in range(68)] + [[record(9)]]})
        self.pending(provider, OLD1)
        bundle = load_bundle(self.config)
        plan = provider.plan_queries(bundle["topic"], bundle["focus"], DateWindow(*OLD1))[0]
        AcademicStore(self.database)
        with sqlite3.connect(self.database) as db, db:
            db.execute("INSERT INTO provider_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("europepmc", bundle["topic"]["id"], plan.query_id,
                        plan.query_signature, *OLD1, "68", 68, 33980,
                        "IN_PROGRESS", None, STAMP))
        report = self.run_weekly_case({"europepmc": provider})
        self.assertEqual(provider.window_calls, [(CURRENT, 0), (OLD1, 68)])
        self.assertEqual(report["provider_recovery"][0]["status"], "SUCCESS")
        cp = AcademicStore(self.database).checkpoint("europepmc", bundle["topic"]["id"],
                                                     plan.query_signature)
        self.assertEqual((cp["page_number"], cp["records_fetched"], cp["status"]),
                         (69, 33981, "SUCCESS"))

    def test_success_group_does_not_reserve_time_or_repeat_request(self):
        class Grouped(SyntheticProvider):
            def __init__(self):
                super().__init__("openalex", [])
                self.client = HTTPClient("openalex")
                self.fail_second = True
                self.groups = []
                self.remaining = None

            def plan_queries(self, topic, focus, date_window=None):
                return tuple(QueryPlan("openalex", topic["id"] + ":openalex:" + name,
                                       {"terms": [name]}, date_window)
                             for name in ("done", "remaining"))

            def fetch_page(self, query, cursor=None):
                self.groups.append(query.query_id)
                if query.query_id.endswith(":remaining"):
                    if self.fail_second:
                        raise TimeoutError("fictional group timeout")
                    self.remaining = self.client.budget_deadline - time.monotonic()
                return PageResult((RawRecord("openalex", fictional(query.query_id,
                                  doi="10.0000/" + query.query_id.split(":")[-1]), STAMP),),
                                  None, False, RequestMetadata("openalex", STAMP))

        provider = Grouped()
        config = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"
        first = discover_incremental(config, ["openalex"], database_path=self.database,
                                     since="2026-01-01", until="2026-01-31",
                                     providers={"openalex": provider}, max_seconds=1)
        self.assertEqual(first["providers"]["openalex"]["status"], "PARTIAL_SUCCESS")
        provider.groups.clear()
        provider.fail_second = False
        second = discover_incremental(config, ["openalex"], database_path=self.database,
                                      since="2026-01-01", until="2026-01-31",
                                      providers={"openalex": provider}, max_seconds=0.3)
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(provider.groups, [load_bundle(config)["topic"]["id"] + ":openalex:remaining"])
        self.assertGreater(provider.remaining, 0.20)


if __name__ == "__main__":
    unittest.main()
