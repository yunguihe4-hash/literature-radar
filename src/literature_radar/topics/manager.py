"""Build complete v1 Topic bundles from a short, user-owned intake form."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from literature_radar.config import CONFIG_PARTS, load_bundle, validate_config_dir
from literature_radar.private_paths import ensure_private_path

PROVIDERS = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")
_ID = re.compile(r"^[a-z][a-z0-9-]*$")


def _strings(value, field: str, *, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be an array of nonempty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if required and not result:
        raise ValueError(f"{field} must contain at least one item")
    return result


def _id(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not _ID.fullmatch(text):
        raise ValueError("Topic ID must begin with a Latin letter and contain only lowercase letters, digits and hyphens")
    return text


def _entries(values: list[str], prefix: str) -> list[dict]:
    entries, used = [], set()
    for index, label in enumerate(values, 1):
        identifier = _id(f"{prefix}-{label}")[:70].rstrip("-")
        if identifier in used:
            identifier = f"{prefix}-{index}"
        used.add(identifier)
        entries.append({"id": identifier, "label": label, "aliases": [label]})
    return entries


def build_bundle(specification: dict) -> dict[str, dict]:
    """Generate data only; validation and persistence are separate steps."""
    if not isinstance(specification, dict):
        raise ValueError("Topic intake must be a JSON object")
    name = specification.get("name")
    goal = specification.get("goal")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    explicit_id = specification.get("id")
    if explicit_id is not None and (not isinstance(explicit_id, str) or not _ID.fullmatch(explicit_id)):
        raise ValueError("Topic ID must be a safe lowercase identifier; path separators and traversal are forbidden")
    topic_id = explicit_id if explicit_id is not None else _id(name)
    questions = _strings(specification.get("questions"), "questions", required=True)
    target_species = _strings(specification.get("target_species"), "target_species", required=True)
    model_species = _strings(specification.get("model_species"), "model_species")
    methods = _strings(specification.get("methods"), "methods", required=True)
    applications = _strings(specification.get("applications"), "applications")
    transfers = _strings(specification.get("transfer_directions"), "transfer_directions")
    exclusions = _strings(specification.get("exclusions"), "exclusions")
    categories = _strings(specification.get("journal_categories"), "journal_categories")
    search_terms = _strings(specification.get("search_terms") or questions, "search_terms", required=True)
    providers = _strings(specification.get("sources") or list(PROVIDERS), "sources", required=True)
    if any(source not in PROVIDERS for source in providers):
        raise ValueError("sources must name existing academic providers")
    weekly_limit = specification.get("weekly_alert_limit")
    if weekly_limit is not None and (type(weekly_limit) is not int or weekly_limit < 0 or weekly_limit > 50):
        raise ValueError("weekly_alert_limit must be an integer from 0 to 50")
    notification = specification.get("notification", "none")
    if notification not in ("none", "obsidian", "desktop"):
        raise ValueError("notification must be none, obsidian or desktop")
    weekly_enabled = specification.get("weekly_enabled", True)
    if type(weekly_enabled) is not bool:
        raise ValueError("weekly_enabled must be a boolean")
    question_entries = _entries(questions, "question")
    method_entries = _entries(methods, "method")
    application_entries = _entries(applications, "application")
    species_entries = _entries(target_species + model_species, "species")
    target_ids = {entry["id"] for entry in species_entries[:len(target_species)]}
    relations = [{"from": species["id"], "to": question["id"],
                  "type": "direct_reference" if species["id"] in target_ids else "method_transfer",
                  **({"scope": "cross_species"} if species["id"] not in target_ids else {})}
                 for species in species_entries for question in question_entries]
    return {
        "topic": {"schema_version": 1, "id": topic_id, "display_name": name.strip(),
                  "enabled": weekly_enabled,
                  "search_terms": search_terms, "sources": providers,
                  "extensions": {"research_goal": goal.strip(), "target_species": target_species,
                                 "model_species": model_species, "transfer_directions": transfers,
                                 "journal_categories": categories,
                                 "weekly": {"primary_days": 7, "overlap_days": 14,
                                            **({"alert_limit": weekly_limit} if weekly_limit is not None else {})}}},
        "focus": {"schema_version": 1, "topic_id": topic_id, "version": "v001",
                  "search_terms": search_terms},
        "profile": {"schema_version": 1, "topic_id": topic_id,
                    "questions": question_entries, "methods": method_entries,
                    "applications": application_entries},
        "rules": {"schema_version": 1, "topic_id": topic_id,
                  "positive": ([{"term": value, "dimension": "question", "weight": 3} for value in questions] +
                               [{"term": value, "dimension": "method", "weight": 2} for value in methods]),
                  "negative": [{"term": value, "dimension": "general", "weight": 5} for value in exclusions],
                  "priority_thresholds": {"high": 10, "medium": 5}},
        "ontology": {"schema_version": 1, "topic_id": topic_id, "concepts": species_entries,
                     "relations": relations,
                     "extensions": {"transfer_review_required": True,
                                    "allowed_directions": transfers}},
        "phase": {"schema_version": 1, "topic_id": topic_id,
                  "priority_question_ids": [entry["id"] for entry in question_entries],
                  "priority_method_ids": [entry["id"] for entry in method_entries]},
        "attention-memory": {"schema_version": 1, "topic_id": topic_id,
                             "preferred_concept_ids": []},
        "delivery": {"schema_version": 1, "topic_id": topic_id,
                     "output": {"format": "json", "relative_path": "reports/latest.json"},
                     "notification": {"enabled": notification != "none", "provider": notification}},
    }


def _write_json_new(path: Path, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def initialize(root: Path, specification: dict) -> Path:
    requested_root = Path(root).expanduser()
    if requested_root.is_symlink():
        raise ValueError("config root cannot be a symlink")
    root = ensure_private_path(requested_root)
    bundle = build_bundle(specification)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    topic_id = bundle["topic"]["id"]
    destination = root / topic_id
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Topic ID already exists")
    for candidate in root.iterdir():
        if candidate.is_dir() and (candidate / "topic.json").is_file():
            try:
                if load_bundle(candidate)["topic"]["id"] == topic_id:
                    raise FileExistsError("Topic ID already exists in another directory")
            except ValueError:
                continue
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix=".topic-init-", dir=root) as temporary:
            draft = Path(temporary)
            os.chmod(draft, 0o700)
            for part in CONFIG_PARTS:
                _write_json_new(draft / f"{part}.json", bundle[part])
            _write_json_new(draft / "feedback.json", {"schema_version": 1, "entries": []})
            _write_json_new(draft / "workflow-state.json", {"schema_version": 1,
                             "topic_id": topic_id, "state": "BOOTSTRAP_PENDING",
                             "history": ["TOPIC_INITIALIZED", "BOOTSTRAP_PENDING"]})
            validate_config_dir(draft)
            destination.mkdir(mode=0o700, exist_ok=False)
            try:
                for path in draft.iterdir():
                    _write_json_new(destination / path.name, json.loads(path.read_text()))
            except BaseException:
                for path in destination.iterdir():
                    path.unlink()
                destination.rmdir()
                raise
    finally:
        os.umask(old_umask)
    return destination


def describe(config_dir: Path) -> dict:
    bundle = load_bundle(config_dir)
    topic = bundle["topic"]
    profile = bundle["profile"]
    extensions = topic.get("extensions", {})
    return {"id": topic["id"], "name": topic["display_name"],
            "weekly_enabled": topic.get("enabled", True),
            "goal": extensions.get("research_goal"),
            "questions": [item["label"] for item in profile["questions"]],
            "methods": [item["label"] for item in profile["methods"]],
            "applications": [item["label"] for item in profile["applications"]],
            "target_species": extensions.get("target_species", []),
            "model_species": extensions.get("model_species", []),
            "transfer_directions": extensions.get("transfer_directions", []),
            "sources": topic["sources"],
            "journal_categories": extensions.get("journal_categories", []),
            "notification": bundle.get("delivery", {}).get("notification", {}).get("provider", "none")}


def list_topics(root: Path) -> list[dict]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []
    return [describe(path) for path in sorted(root.iterdir())
            if path.is_dir() and (path / "topic.json").is_file()]
