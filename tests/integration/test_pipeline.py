import json
import tempfile
import unittest
from pathlib import Path
from literature_radar.config import load_bundle
from literature_radar.pipeline import run
from literature_radar.sources.fixture import FixtureProvider
from literature_radar.database.store import PaperStore
from literature_radar.feedback.store import add_feedback

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/synthetic-topic"
FIXTURE = ROOT / "tests/fixtures/synthetic_papers.json"

class Recorder:
    def __init__(self): self.calls = []
    def notify(self, title, message): self.calls.append((title, message))

class PipelineTests(unittest.TestCase):
    def test_complete_offline_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            note = Recorder()
            result = run(EXAMPLE, FixtureProvider(FIXTURE), tmp / "output/result.json", database_path=tmp / "db/papers.sqlite3", notifier=note, run_directory=tmp / "runs/demo" )
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["papers"][0]["attention"]["priority"], "HIGH")
            self.assertEqual(result["papers"][0]["alignment"]["transfers"][0]["type"], "method_transfer")
            self.assertEqual(result["papers"][1]["attention"]["priority"], "LOW")
            self.assertEqual(len(note.calls), 1)
            self.assertEqual(PaperStore(tmp / "db/papers.sqlite3").count("coating-demo"), 2)
            feedback = add_feedback(tmp / "feedback/events.json", result["topic_id"], result["papers"][0]["key"], "keep", "Synthetic test signal")
            self.assertEqual(feedback["decision"], "keep")
            self.assertTrue((tmp / "output/result.json").exists())
            self.assertTrue((tmp / "runs/demo/candidates.json").exists())

    def test_new_topic_requires_only_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = tmp / "acoustic"
            config.mkdir()
            bundle = load_bundle(EXAMPLE)
            bundle["topic"] = {"schema_version":1,"id":"acoustic-demo","display_name":"Synthetic acoustic foam","search_terms":["foam"],"sources":["synthetic-index"]}
            for part in ("focus", "profile", "rules", "ontology", "phase", "attention-memory", "delivery"):
                bundle[part]["topic_id"] = "acoustic-demo"
            bundle["focus"]["search_terms"] = ["sound dampening"]
            bundle["profile"] = {"schema_version":1,"topic_id":"acoustic-demo","questions":[{"id":"attenuation","label":"sound attenuation","aliases":["sound attenuation"]}],"methods":[{"id":"resonance","label":"resonance measurement","aliases":["resonance measurement"]}],"applications":[]}
            bundle["rules"] = {"schema_version":1,"topic_id":"acoustic-demo","positive":[{"term":"resonance measurement","dimension":"method","weight":3}],"negative":[],"priority_thresholds":{"high":7,"medium":3}}
            bundle["ontology"] = {"schema_version":1,"topic_id":"acoustic-demo","concepts":[],"relations":[]}
            bundle["phase"] = {"schema_version":1,"topic_id":"acoustic-demo","priority_question_ids":["attenuation"],"priority_method_ids":[]}
            bundle["attention-memory"] = {"schema_version":1,"topic_id":"acoustic-demo","preferred_concept_ids":[]}
            for part, data in bundle.items(): (config / f"{part}.json").write_text(json.dumps(data))
            fixture = tmp / "acoustic-fixture.json"
            fixture.write_text(json.dumps([{"source_id":"SYN-101","title":"Synthetic Foam Study","abstract":"Sound attenuation by foam tested with resonance measurement.","source":"synthetic-index","identifiers":{}}]))
            result = run(config, FixtureProvider(fixture), tmp / "acoustic-result.json")
            self.assertEqual((result["topic_id"], result["count"]), ("acoustic-demo", 1))
            self.assertEqual(result["papers"][0]["attention"]["priority"], "HIGH")
