"""Strict, offline configuration loading and cross-document validation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REQUIRED_PARTS = ("topic", "focus", "profile", "rules", "ontology", "phase")
OPTIONAL_PARTS = ("attention-memory", "delivery")
CONFIG_PARTS = REQUIRED_PARTS + OPTIONAL_PARTS


class ConfigurationError(ValueError):
    category = "configuration"

    def __init__(self, message: str, *, file: Path | str | None = None, field: str = "$", correction: str = ""):
        self.file = str(file) if file is not None else ""
        self.field = field
        self.correction = correction
        prefix = f"{self.file}: {self.field}: " if self.file else ""
        super().__init__(prefix + message)


class FileLoadingError(ConfigurationError):
    category = "file_loading"


class ConfigSyntaxError(ConfigurationError):
    category = "syntax"


class SchemaValidationError(ConfigurationError):
    category = "schema"


class CrossReferenceError(ConfigurationError):
    category = "cross_reference"


class UnsupportedVersionError(ConfigurationError):
    category = "unsupported_version"


@dataclass(frozen=True)
class ValidationReport:
    bundle: dict[str, dict]
    schemas_validated: int
    files_validated: int
    cross_references_checked: int


def _safe_actual(value: Any) -> str:
    if isinstance(value, str):
        return f"string of length {len(value)}"
    if isinstance(value, (int, float, bool)) or value is None:
        return repr(value)
    return type(value).__name__


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileLoadingError("missing configuration file", file=path, correction="Create the referenced JSON file.") from exc
    except UnicodeError as exc:
        raise FileLoadingError("file is not valid UTF-8", file=path, correction="Save the file as UTF-8 JSON.") from exc
    except OSError as exc:
        raise FileLoadingError(f"cannot read file: {exc.strerror}", file=path) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigSyntaxError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}", file=path, correction="Fix JSON syntax.") from exc
    if not isinstance(value, dict):
        raise SchemaValidationError("document must be an object", file=path, correction="Use a JSON object at the top level.")
    return value


def _schema_files():
    root = files("literature_radar.schemas")
    return {name: root.joinpath(f"{name}.schema.json") for name in (*CONFIG_PARTS, "feedback")}


def load_schemas() -> dict[str, dict]:
    schemas = {}
    ids = set()
    for name, path in _schema_files().items():
        schema = _read_json(path)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise SchemaValidationError(f"invalid JSON Schema: {exc}", file=path) from exc
        identifier = schema.get("$id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise SchemaValidationError("missing or duplicate $id", file=path, field="$id")
        ids.add(identifier)
        def check_refs(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and (not isinstance(value, str) or not value.startswith("#/")):
                        raise SchemaValidationError("only local fragment $ref values are allowed", file=path, field="$ref")
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)
        check_refs(schema)
        schemas[name] = schema
    return schemas


def _validate_schema(name: str, value: dict, schema: dict, path: Path | str) -> None:
    version = value.get("schema_version")
    if type(version) is not int or version != 1:
        raise UnsupportedVersionError(
            f"unsupported schema_version {_safe_actual(version)}; expected 1",
            file=path, field="schema_version",
            correction="Use a supported v1 document; migration is never automatic.",
        )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: (list(map(str, error.absolute_path)), error.message))
    if errors:
        error = errors[0]
        field = ".".join([name, *map(str, error.absolute_path)])
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = sorted(set(error.schema.get("required", ())) - set(error.instance))
            if missing:
                field = f"{field}.{missing[0]}"
        elif error.validator == "additionalProperties" and isinstance(error.instance, dict):
            unknown = sorted(set(error.instance) - set(error.schema.get("properties", ())))
            if unknown:
                field = f"{field}.{unknown[0]}"
        expected = repr(error.validator_value) if error.validator in {"type", "enum", "minimum", "maximum", "minLength", "minItems", "uniqueItems", "additionalProperties", "pattern", "format"} else "required schema constraint"
        raise SchemaValidationError(
            f"failed {error.validator} constraint; expected {expected}; actual: {_safe_actual(error.instance)}",
            file=path, field=field,
            correction="Correct the value to satisfy the named schema constraint.",
        )


def _relative_path(root: Path, value: str, *, source: Path, field: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or "\\" in value or ":" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise CrossReferenceError("path must be a safe relative path", file=source, field=field, correction="Use a path below the configured root without '..'.")
    resolved_root = root.resolve()
    resolved = (resolved_root / path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise CrossReferenceError("path resolves outside the configured root", file=source, field=field)
    return resolved


def _check_references(bundle: dict[str, dict], paths: dict[str, Path], config_root: Path | None) -> int:
    topic_id = bundle["topic"]["id"]
    count = 0
    for part in REQUIRED_PARTS[1:] + tuple(x for x in OPTIONAL_PARTS if x in bundle):
        if part == "attention-memory" and "topic_id" not in bundle[part]:
            continue
        count += 1
        if bundle[part]["topic_id"] != topic_id:
            raise CrossReferenceError(
                "topic_id does not match topic.id",
                file=paths[part], field="topic_id", correction="Set topic_id to the ID in topic.json.",
            )
    profile = bundle["profile"]
    seen: dict[str, str] = {}
    for part in ("questions", "methods", "applications"):
        for index, entry in enumerate(profile[part]):
            identifier = entry["id"]
            if identifier in seen:
                raise CrossReferenceError(f"duplicate identifier; first defined at {seen[identifier]}", file=paths["profile"], field=f"{part}.{index}.id")
            seen[identifier] = f"profile.{part}"
    for index, entry in enumerate(bundle["ontology"]["concepts"]):
        identifier = entry["id"]
        if identifier in seen:
            raise CrossReferenceError(f"duplicate identifier; first defined at {seen[identifier]}", file=paths["ontology"], field=f"concepts.{index}.id")
        seen[identifier] = "ontology.concepts"
    question_ids = {e["id"] for e in profile["questions"]}
    method_ids = {e["id"] for e in profile["methods"]}
    for field, valid in (("priority_question_ids", question_ids), ("priority_method_ids", method_ids)):
        for index, identifier in enumerate(bundle["phase"][field]):
            count += 1
            if identifier not in valid:
                raise CrossReferenceError("unknown ids: referenced priority is absent", file=paths["phase"], field=f"{field}.{index}", correction="Define the ID in the matching Profile section or remove the reference.")
    for index, relation in enumerate(bundle["ontology"]["relations"]):
        for endpoint in ("from", "to"):
            count += 1
            if relation[endpoint] not in seen:
                raise CrossReferenceError("unknown identifier", file=paths["ontology"], field=f"relations.{index}.{endpoint}", correction="Define the ID in Profile or Ontology.")
    if "attention-memory" in bundle:
        for index, identifier in enumerate(bundle["attention-memory"]["preferred_concept_ids"]):
            count += 1
            if identifier not in seen:
                raise CrossReferenceError("unknown identifier", file=paths["attention-memory"], field=f"preferred_concept_ids.{index}")
    thresholds = bundle["rules"]["priority_thresholds"]
    if thresholds["high"] < thresholds["medium"]:
        raise CrossReferenceError("high threshold must be >= medium threshold", file=paths["rules"], field="priority_thresholds.high")
    if "delivery" in bundle:
        delivery = bundle["delivery"]
        _relative_path(config_root or Path("."), delivery["output"]["relative_path"], source=paths["delivery"], field="output.relative_path")
        count += 1
        if delivery["notification"]["enabled"] and delivery["notification"]["provider"] == "none":
            raise CrossReferenceError("enabled notification requires a provider", file=paths["delivery"], field="notification.provider")
    return count


def validate_bundle(bundle: dict[str, dict]) -> dict[str, dict]:
    schemas = load_schemas()
    paths = {name: Path(f"{name}.json") for name in bundle}
    for part in REQUIRED_PARTS:
        if part not in bundle:
            raise FileLoadingError("missing configuration file", file=paths.get(part, Path(f"{part}.json")))
    for part in CONFIG_PARTS:
        if part in bundle:
            _validate_schema(part, bundle[part], schemas[part], paths[part])
    _check_references(bundle, paths, None)
    return bundle


def validate_config_dir(config_dir: Path) -> ValidationReport:
    if Path(config_dir).is_symlink():
        raise CrossReferenceError("configuration directory must not be a symbolic link",
                                  file=config_dir, field="$", correction="Use the real private directory path.")
    root = Path(config_dir).resolve()
    if not root.is_dir():
        raise FileLoadingError("configuration directory does not exist", file=root)
    schemas = load_schemas()
    bundle: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    for part in CONFIG_PARTS:
        if part == "focus" and "topic" in bundle:
            filename = bundle["topic"].get("focus_file", "focus.json")
            path = _relative_path(root, filename, source=paths["topic"], field="focus_file")
        else:
            path = _relative_path(root, f"{part}.json", source=root, field=f"{part}_file")
        if part in OPTIONAL_PARTS and not path.exists():
            continue
        paths[part] = path
        value = _read_json(path)
        _validate_schema(part, value, schemas[part], path)
        bundle[part] = value
    references = _check_references(bundle, paths, root)
    return ValidationReport(bundle, len(CONFIG_PARTS), len(bundle), references)


def load_bundle(config_dir: Path) -> dict[str, dict]:
    return validate_config_dir(config_dir).bundle


def load_attention_memory(path: Path, bundle: dict[str, dict]) -> dict:
    """Validate an explicitly supplied local memory file before using it."""
    path = Path(path)
    value = _read_json(path)
    _validate_schema("attention-memory", value, load_schemas()["attention-memory"], path)
    topic_id = bundle["topic"]["id"]
    if value.get("topic_id", topic_id) != topic_id:
        raise CrossReferenceError("topic_id does not match topic.id", file=path, field="topic_id")
    known = {
        entry["id"]
        for section in ("questions", "methods", "applications")
        for entry in bundle["profile"][section]
    } | {entry["id"] for entry in bundle["ontology"]["concepts"]}
    for index, identifier in enumerate(value["preferred_concept_ids"]):
        if identifier not in known:
            raise CrossReferenceError("unknown identifier", file=path, field=f"preferred_concept_ids.{index}")
    return value


def validate_feedback_document(value: Any, path: Path | str) -> dict:
    """Validate local feedback data with the packaged Feedback schema."""
    if not isinstance(value, dict):
        raise SchemaValidationError("document must be an object", file=path, field="feedback")
    _validate_schema("feedback", value, load_schemas()["feedback"], path)
    return value
