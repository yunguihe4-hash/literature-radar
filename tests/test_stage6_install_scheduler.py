"""New-user database and schedule generation, wholly inside temporary paths."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from literature_radar.cli import main
from literature_radar.scheduler.systemd import generate_weekly_units
from literature_radar.workflow.weekly_scheduler import _last_monday, discover_enabled, run_scheduled


class Stage6InstallSchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_database_init_and_idempotent_open(self):
        target = self.root / "data" / "academic.sqlite3"
        self.assertEqual(main(["database", "init", "--database", str(target)]), 0)
        self.assertTrue(target.is_file())
        self.assertEqual(main(["database", "init", "--database", str(target)]), 0)

    def test_weekly_activation_and_status_cli_exit_successfully(self):
        from literature_radar.topics.manager import initialize
        from tests.test_stage37_39_workflow import intake

        topic = initialize(self.root / "config", intake("fictional-weekly"))
        self.assertEqual(main(["weekly", "activate", "--config-dir", str(topic),
                               "--skip-bootstrap", "--reason", "isolated fixture"]), 0)
        self.assertEqual(main(["weekly", "status", "--config-dir", str(topic)]), 0)

    def test_custom_weekly_slot_and_timezone_are_used_for_fresh_topic(self):
        from literature_radar.topics.manager import initialize
        from literature_radar.workflow.weekly import activate_weekly
        from tests.test_stage37_39_workflow import intake

        config = initialize(self.root / "config", intake("fictional-alpha"))
        activate_weekly(config, skip_bootstrap=True, reason="isolated scheduler fixture")
        result = run_scheduled(self.root / "config", self.root / "data.sqlite3",
                               self.root / "out", timezone="Europe/Berlin",
                               weekday=2, local_time="14:30",
                               now=datetime(2026, 11, 26, 12, tzinfo=timezone.utc),
                               runner=lambda _config, _db, _out, **kwargs: {
                                   "status": "SUCCESS", "coverage_complete": True,
                                   "overlap_window": {"end": kwargs["scheduled_at"].date().isoformat()},
                                   "run_id": "fictional", "candidate_count": 0,
                                   "recommended_count": 0, "pending_provider_windows": []})
        self.assertEqual(result["topics"][0]["runs"][0]["scheduled_time"],
                         "2026-11-25T14:30:00+01:00")
        self.assertEqual(result["topics"][0]["next_scheduled_time"],
                         "2026-12-02T14:30:00+01:00")
        self.assertEqual(_last_monday(datetime(2026, 11, 26, tzinfo=timezone.utc),
                                      ZoneInfo("Europe/Berlin"), 2,
                                      datetime.strptime("14:30", "%H:%M").time()).date().isoformat(),
                         "2026-11-25")

    def test_topic_intake_can_disable_scheduled_weekly(self):
        from literature_radar.topics.manager import initialize
        from literature_radar.workflow.weekly import activate_weekly
        from tests.test_stage37_39_workflow import intake

        spec = intake("fictional-alpha")
        spec["weekly_enabled"] = False
        config = initialize(self.root / "config", spec)
        activate_weekly(config, skip_bootstrap=True, reason="isolated disable fixture")
        found, errors = discover_enabled(self.root / "config")
        self.assertEqual((found, errors), ([], []))

    def test_generated_units_are_named_and_never_overwrite(self):
        cli = self.root / "venv" / "bin" / "literature-radar"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o755)
        config = self.root / "config"
        config.mkdir()
        units = self.root / "units"
        result = generate_weekly_units(units, cli=cli, config_root=config,
                                       database=self.root / "db.sqlite3",
                                       output_root=self.root / "output",
                                       cards_dir=self.root / "cards",
                                       unit_name="fictional-radar", timezone="Europe/Berlin",
                                       weekday=2, local_time="14:30")
        service = Path(result["service"])
        timer = Path(result["timer"])
        self.assertIn("--timezone", service.read_text())
        self.assertIn("--weekday", service.read_text())
        self.assertIn("OnCalendar=Wed *-*-* 14:30:00 Europe/Berlin", timer.read_text())
        self.assertIn("Persistent=true", timer.read_text())
        with self.assertRaises(FileExistsError):
            generate_weekly_units(units, cli=cli, config_root=config,
                                  database=self.root / "db.sqlite3",
                                  output_root=self.root / "output",
                                  cards_dir=self.root / "cards",
                                  unit_name="fictional-radar")
        self.assertEqual(result["unit_name"], "fictional-radar")


if __name__ == "__main__":
    unittest.main()
