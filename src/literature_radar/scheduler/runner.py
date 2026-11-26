"""Run due Topic/Provider pairs once; the host decides when to invoke this CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.delivery.notifiers import configured_notifiers
from literature_radar.workflow.state import read_state

SCHEDULE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "required": ["schema_version", "topics"], "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "topics": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["config_dir", "providers"],
            "additionalProperties": False,
            "properties": {
                "config_dir": {"type": "string", "minLength": 1},
                "providers": {"type": "object", "minProperties": 1,
                              "propertyNames": {"enum": ["openalex", "crossref", "europepmc", "pubmed", "biorxiv"]},
                              "additionalProperties": {"anyOf": [{"type": "null"},
                                                            {"type": "number", "minimum": 1, "maximum": 720}]}},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 10000},
                "max_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "overlap_days": {"type": "integer", "minimum": 0, "maximum": 365},
                "reconcile_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                "feedback_file": {"type": "string", "minLength": 1},
            }}},
    },
}
Draft202012Validator.check_schema(SCHEDULE_SCHEMA)


def _within(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("scheduled config_dir must be a safe relative path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("scheduled config_dir escapes schedule root")
    return resolved


def run_due(schedule_file: Path, database: Path, *, now: datetime | None = None,
            providers: Mapping[str, object] | None = None,
            dry_run: bool = False) -> dict:
    schedule_file = Path(schedule_file).resolve()
    try:
        specification = json.loads(schedule_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule file cannot be read as UTF-8 JSON") from exc
    errors = sorted(Draft202012Validator(SCHEDULE_SCHEMA).iter_errors(specification),
                    key=lambda error: tuple(map(str, error.absolute_path)))
    if errors:
        field = ".".join(map(str, errors[0].absolute_path)) or "$"
        raise ValueError(f"invalid schedule at {field}: {errors[0].validator} constraint")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("scheduler time must include a timezone")
    instant = instant.astimezone(timezone.utc)
    store = None if dry_run else AcademicStore(database)
    result = {"status": "SUCCESS", "jobs": []}
    seen = set()
    for entry in specification["topics"]:
        config_dir = _within(schedule_file.parent, entry["config_dir"])
        bundle = load_bundle(config_dir)
        workflow_file = config_dir / "workflow-state.json"
        workflow_state = read_state(config_dir)["state"] if workflow_file.exists() else None
        channels, notifiers = configured_notifiers(bundle)
        feedback_file = _within(schedule_file.parent, entry["feedback_file"]) if entry.get("feedback_file") else None
        topic_id = bundle["topic"]["id"]
        for provider_id, interval_hours in entry["providers"].items():
            interval_hours = interval_hours or (168 if bundle["topic"].get("extensions", {}).get("weekly") else 24)
            key = (topic_id, provider_id)
            if key in seen:
                raise ValueError("duplicate scheduled Topic/Provider pair")
            seen.add(key)
            if workflow_state is not None:
                result["jobs"].append({"topic_id": topic_id, "provider_id": provider_id,
                                       "status": "WEEKLY_CLI_REQUIRED" if workflow_state == "WEEKLY_MONITORING" else "BOOTSTRAP_PENDING"})
                continue
            lease_until = (instant + timedelta(seconds=entry.get("max_seconds", 300) + 120)).isoformat()
            if dry_run:
                result["jobs"].append({"topic_id": topic_id, "provider_id": provider_id,
                                       "status": "DRY_RUN"})
                continue
            if not store.claim_due(*key, now=instant.isoformat(), lease_until=lease_until):
                result["jobs"].append({"topic_id": topic_id, "provider_id": provider_id,
                                       "status": "NOT_DUE"})
                continue
            try:
                summary = discover_incremental(
                    config_dir, [provider_id], database_path=database,
                    max_pages=entry.get("max_pages", 200),
                    max_seconds=entry.get("max_seconds", 300),
                    overlap_days=entry.get("overlap_days", 7),
                    reconcile_days=entry.get("reconcile_days"), strict_records=True,
                    providers=providers, feedback_path=feedback_file,
                    channels=channels, notifiers=notifiers, current_time=instant)
                status = summary["providers"][provider_id]["status"]
            except Exception as exc:
                status = "FAILED"
                result["jobs"].append({"topic_id": topic_id, "provider_id": provider_id,
                                       "error_category": type(exc).__name__, "status": status})
            else:
                result["jobs"].append({"topic_id": topic_id, "provider_id": provider_id,
                                       "status": status,
                                       "pages": summary["providers"][provider_id]["pages"]})
            delay = (timedelta(hours=interval_hours) if status == "SUCCESS" else
                     timedelta(minutes=30))
            store.finish_due(*key, next_due_at=(instant + delay).isoformat(),
                             status=status, lease_until=lease_until)
            if status not in ("SUCCESS", "NOT_DUE"):
                result["status"] = "PARTIAL_SUCCESS"
    return result
