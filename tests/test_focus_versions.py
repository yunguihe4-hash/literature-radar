import json
import tempfile
import unittest
from pathlib import Path

from literature_radar.topics.focus import set_focus, focus_history
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake


class FocusVersionTests(unittest.TestCase):
    def test_version_snapshots_are_immutable_and_topic_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = initialize(Path(temporary) / "config", intake("fictional-alpha"))
            original = json.loads((config / "focus.json").read_text())
            result = set_focus(config, ["fictional receptor", "fictional method"],
                               reason="new fictional research phase")
            self.assertEqual(result["version"], "v002")
            self.assertEqual(json.loads((config / "focuses/v001.json").read_text()), original)
            self.assertEqual([item["version"] for item in focus_history(config)], ["v001", "v002"])
            current = json.loads((config / "focus.json").read_text())
            self.assertEqual(json.loads((config / "focuses/v002.json").read_text()), current)
            (config / "focuses/v002.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Focus history"):
                focus_history(config)
