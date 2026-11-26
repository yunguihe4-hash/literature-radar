import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from literature_radar.config import ConfigurationError, load_bundle, validate_bundle

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/synthetic-topic"

class ConfigurationTests(unittest.TestCase):
    def test_example_valid(self):
        self.assertEqual(load_bundle(EXAMPLE)["topic"]["id"], "coating-demo")

    def test_invalid_reference_has_clear_error(self):
        bundle = load_bundle(EXAMPLE)
        bundle["phase"]["priority_question_ids"] = ["missing-id"]
        with self.assertRaisesRegex(ConfigurationError, "unknown ids"):
            validate_bundle(bundle)

    def test_missing_config_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigurationError, "missing configuration file"):
                load_bundle(Path(tmp))

    def test_schemas_are_json(self):
        paths = list(files("literature_radar.schemas").iterdir())
        self.assertEqual(len([path for path in paths if path.name.endswith(".schema.json")]), 9)
        for path in paths:
            if not path.name.endswith(".schema.json"):
                continue
            self.assertEqual(json.loads(path.read_text())["$schema"], "https://json-schema.org/draft/2020-12/schema")
