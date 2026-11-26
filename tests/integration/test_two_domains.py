"""The same implementation accepts two independent fictional domains."""
import tempfile
import unittest
from pathlib import Path

from literature_radar.config import validate_config_dir
from literature_radar.pipeline import run
from literature_radar.sources.fixture import FixtureProvider

ROOT = Path(__file__).resolve().parents[2]


class Recorder:
    def __init__(self):
        self.events = []
    def notify(self, title, message):
        self.events.append((title, message))


class DomainCompatibilityTests(unittest.TestCase):
    def test_two_domains_validate_and_run_without_source_edits(self):
        coating = ROOT / "examples/synthetic-topic"
        acoustic = ROOT / "examples/synthetic-acoustic"
        self.assertNotEqual(validate_config_dir(coating).bundle["profile"], validate_config_dir(acoustic).bundle["profile"])
        with tempfile.TemporaryDirectory() as temp:
            for name, config, fixture in (
                ("coating", coating, ROOT / "tests/fixtures/synthetic_papers.json"),
                ("acoustic", acoustic, ROOT / "tests/fixtures/synthetic_acoustic_papers.json"),
            ):
                recorder = Recorder()
                result = run(config, FixtureProvider(fixture), Path(temp) / f"{name}.json",
                             memory_path=config / "attention-memory.json", notifier=recorder)
                self.assertEqual(result["count"], 2)
                self.assertEqual(result["papers"][0]["attention"]["priority"], "HIGH")
                self.assertGreater(result["papers"][0]["attention"]["memory_hits"], 0)
                self.assertEqual(len(recorder.events), 1)
