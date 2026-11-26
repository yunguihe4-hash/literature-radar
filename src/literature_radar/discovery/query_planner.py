"""Supplement existing checkpointed queries with AI_OS semantic query groups.

The original broad query is always first and unchanged. Extra groups have
distinct IDs and signatures, so old successful pages and cursors stay valid.
BioRxiv's official API has no keyword query; it keeps one bounded date scan.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from literature_radar.providers.models import QueryPlan
from literature_radar.private_paths import ensure_private_path
from literature_radar.topics.focus import _write_once


def _unique(values, limit=8):
    seen, result = set(), []
    for value in values:
        clean = " ".join(str(value).split()).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def load_search_semantics(config_dir: Path, bundle: dict) -> dict | None:
    path = Path(config_dir) / "search-semantics" / f"{bundle['focus']['version']}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or
            data.get("topic_id") != bundle["topic"]["id"] or
            data.get("focus_version") != bundle["focus"]["version"]):
        raise ValueError("Search semantics sidecar identity differs from Topic/Focus")
    return data


def import_ai_os_search(config_dir: Path, ai_os_root: Path, *, dry_run: bool = False) -> dict:
    """One-time import of original search vocabulary into private Focus snapshots."""
    config_dir = ensure_private_path(config_dir)
    from literature_radar.config import load_bundle
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    root = Path(ai_os_root).expanduser().resolve()
    original = root / "Projects" / "literature-radar" / "topics" / topic_id
    topic_path = original / "topic.toml"
    topic_bytes = topic_path.read_bytes()
    topic = tomllib.loads(topic_bytes.decode("utf-8"))
    if topic.get("topic_id") != topic_id:
        raise ValueError("Original AI_OS Topic identity differs")
    fields = ("concepts", "entities", "synonyms", "organisms", "methods",
              "applications", "exclusions")
    if any(not isinstance(topic.get(key, []), list) for key in fields):
        raise ValueError("Original search vocabulary fields must be lists")
    vocabulary = {key: list(topic.get(key, [])) for key in fields}
    vocabulary["description"] = topic.get("description", "")
    if (not isinstance(vocabulary["description"], str) or
            any(not isinstance(value, str) for key in fields for value in vocabulary[key])):
        raise ValueError("Original search vocabulary contains invalid terms")
    snapshots = []
    for focus_path in sorted((original / "focuses").glob("v*.toml")):
        focus_bytes = focus_path.read_bytes()
        focus = tomllib.loads(focus_bytes.decode("utf-8"))
        if focus.get("topic_id") != topic_id or focus.get("version") != focus_path.stem:
            raise ValueError("Original Focus snapshot identity differs")
        signals = focus.get("positive_signals", [])
        questions = focus.get("priority_questions", [])
        if (not isinstance(signals, list) or not isinstance(questions, list) or
                any(not isinstance(value, str) for value in [*signals, *questions])):
            raise ValueError("Original Focus search vocabulary contains invalid terms")
        document = {"schema_version": 1, "topic_id": topic_id,
                    "focus_version": focus["version"], "topic": vocabulary,
                    "focus": {"positive_signals": list(signals),
                              "priority_questions": list(questions),
                              "title": focus.get("title", "")},
                    "source_sha256": {"topic": hashlib.sha256(topic_bytes).hexdigest(),
                                      "focus": hashlib.sha256(focus_bytes).hexdigest()}}
        snapshots.append((focus["version"], document))
    versions = [version for version, _ in snapshots]
    if bundle["focus"]["version"] not in versions:
        raise ValueError("Current Focus has no original search snapshot")
    # Validate the entire batch, including immutable existing files, before writing.
    for version, document in snapshots:
        target = config_dir / "search-semantics" / f"{version}.json"
        if target.exists() and json.loads(target.read_text(encoding="utf-8")) != document:
            raise ValueError("Immutable search semantics version already differs")
    if not dry_run:
        for version, document in snapshots:
            _write_once(config_dir / "search-semantics" / f"{version}.json", document)
    return {"topic_id": topic_id, "versions": versions,
            "status": "DRY_RUN" if dry_run else "IMPORTED",
            "private_output": str(config_dir / "search-semantics")}


def _original_groups(document: dict) -> list[tuple[str, list[str]]]:
    """Unchanged final AI_OS semantic category ordering and limits."""
    topic, focus = document["topic"], document["focus"]
    core = _unique(list(topic.get("concepts", []))[:3] +
                   list(topic.get("entities", []))[:3] +
                   list(topic.get("synonyms", []))[:2])
    organisms = _unique(list(topic.get("organisms", [])), 4)
    methods = _unique(list(topic.get("methods", [])), 4)
    applications = _unique(list(topic.get("applications", [])), 4)
    explicit = list(focus.get("positive_signals", []))
    focus_terms = (_unique(explicit, 6) if explicit else _unique(
        re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", focus.get("title", "")), 6))
    anchor = core[:3] or focus_terms[:3] or applications[:3]
    groups = []
    if core:
        groups.append(("core", core[:6]))
    if organisms and anchor:
        groups.append(("organism", organisms[:3] + anchor[:3]))
    if focus_terms and anchor:
        groups.append(("focus", focus_terms[:4] + anchor[:2]))
    if methods and anchor:
        groups.append(("method", methods[:3] + anchor[:3]))
    if not groups:
        description_terms = re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", topic["description"])
        groups.append(("core", _unique(description_terms, 6)))
    return groups


def semantic_query_groups(bundle: dict, semantics: dict | None = None) -> list[tuple[str, list[str]]]:
    """Map current Topic/Focus/Profile fields to the original four group roles."""
    if semantics is not None:
        return _original_groups(semantics)
    topic, focus, profile = bundle["topic"], bundle["focus"], bundle["profile"]
    ext = topic.get("extensions", {})
    roles = ext.get("source_roles", {})
    # A Topic may supply the exact original semantic lists in its own config.
    semantics = ext.get("ai_os_semantics", {})
    core = _unique(semantics.get("core", topic["search_terms"][:3]), 6)
    organisms = _unique(semantics.get("organisms", [
        *roles.get("direct", []), *roles.get("model", [])]), 4)
    methods = _unique(semantics.get("methods", [
        item["label"] for item in profile.get("methods", [])]), 4)
    focus_terms = _unique(semantics.get("focus", focus.get("search_terms", [])), 6)
    anchor = core[:3] or focus_terms[:3]
    groups = []
    if core:
        groups.append(("core", core))
    if organisms and anchor:
        groups.append(("organism", organisms[:3] + anchor[:3]))
    if focus_terms and anchor:
        groups.append(("focus", focus_terms[:4] + anchor[:2]))
    if methods and anchor:
        groups.append(("method", methods[:3] + anchor[:3]))
    return groups


def _quoted(term: str) -> str:
    return '"' + term.replace('"', " ") + '"'


def planned_queries(provider, bundle: dict, window, *, config_dir: Path | None = None) -> tuple[QueryPlan, ...]:
    """Return the unmodified primary plan plus source-specific semantic plans."""
    topic, focus = bundle["topic"], bundle["focus"]
    primary = tuple(provider.plan_queries(topic, focus, window))
    if provider.provider_id == "biorxiv" and config_dir is not None:
        semantics = load_search_semantics(config_dir, bundle)
        if semantics is not None:
            groups = _original_groups(semantics)
            # The official bioRxiv API only filters by date. One local OR
            # filter over all original group terms has the same recall as the
            # original four repeated date scans and retains distinct cursors.
            original_terms = _unique([term for _, values in groups for term in values], 32)
            extra = tuple(QueryPlan("biorxiv", f"{plan.query_id}:aios",
                                    {**plan.query_parameters, "terms": original_terms,
                                     "semantic_groups": [name for name, _ in groups]},
                                    window) for plan in primary)
            return primary + extra
    if not getattr(provider, "supports_semantic_groups", False):
        return primary
    name = provider.provider_id
    if name not in {"openalex", "pubmed", "europepmc"}:
        return primary
    if not primary:
        return primary
    template = primary[0]
    semantics = load_search_semantics(config_dir, bundle) if config_dir is not None else None
    key = {"openalex": "search", "pubmed": "term", "europepmc": "query"}[name]
    extra = []
    # Imported AI_OS semantics must not invalidate already committed fallback
    # group checkpoints. Keep those original query IDs and signatures, then
    # add the AI_OS groups under distinct IDs for complete historical recall.
    groups = ([(group, terms, "") for group, terms in semantic_query_groups(bundle, None)] +
              [(group, terms, "aios:") for group, terms in semantic_query_groups(bundle, semantics)]
              if semantics is not None else
              [(group, terms, "") for group, terms in semantic_query_groups(bundle)])
    for group, terms, id_prefix in groups:
        if name == "openalex":
            expression = " ".join(terms)
        else:
            prefix = "" if name == "pubmed" else "TITLE_ABS:"
            suffix = "[Title/Abstract]" if name == "pubmed" else ""
            clauses = [f"{prefix}{_quoted(term)}{suffix}" for term in terms]
            desired = 3 if group in {"organism", "method"} else (4 if group == "focus" else 0)
            split = min(desired, len(clauses) - 1) if len(clauses) > 1 else 0
            expression = (("(" + " OR ".join(clauses[:split]) + ") AND (" +
                           " OR ".join(clauses[split:]) + ")") if split else
                          "(" + " OR ".join(clauses) + ")")
            exclusions = _unique((semantics["topic"].get("exclusions", []) if id_prefix else
                                  topic.get("extensions", {}).get("ai_os_semantics", {}).get(
                                      "exclusions", [])), 4)
            if exclusions:
                expression += " NOT (" + " OR ".join(
                    f"{prefix}{_quoted(term)}{suffix}" for term in exclusions) + ")"
        parameters = {**template.query_parameters, key: expression,
                      "semantic_group": group}
        extra.append(QueryPlan(name, f"{topic['id']}:{name}:{id_prefix}{group}",
                               parameters, window))
    return primary + tuple(extra)
