"""Draft 2020-12 schema quality and document-level behavior."""
import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from literature_radar.config import (
    SchemaValidationError, UnsupportedVersionError, load_bundle,
    load_schemas, validate_bundle,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/synthetic-topic"


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.bundle = copy.deepcopy(load_bundle(EXAMPLE))

    def test_all_schemas_self_validate_and_have_unique_ids(self):
        schemas = load_schemas()
        self.assertEqual(len(schemas), 9)  # Eight configuration types plus Feedback data.
        ids = []
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
            ids.append(schema["$id"])
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(len(ids), len(set(ids)))

    def test_both_synthetic_configurations_pass(self):
        for name in ("synthetic-topic", "synthetic-acoustic"):
            self.assertEqual(len(load_bundle(ROOT / "examples" / name)), 8)

    def test_missing_required_property_fails(self):
        del self.bundle["topic"]["display_name"]
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)

    def test_invalid_type_fails(self):
        self.bundle["phase"]["priority_question_ids"] = "not an array"
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)

    def test_invalid_enum_fails(self):
        self.bundle["rules"]["positive"][0]["dimension"] = "unsupported"
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)

    def test_numeric_minimum_and_maximum_fail(self):
        for value in (-1, 1000000001):
            with self.subTest(value=value):
                bundle = copy.deepcopy(self.bundle)
                bundle["rules"]["positive"][0]["weight"] = value
                with self.assertRaises(SchemaValidationError):
                    validate_bundle(bundle)

    def test_invalid_array_structure_and_uniqueness_fail(self):
        self.bundle["topic"]["sources"] = []
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)
        self.bundle["topic"]["sources"] = ["synthetic-index", "synthetic-index"]
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)

    def test_unsupported_version_is_distinct(self):
        self.bundle["topic"]["schema_version"] = 2
        with self.assertRaises(UnsupportedVersionError):
            validate_bundle(self.bundle)

    def test_unknown_property_rejected_extensions_allowed(self):
        self.bundle["topic"]["unknown_key"] = "value"
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)
        del self.bundle["topic"]["unknown_key"]
        self.bundle["topic"]["extensions"] = {"custom_meta": {"arbitrary": [1, 2]}}
        validate_bundle(self.bundle)

    def test_format_checker_rejects_bad_datetime(self):
        self.bundle["focus"]["created_at"] = "yesterday"
        with self.assertRaises(SchemaValidationError):
            validate_bundle(self.bundle)

    def test_local_refs_only(self):
        for schema in load_schemas().values():
            raw = json.dumps(schema)
            self.assertNotIn('"$ref": "http', raw)
