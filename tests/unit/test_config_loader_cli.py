"""Read-only loader, error category and CLI behavior."""
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_radar import cli
from literature_radar.config import (
    ConfigSyntaxError, FileLoadingError, SchemaValidationError,
    UnsupportedVersionError, validate_config_dir,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/synthetic-topic"


class LoaderCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "config"
        shutil.copytree(EXAMPLE, self.root)

    def test_missing_file(self):
        (self.root / "profile.json").unlink()
        with self.assertRaises(FileLoadingError):
            validate_config_dir(self.root)

    def test_json_only_does_not_silently_parse_toml(self):
        (self.root / "profile.json").unlink()
        (self.root / "profile.toml").write_text('schema_version = 1')
        with self.assertRaises(FileLoadingError):
            validate_config_dir(self.root)

    def test_malformed_json(self):
        (self.root / "rules.json").write_text("{broken")
        with self.assertRaises(ConfigSyntaxError):
            validate_config_dir(self.root)

    def test_invalid_encoding(self):
        (self.root / "ontology.json").write_bytes(b"\xff")
        with self.assertRaises(FileLoadingError):
            validate_config_dir(self.root)

    def test_deterministic_order_and_read_only(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob("*.json")}
        first = validate_config_dir(self.root)
        second = validate_config_dir(self.root)
        self.assertEqual(list(first.bundle), list(second.bundle))
        self.assertEqual(first.bundle, second.bundle)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob("*.json")})

    def test_json_cli_success_and_no_search(self):
        output = io.StringIO()
        with patch.object(cli, "FixtureProvider", side_effect=AssertionError("discovery started")):
            with contextlib.redirect_stdout(output):
                code = cli.main(["config", "validate", "--config-dir", str(self.root), "--format", "json"])
        payload = json.loads(output.getvalue())
        self.assertEqual((code, payload["status"], payload["configuration_files_validated"]), (0, "PASS", 8))

    def test_json_cli_failure_file_field_and_no_database_write(self):
        db = Path(self.temp.name) / "papers.sqlite3"
        rules = self.root / "rules.json"
        data = json.loads(rules.read_text())
        data["priority_thresholds"]["high"] = "bad"
        rules.write_text(json.dumps(data))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["config", "validate", "--config-dir", str(self.root), "--format", "json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["category"], "schema")
        self.assertIn("rules.json", payload["file"])
        self.assertIn("priority_thresholds.high", payload["field"])
        self.assertFalse(db.exists())

    def test_unsupported_version_category(self):
        topic = self.root / "topic.json"
        data = json.loads(topic.read_text())
        data["schema_version"] = 99
        topic.write_text(json.dumps(data))
        with self.assertRaises(UnsupportedVersionError):
            validate_config_dir(self.root)

    def test_legacy_cli_alias(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["validate", "--config", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])
