"""Cross-file identity, reference and path constraints."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from literature_radar.config import (
    CrossReferenceError, FileLoadingError, load_bundle, validate_config_dir,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/synthetic-topic"


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "config"
        shutil.copytree(EXAMPLE, self.root)

    def edit(self, part, change):
        path = self.root / f"{part}.json"
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))
        return path

    def test_valid_bundle_passes(self):
        report = validate_config_dir(self.root)
        self.assertEqual(report.files_validated, 8)
        self.assertGreater(report.cross_references_checked, 0)

    def test_optional_memory_and_delivery_may_be_absent(self):
        (self.root / "attention-memory.json").unlink()
        (self.root / "delivery.json").unlink()
        report = validate_config_dir(self.root)
        self.assertEqual(report.files_validated, 6)

    def test_missing_focus_reference_fails(self):
        self.edit("topic", lambda d: d.update(focus_file="missing-focus.json"))
        with self.assertRaisesRegex(FileLoadingError, "missing configuration file"):
            validate_config_dir(self.root)

    def test_unknown_topic_identity_fails(self):
        self.edit("topic", lambda d: d.update(id="another-synthetic-topic"))
        with self.assertRaisesRegex(CrossReferenceError, "topic_id"):
            validate_config_dir(self.root)

    def test_mismatched_profile_identity_fails(self):
        self.edit("profile", lambda d: d.update(topic_id="other-demo"))
        with self.assertRaisesRegex(CrossReferenceError, "profile.json"):
            validate_config_dir(self.root)

    def test_invalid_ontology_reference_fails(self):
        self.edit("ontology", lambda d: d["relations"][0].update(to="unknown-concept"))
        with self.assertRaisesRegex(CrossReferenceError, "relations.0.to"):
            validate_config_dir(self.root)

    def test_invalid_phase_reference_fails(self):
        self.edit("phase", lambda d: d["priority_question_ids"].append("unknown-question"))
        with self.assertRaisesRegex(CrossReferenceError, "priority_question_ids.1"):
            validate_config_dir(self.root)

    def test_duplicate_profile_ids_fail(self):
        self.edit("profile", lambda d: d["methods"][0].update(id=d["questions"][0]["id"]))
        with self.assertRaisesRegex(CrossReferenceError, "duplicate identifier"):
            validate_config_dir(self.root)

    def test_duplicate_ontology_id_fails(self):
        self.edit("ontology", lambda d: d["concepts"][0].update(id="durability"))
        with self.assertRaisesRegex(CrossReferenceError, "duplicate identifier"):
            validate_config_dir(self.root)

    def test_unknown_memory_identifier_fails(self):
        self.edit("attention-memory", lambda d: d["preferred_concept_ids"].append("missing-concept"))
        with self.assertRaisesRegex(CrossReferenceError, "preferred_concept_ids.1"):
            validate_config_dir(self.root)

    def test_absolute_and_traversal_focus_paths_fail(self):
        for reference in ("/outside.json", "../outside.json", "nested/../../outside.json"):
            with self.subTest(reference=reference):
                self.edit("topic", lambda d: d.update(focus_file=reference))
                with self.assertRaises(CrossReferenceError):
                    validate_config_dir(self.root)

    def test_focus_symlink_outside_root_fails(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text((self.root / "focus.json").read_text())
        (self.root / "link.json").symlink_to(outside)
        self.edit("topic", lambda d: d.update(focus_file="link.json"))
        with self.assertRaises(CrossReferenceError):
            validate_config_dir(self.root)

    def test_focus_reference_to_directory_fails(self):
        (self.root / "folder").mkdir()
        self.edit("topic", lambda d: d.update(focus_file="folder"))
        with self.assertRaises(FileLoadingError):
            validate_config_dir(self.root)

    def test_delivery_path_is_safe_and_relative(self):
        self.edit("delivery", lambda d: d["output"].update(relative_path="../escape.json"))
        with self.assertRaisesRegex(CrossReferenceError, "output.relative_path"):
            validate_config_dir(self.root)

    def test_delivery_notification_provider_consistency(self):
        self.edit("delivery", lambda d: d["notification"].update(enabled=True, provider="none"))
        with self.assertRaisesRegex(CrossReferenceError, "notification.provider"):
            validate_config_dir(self.root)
