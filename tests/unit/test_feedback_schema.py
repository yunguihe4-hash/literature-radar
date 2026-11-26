"""Feedback schema enforcement and atomic file integrity."""
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_radar.config import SchemaValidationError
from literature_radar import cli
from literature_radar.feedback.store import add_feedback, load_feedback


class FeedbackSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feedback.json"
        self.valid_entry = {
            "topic_id": "synthetic",
            "paper_key": "source:fixture:one",
            "decision": "keep",
            "reason": "Fictional review",
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }

    def write_history(self, entry=None):
        self.path.write_text(json.dumps({"schema_version": 1, "entries": [entry or self.valid_entry]}))
        return self.path.read_bytes()

    def test_valid_feedback_write_and_history_read(self):
        before = self.write_history()
        loaded = load_feedback(self.path)
        self.assertEqual(loaded["entries"][0], self.valid_entry)
        added = add_feedback(self.path, "synthetic", "source:fixture:two", "later", "Fictional follow-up")
        self.assertEqual(added["decision"], "later")
        self.assertEqual(len(load_feedback(self.path)["entries"]), 2)
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_missing_required_field_is_rejected_without_change(self):
        entry = {k: v for k, v in self.valid_entry.items() if k != "reason"}
        before = self.write_history(entry)
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.reason"):
            add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_existing_decision_is_rejected_without_change(self):
        entry = {**self.valid_entry, "decision": "unknown"}
        before = self.write_history(entry)
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.decision"):
            add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_type_is_rejected_without_change(self):
        entry = {**self.valid_entry, "paper_key": 42}
        before = self.write_history(entry)
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.paper_key"):
            add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_datetime_is_rejected_without_change(self):
        entry = {**self.valid_entry, "recorded_at": "yesterday"}
        before = self.write_history(entry)
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.recorded_at"):
            add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_field_is_rejected(self):
        entry = {**self.valid_entry, "unexpected": True}
        before = self.write_history(entry)
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.unexpected"):
            add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_new_record_never_creates_file(self):
        with self.assertRaisesRegex(SchemaValidationError, "entries.0.paper_key"):
            add_feedback(self.path, "synthetic", 42, "keep", "Reason")
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_preserves_existing_file(self):
        before = self.write_history()
        with patch("literature_radar.feedback.store.os.replace", side_effect=OSError("replacement failed")):
            with self.assertRaises(OSError):
                add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["feedback.json"])

    def test_temp_write_failure_preserves_existing_file(self):
        before = self.write_history()
        with patch("literature_radar.feedback.store.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaises(OSError):
                add_feedback(self.path, "synthetic", "new", "keep", "Reason")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["feedback.json"])

    def test_cli_rejects_invalid_decision_with_field(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = cli.main([
                "feedback", "--file", str(self.path), "--topic", "synthetic",
                "--paper-key", "source:fixture:one", "--decision", "unknown",
                "--reason", "Fictional review",
            ])
        self.assertEqual(code, 2)
        self.assertIn("feedback.entries.new.decision", error.getvalue())
        self.assertFalse(self.path.exists())
