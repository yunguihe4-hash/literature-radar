"""Map independent Topic bundles into the original AI_OS Attention contract."""
from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

from literature_radar.attention.legacy_rank import (
    assess_attention, attention_sort_key, classified_attention,
    clean_text, collect_evidence, normalized_text,
)
from literature_radar.attention.legacy_config import load_json_yaml
from literature_radar.config import load_bundle


def _named(values: list[str]) -> list[dict]:
    return [{"name": value, "aliases": [value]} for value in values]


def _memory_with_current_preferences(bundle: dict, profile: dict, methods: dict,
                                     original: dict) -> dict:
    """Feed current, human-curated Topic Memory through the old matching rule."""
    selected = set(bundle.get("attention-memory", {}).get("preferred_concept_ids", []))
    if not selected:
        return original
    current = bundle["profile"]
    def aliases(entry):
        return {str(value).strip().casefold() for value in
                [entry.get("label", entry.get("name", "")), *entry.get("aliases", [])]}
    questions = []
    for entry in current["questions"]:
        if entry["id"] in selected:
            questions.extend(old["name"] for old in profile.get("important_questions", [])
                             if aliases(entry) & aliases(old))
    method_names = []
    for entry in current["methods"]:
        if entry["id"] in selected:
            method_names.extend(old["name"] for old in methods.get("methods", [])
                                if aliases(entry) & aliases(old))
    transfers = [relation["type"] for relation in bundle["ontology"]["relations"]
                 if relation["from"] in selected and relation["type"] in
                 {"direct_reference", "method_transfer", "application_transfer", "concept_transfer"}]
    if not (questions or method_names or transfers):
        return original
    return {**original, "entries": [*original.get("entries", []), {
        "topic": bundle["topic"]["id"],
        "important_questions": sorted(set(questions)),
        "important_methods": sorted(set(method_names)),
        "preferred_transfer": sorted(set(transfers)),
    }]}


def import_ai_os_profile(ai_os_root: Path, config_dir: Path, *, dry_run: bool = False) -> dict:
    """Import a Topic's exact old rules into a private, non-overwriting sidecar."""
    ai_os_root, config_dir = Path(ai_os_root), Path(config_dir)
    topic = load_bundle(config_dir)["topic"]["id"]
    source = ai_os_root / "skills" / "literature-intelligence"
    profile_doc = load_json_yaml(source / "profile/research-profile.yaml")
    phase_doc = load_json_yaml(source / "profile/research-phase.yaml")
    sidecar = {"schema_version": 1, "topic_id": topic,
               "profile": profile_doc["topics"][topic],
               "cross_species": load_json_yaml(source / "ontology/cross-species-transfer.yaml"),
               "methods_ontology": load_json_yaml(source / "ontology/methods.yaml"),
               "memory": load_json_yaml(source / "memory/attention-memory.yaml"),
               "phase": phase_doc["topics"][topic]}
    destination = config_dir / "legacy-intelligence.json"
    if not dry_run:
        data = (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {"topic_id": topic, "destination": str(destination),
            "status": "DRY_RUN" if dry_run else "IMPORTED",
            "question_count": len(sidecar["profile"]["important_questions"]),
            "method_count": len(sidecar["methods_ontology"]["methods"])}


def import_ai_os_evidence(ai_os_root: Path, config_dir: Path, *, dry_run: bool = False) -> dict:
    """Import the original keyword rules into a separate private sidecar."""
    ai_os_root, config_dir = Path(ai_os_root), Path(config_dir)
    topic = load_bundle(config_dir)["topic"]["id"]
    source = ai_os_root / "skills" / "literature-intelligence"
    rules = load_json_yaml(source / "rules" / f"{topic}.yaml")
    if rules.get("schema_version") != 2 or rules.get("topic") != topic:
        raise ValueError("original Intelligence rule Topic mismatch")
    ontologies = []
    for name in rules.get("ontologies", []):
        if Path(name).name != name:
            raise ValueError("invalid original Intelligence ontology name")
        ontology = load_json_yaml(source / "ontology" / name)
        if ontology.get("schema_version") != 1:
            raise ValueError("invalid original Intelligence ontology")
        ontologies.append(ontology)
    sidecar = {"schema_version": 1, "topic_id": topic, "rules": rules,
               "ontologies": ontologies}
    destination = config_dir / "legacy-evidence.json"
    if not dry_run:
        data = (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {"topic_id": topic, "destination": str(destination),
            "status": "DRY_RUN" if dry_run else "IMPORTED",
            "ontology_count": len(ontologies)}


def original_keyword_evidence(paper, config_dir: Path | None,
                              fallback_score: float) -> tuple[int, list[dict], str]:
    """Return the seventh feature from the old rules when explicitly imported."""
    if config_dir is None:
        return int(fallback_score), [], "fallback"
    path = Path(config_dir) / "legacy-evidence.json"
    if not path.is_file():
        if (Path(config_dir) / "legacy-intelligence.json").is_file():
            raise ValueError("original Intelligence profile requires its evidence rules")
        return int(fallback_score), [], "fallback"
    document = json.loads(path.read_text(encoding="utf-8"))
    if (document.get("schema_version") != 1 or
            document.get("topic_id") != load_bundle(Path(config_dir))["topic"]["id"]):
        raise ValueError("original Intelligence evidence belongs to another Topic")
    title = clean_text(paper.title)
    abstract = clean_text(paper.abstract or "")
    text = normalized_text(f"{title} {abstract}")
    scores, _, evidence = collect_evidence(
        text, document["rules"], document["ontologies"])
    return sum(scores.values()), evidence, "original_ai_os"


def _legacy_documents(bundle: dict, config_dir: Path | None = None) -> tuple[dict, dict, dict, dict, dict]:
    topic = bundle["topic"]
    topic_id = topic["id"]
    explicit = Path(config_dir) / "legacy-intelligence.json" if config_dir else None
    if explicit is not None and explicit.is_file():
        document = json.loads(explicit.read_text(encoding="utf-8"))
        if document.get("topic_id") != topic_id or document.get("schema_version") != 1:
            raise ValueError("legacy Intelligence configuration belongs to another Topic")
        memory = _memory_with_current_preferences(bundle, document["profile"],
                                                   document["methods_ontology"], document["memory"])
        return (document["profile"], document["cross_species"],
                document["methods_ontology"], memory, document["phase"])
    profile = bundle["profile"]
    phase = bundle["phase"]
    ext = topic.get("extensions", {})
    methods = json.loads(files("literature_radar.attention").joinpath("legacy_methods.json").read_text())
    known = {item["name"].casefold(): item for item in methods["methods"]}
    mapped_methods = []
    question_by_id = {item["id"]: item["label"] for item in profile["questions"]}
    for method in profile["methods"]:
        legacy = known.get(method["label"].casefold())
        targets = [question_by_id[relation["to"]] for relation in bundle["ontology"]["relations"]
                   if relation["from"] == method["id"] and relation["to"] in question_by_id]
        mapped_methods.append({
            "name": method["label"],
            "aliases": list(dict.fromkeys([*method.get("aliases", []),
                                             *(legacy.get("aliases", []) if legacy else [])])),
            "transferable_to": list(dict.fromkeys([*targets, *(legacy.get("transferable_to", []) if legacy else [])])),
        })
    original_profile = {
        "important_questions": [{"name": item["label"], "aliases": item.get("aliases", [item["label"]])}
                                for item in profile["questions"]],
        "important_applications": [item["label"] for item in profile["applications"]],
        "accepted_cross_species_sources": [*ext.get("target_species", []),
                                          *ext.get("model_species", []),
                                          *ext.get("source_roles", {}).get("technology", [])],
        "excluded_directions": [item["term"] for item in bundle["rules"].get("negative", [])],
    }
    cross_species = {
        "target_species": _named(ext.get("target_species", [])),
        "model_species": _named(ext.get("model_species", [])),
        "technology_source": _named(ext.get("source_roles", {}).get("technology", [])),
    }
    priority_ids = set(phase["priority_method_ids"])
    active = [item["label"] for item in profile["methods"] if item["id"] in priority_ids]
    active.extend(ext.get("legacy_phase_signals", []))
    original_phase = {"current_phase": ext.get("research_phase"),
                      "active_signals": list(dict.fromkeys(active))}
    preferred = set(bundle.get("attention-memory", {}).get("preferred_concept_ids", []))
    original_memory = {"entries": [{
        "topic": topic_id,
        "important_questions": [item["label"] for item in profile["questions"]
                                if item["id"] in preferred],
        "important_methods": [item["label"] for item in profile["methods"]
                              if item["id"] in preferred],
        "preferred_transfer": ext.get("preferred_transfer", []),
    }]}
    return original_profile, cross_species, {"methods": mapped_methods}, original_memory, original_phase


def assess_original(paper, bundle: dict, evidence_score: float, *,
                    config_dir: Path | None = None) -> dict:
    """Call unchanged AI_OS rule functions; keep every original rank field."""
    profile, species, methods, memory, phase = _legacy_documents(bundle, config_dir)
    old_paper = {
        "paper_id": paper.paper_id, "title": paper.title, "doi": paper.doi,
        "_abstract": paper.abstract or "", "category": "Background",
        "evidence_score": int(evidence_score),
    }
    result = assess_attention(old_paper, bundle["topic"]["id"], profile, species,
                              methods, memory, phase)
    result["intrinsic_attention"] = classified_attention(result)
    result["attention_level"] = result["intrinsic_attention"]
    result["classified_attention"] = result["intrinsic_attention"]
    result["final_attention"] = result["intrinsic_attention"]
    result["sort_key"] = attention_sort_key(result)
    return result
