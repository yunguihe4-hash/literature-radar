"""A completed old retry must not consume a later calendar week's scan."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.topics.manager import initialize
from literature_radar.workflow.multi_topic import run_weeklies
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from literature_radar.workflow.state import read_state
from tests.test_stage37_39_workflow import RichProvider, intake, record


class WeeklyCatchupTests(unittest.TestCase):
    def test_late_completion_requests_immediate_current_week_catchup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = initialize(root / "config", intake("fictional-catchup"))
            activate_weekly(config, skip_bootstrap=True, reason="Fictional offline test")
            database = root / "academic.sqlite3"
            provider = RichProvider("openalex", [[record(1)], [record(2)]])
            friday = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
            monday = datetime(2026, 11, 9, 14, tzinfo=timezone.utc)
            first = run_weekly(config, database, root / "first",
                               provider_names=["openalex"], providers={"openalex": provider},
                               now=friday, max_pages=1)
            self.assertEqual(first["status"], "INCOMPLETE")
            old_run_at = read_state(config)["last_weekly_window"]["run_at"]
            resumed = run_weeklies([config], database, root / "resumed",
                                   provider_names=["openalex"],
                                   providers_by_topic={"fictional-catchup": {"openalex": provider}},
                                   now=monday)
            self.assertEqual(resumed["status"], "SUCCESS")
            self.assertEqual(resumed["topics"]["fictional-catchup"]["status"], "SUCCESS")
            state = read_state(config)
            self.assertIsNone(state["active_weekly_window"])
            self.assertEqual(state["pending_provider_windows"], [])
            self.assertNotEqual(state["last_weekly_run_at"], old_run_at)
            current = run_weeklies([config], database, root / "current",
                                   provider_names=["openalex"],
                                   providers_by_topic={"fictional-catchup": {
                                       "openalex": RichProvider("openalex", [[]])}},
                                   now=monday)
            self.assertEqual(current["topics"]["fictional-catchup"]["status"], "NOT_DUE")


if __name__ == "__main__":
    unittest.main()
