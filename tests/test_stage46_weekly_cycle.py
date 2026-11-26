"""Weekly orchestration contracts using fictional, local Provider records."""
from __future__ import annotations

import tempfile
import sqlite3
import json
from contextlib import closing
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from literature_radar.topics.manager import initialize
from literature_radar.workflow.multi_topic import run_weeklies
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from literature_radar.workflow.state import advance, read_state
from literature_radar.delivery.cards import frontmatter
from literature_radar.config import load_bundle
from literature_radar.discovery.query_planner import planned_queries
from literature_radar.providers.models import DateWindow
from tests.test_stage37_39_workflow import RichProvider, intake, record


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


class Stage46WeeklyCycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_a = initialize(self.root / "config", intake("fictional-alpha"))
        self.config_b = initialize(self.root / "config", intake("fictional-beta"))
        for config in (self.config_a, self.config_b):
            activate_weekly(config, skip_bootstrap=True, reason="Fictional offline acceptance")
        self.database = self.root / "academic.sqlite3"

    def test_training_runs_before_discovery_and_failure_uses_prior_memory(self):
        sequence = []
        from literature_radar.workflow import weekly
        original_discover = weekly.discover_incremental

        def discover(*args, **kwargs):
            sequence.append("discover")
            return original_discover(*args, **kwargs)

        def train(_):
            sequence.append("train")
            raise RuntimeError("fictional calibration failure")

        with patch.object(weekly, "train_due", side_effect=train), \
             patch.object(weekly, "discover_incremental", side_effect=discover):
            result = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                                provider_names=["openalex"],
                                providers={"openalex": RichProvider("openalex", [[record(1)]])},
                                now=NOW)
        self.assertEqual(sequence, ["train", "discover"])
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["attention_training"]["status"], "FAILED")
        self.assertEqual(result["recommended_count"], 1)

    def test_real_card_path_can_preview_notifications_without_outbox_mutation(self):
        delivery_path = self.config_a / "delivery.json"
        delivery = json.loads(delivery_path.read_text())
        delivery["notification"] = {"enabled": True, "provider": "desktop"}
        delivery_path.write_text(json.dumps(delivery))

        class NeverSend:
            def send(self, *_args):
                raise AssertionError("preview must never call notifier")

        result = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[record(1)]])},
                            notifiers={"desktop": NeverSend()}, notification_preview=True,
                            now=NOW)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(result["notification"]["preview"])
        self.assertEqual(result["notification"]["sent"], 0)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    def test_one_topic_provider_failure_does_not_cancel_other_topic(self):
        providers = {
            "fictional-alpha": {"openalex": RichProvider("openalex", [[record(1)]])},
            "fictional-beta": {"openalex": RichProvider("openalex", [[]], fail_at=0)},
        }
        result = run_weeklies([self.config_a, self.config_b], self.database,
                              self.root / "reports", provider_names=["openalex"],
                              providers_by_topic=providers, now=NOW, max_workers=2)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["topics"]["fictional-alpha"]["status"], "SUCCESS")
        self.assertEqual(result["topics"]["fictional-alpha"]["recommended_count"], 1)
        self.assertEqual(result["topics"]["fictional-beta"]["status"], "INCOMPLETE")

    def test_incomplete_window_resumes_across_days_and_next_monday_runs(self):
        provider = RichProvider("openalex", [[record(1)], [record(2)]])
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex"], providers={"openalex": provider},
                           now=NOW, max_pages=1)
        self.assertEqual(first["status"], "INCOMPLETE")
        self.assertEqual(read_state(self.config_a)["pending_provider_windows"][0]["end"], "2026-11-06")
        self.assertFalse(first["coverage_complete"])
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"], providers={"openalex": provider},
                            now=datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(second["overlap_window"]["end"], "2026-11-06")
        self.assertEqual(provider.calls, [0, 1])
        self.assertIsNone(read_state(self.config_a)["active_weekly_window"])
        self.assertEqual(read_state(self.config_a)["pending_provider_windows"], [])
        monday = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[]])},
                            now=datetime(2026, 11, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(monday["status"], "SUCCESS")
        self.assertEqual(monday["overlap_window"]["end"], "2026-11-09")

    def test_failed_provider_keeps_its_window_while_other_provider_advances(self):
        healthy = RichProvider("openalex", [[record(1)]])
        failing = RichProvider("crossref", [[record(2)]], fail_at=0)
        providers = {"openalex": healthy, "crossref": failing}
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex", "crossref"], providers=providers,
                           now=NOW)
        self.assertEqual(first["status"], "PARTIAL_SUCCESS")
        self.assertFalse(first["coverage_complete"])
        self.assertEqual(first["recommended_count"], 1)
        self.assertEqual(len(read_state(self.config_a)["pending_provider_windows"]), 1)
        monday = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex", "crossref"], providers=providers,
                            now=datetime(2026, 11, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(monday["status"], "PARTIAL_SUCCESS")
        self.assertFalse(monday["coverage_complete"])
        self.assertEqual(monday["providers"]["openalex"]["status"], "SUCCESS")
        self.assertEqual(monday["providers"]["crossref"]["error_category"], "TimeoutError")
        self.assertEqual(len(read_state(self.config_a)["pending_provider_windows"]), 2)
        self.assertEqual(len(failing.calls), 3)  # current first, then bounded old recovery
        self.assertEqual(len(healthy.calls), 2)
        tuesday = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                             provider_names=["openalex", "crossref"], providers=providers,
                             now=datetime(2026, 11, 10, 14, tzinfo=timezone.utc))
        self.assertEqual(tuesday["status"], "PARTIAL_SUCCESS")
        self.assertEqual(len(failing.calls), 5)
        self.assertEqual(tuesday["provider_recovery"][0]["status"], "FAILED")

    def test_recovered_provider_clears_pending_window_without_replaying_healthy_source(self):
        healthy = RichProvider("openalex", [[record(1)]])
        recovering = RichProvider("crossref", [[record(2)]], fail_at=0)
        providers = {"openalex": healthy, "crossref": recovering}
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex", "crossref"], providers=providers,
                           now=NOW)
        self.assertEqual(first["status"], "PARTIAL_SUCCESS")
        recovering.fail_at = None
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex", "crossref"], providers=providers,
                            now=datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertTrue(second["coverage_complete"])
        self.assertEqual(second["providers"]["crossref"]["status"], "SUCCESS")
        self.assertEqual(second["provider_recovery"], [])
        self.assertEqual(len(healthy.calls), 1)
        self.assertEqual(len(recovering.calls), 2)
        self.assertEqual(read_state(self.config_a)["pending_provider_windows"], [])

    def test_failed_semantic_group_remains_pending_and_only_failed_group_retries(self):
        class GroupedRichProvider(RichProvider):
            supports_semantic_groups = True

            def __init__(self):
                super().__init__("openalex", [[record(1)]])
                self.fail_primary = True
                self.query_calls = []

            def fetch_page(self, query, cursor=None):
                self.query_calls.append(query.query_id)
                if self.fail_primary and query.query_id.endswith(":openalex"):
                    raise TimeoutError("synthetic primary query timeout")
                return super().fetch_page(query, cursor)

        provider = GroupedRichProvider()
        plans = planned_queries(provider, load_bundle(self.config_a),
                                DateWindow("2026-10-23", "2026-11-06"),
                                config_dir=self.config_a)
        self.assertGreater(len(plans), 1)
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex"], providers={"openalex": provider}, now=NOW)
        self.assertEqual(first["providers"]["openalex"]["status"], "PARTIAL_SUCCESS")
        self.assertEqual(len(first["providers"]["openalex"]["failed_queries"]), 1)
        self.assertFalse(first["coverage_complete"])
        self.assertEqual(len(read_state(self.config_a)["pending_provider_windows"]), 1)
        self.assertEqual(len(provider.query_calls), len(plans))
        cards = list(first["cards"])

        provider.fail_primary = False
        before = len(provider.query_calls)
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"], providers={"openalex": provider},
                            now=datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertTrue(second["coverage_complete"])
        self.assertEqual(provider.query_calls[before:], ["fictional-alpha:openalex"])
        self.assertEqual(read_state(self.config_a)["pending_provider_windows"], [])
        self.assertEqual(second["cards"], [])
        self.assertEqual(len(cards), 1)
        self.assertTrue(Path(cards[0]).is_file())

    def test_pending_event_restores_observation_after_provider_checkpoint(self):
        provider = RichProvider("openalex", [[record(1)]])
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex"], providers={"openalex": provider},
                           now=NOW)
        paper_id = first["recommended"][0]["paper_id"]
        run_at = "2026-11-06T13:00:00+00:00"
        advance(self.config_a, "WEEKLY_MONITORING", active_weekly_window={
            "start": "2026-10-23", "end": "2026-11-06", "run_at": run_at})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO paper_events VALUES (?,?,?,?,?,?,NULL)",
                       ("fictional-alpha", paper_id, "fictional-revision", "MATERIAL_UPDATE",
                        None, "2026-11-06T13:01:00+00:00"))
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"], providers={"openalex": provider},
                            now=datetime(2026, 11, 6, 14, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(second["resumed_pending_event_count"], 1)
        self.assertEqual(second["assessed_count"], 1)
        self.assertEqual(provider.calls, [0])

    def test_delayed_window_records_actual_delivery_day_on_card(self):
        advance(self.config_a, "WEEKLY_MONITORING", active_weekly_window={
            "start": "2026-10-23", "end": "2026-11-06",
            "run_at": "2026-11-06T12:00:00+00:00"})
        result = run_weekly(self.config_a, self.database, self.root / "delayed",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[record(1)]])},
                            now=datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "SUCCESS")
        meta = frontmatter(Path(result["cards"][0]).read_text())
        self.assertEqual(meta["last_seen"], "2026-11-08")
        self.assertEqual(meta["last_pushed"], "2026-11-08")
        self.assertEqual(json.loads(meta["push_history"])[-1]["date"], "2026-11-08")

    def test_future_publication_is_not_marked_new_or_recommended(self):
        future = run_weekly(self.config_a, self.database, self.root / "future",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[record(9, date="2026-12-01")]])},
                            now=NOW)
        self.assertEqual(future["events"][0]["event"], "FUTURE_DATED")
        self.assertEqual(future["recommended_count"], 0)


if __name__ == "__main__":
    unittest.main()
