"""Explicit research discovery modes outside normal Weekly scheduling.

These workflows reuse the installed Provider adapters, page checkpoints and
identity merge. They do not advance Weekly state, deliver cards or notify.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Mapping

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.providers.models import QueryPlan
from literature_radar.sources.factory import make_provider
from literature_radar.private_paths import ensure_private_path
from literature_radar.research.review import prepare_review
from literature_radar.discovery.query_planner import load_search_semantics

SEARCH_SOURCES = ("openalex", "pubmed", "europepmc", "biorxiv")


def _unique(values, limit=None):
    result, seen = [], set()
    for value in values:
        clean = " ".join(str(value).split()).strip()
        key = re.sub(r"\W+", " ", clean.casefold()).strip()
        if clean and key and key not in seen:
            seen.add(key)
            result.append(clean)
            if limit is not None and len(result) >= limit:
                break
    return result


def query_variants(bundle: dict, per_group: int = 4,
                   semantics: dict | None = None) -> list[tuple[str, str, list[str]]]:
    """Original AI_OS bounded variant design mapped to current Topic fields."""
    if not 1 <= per_group <= 20:
        raise ValueError("per_group must be 1 to 20")
    topic, focus, profile = bundle["topic"], bundle["focus"], bundle["profile"]
    ext = topic.get("extensions", {})
    semantic = semantics["topic"] if semantics is not None else ext.get("ai_os_semantics", {})
    original_focus = semantics["focus"] if semantics is not None else {}
    entities = _unique(semantic.get("entities", topic["search_terms"][:3]), 4)
    concepts = _unique(semantic.get("concepts", topic["search_terms"]), 6)
    synonyms = _unique(semantic.get("synonyms", topic["search_terms"]), 5)
    organisms = _unique(semantic.get("organisms", [
        *ext.get("source_roles", {}).get("direct", []),
        *ext.get("source_roles", {}).get("model", [])]), 8)
    methods = _unique(semantic.get("methods", [item["label"] for item in
                                                profile.get("methods", [])]), 8)
    positives = _unique(original_focus.get("positive_signals", semantic.get(
        "positive_signals", focus.get("search_terms", []))), 8)
    priority = _unique(original_focus.get("priority_questions", semantic.get(
        "priority_questions", [item["label"] for item in
                                                        profile.get("questions", [])])), 4)
    anchor_entities = entities or concepts[:2]
    anchor_concepts = concepts or entities[:2]
    raw = {
        "core": [[concept, entity] for concept in concepts[:4] for entity in anchor_entities[:2]]
                + [[entity, synonym] for entity in entities[:3] for synonym in synonyms[:2]],
        "organism": [[organism, entity] for organism in organisms for entity in anchor_entities[:1]]
                    + [[organism, concept] for organism in organisms for concept in anchor_concepts[:1]],
        "focus": [[signal] for signal in positives[:4]]
                 + [[signal, entity] for signal in positives[4:] for entity in anchor_entities[:1]]
                 + [[question, entity] for question in priority[:2] for entity in anchor_entities[:1]],
        "method": [[method, entity] for method in methods for entity in anchor_entities[:1]]
                  + [[method, concept] for method in methods for concept in anchor_concepts[:1]],
    }
    variants, global_seen = [], set()
    for group in ("core", "organism", "focus", "method"):
        number = 0
        for terms in raw[group]:
            terms = _unique(terms)
            signature = tuple(sorted(re.sub(r"\W+", " ", term.casefold()).strip() for term in terms))
            if not terms or signature in global_seen:
                continue
            global_seen.add(signature)
            number += 1
            variants.append((group, f"{group}:{number:03d}", terms))
            if number >= per_group:
                break
    return variants


class _PlannedProvider:
    supports_semantic_groups = False

    def __init__(self, underlying, variants: list[tuple[str, str, list[str]]], mode: str):
        self.underlying, self.variants, self.mode = underlying, variants, mode
        self.provider_id = underlying.provider_id
        self.sync_capability = underlying.sync_capability

    def __getattr__(self, name):
        return getattr(self.underlying, name)

    def plan_queries(self, topic, focus, date_window=None):
        base_plans = self.underlying.plan_queries(topic, focus, date_window)
        output = []
        for base in base_plans:
            for _, name, terms in self.variants:
                quoted = ['"' + term.replace('"', ' ') + '"' for term in terms]
                parameters = dict(base.query_parameters)
                if self.provider_id == "openalex":
                    parameters["search"] = " ".join(quoted)
                elif self.provider_id == "pubmed":
                    parameters["term"] = " AND ".join(f"{term}[Title/Abstract]" for term in quoted)
                elif self.provider_id == "europepmc":
                    parameters["query"] = " AND ".join(f"TITLE_ABS:{term}" for term in quoted)
                elif self.provider_id == "biorxiv":
                    parameters["terms"] = terms
                suffix = (":" + str(parameters["kind"]) if "kind" in parameters else "")
                output.append(QueryPlan(self.provider_id,
                                        f"{topic['id']}:{self.provider_id}:{self.mode}:{name}{suffix}",
                                        parameters, date_window))
        return tuple(output)


def run_specialty(config_dir: Path, database: Path, *, mode: str,
                  since: str, until: str, term: str | None = None,
                  provider_names: list[str] | None = None,
                  providers: Mapping[str, object] | None = None,
                  variants_per_group: int = 4, max_pages: int = 8,
                  max_seconds: float = 120, dry_run: bool = False,
                  evaluation_run_dir: Path | None = None) -> dict:
    if mode not in {"targeted", "deep-baseline"}:
        raise ValueError("unsupported specialty discovery mode")
    if date.fromisoformat(since) > date.fromisoformat(until):
        raise ValueError("invalid specialty date window")
    config_dir, database = ensure_private_path(config_dir), ensure_private_path(database)
    bundle = load_bundle(config_dir)
    selected = list(dict.fromkeys(provider_names or SEARCH_SOURCES))
    if not selected or any(name not in SEARCH_SOURCES for name in selected):
        raise ValueError("select supported specialty search sources")
    if mode == "targeted":
        allowed = [*bundle["topic"]["search_terms"], *bundle["focus"]["search_terms"],
                   *bundle["topic"].get("extensions", {}).get("targeted_terms", [])]
        if not term or term.casefold() not in {value.casefold() for value in allowed}:
            raise ValueError("targeted term must be configured in this Topic or Focus")
        digest = hashlib.sha256(term.casefold().encode()).hexdigest()[:12]
        variants = [("targeted", digest, [term])]
    else:
        variants = query_variants(bundle, variants_per_group,
                                  load_search_semantics(config_dir, bundle))
    wrapped = {}
    for name in selected:
        underlying = providers[name] if providers is not None else make_provider(
            name, options=bundle["topic"].get("extensions", {}).get("provider_options", {}).get(name, {}))
        wrapped[name] = _PlannedProvider(underlying, variants, mode)
    result = discover_incremental(config_dir, selected, database_path=database,
                                  since=since, until=until, providers=wrapped,
                                  max_pages=max_pages, max_seconds=max_seconds,
                                  channels=[], notifiers={}, defer_notifications=True,
                                  dry_run=dry_run, queue_events=False)
    candidate_ids = list(dict.fromkeys(item["paper_id"] for item in
                                       result.get("recommendations", []) if item.get("paper_id")))
    # Keep the executed search contract with the review.  Provider counts and
    # discovery status describe retrieval, not newly published papers.
    mode_context = {
        "window": {"start": since, "end": until},
        "sources": selected,
        "query_variants": [{"group": group, "name": name, "terms": terms}
                           for group, name, terms in variants],
        "discovery_status": result["status"],
        "candidate_count": len(candidate_ids),
    }
    evaluation = (prepare_review(config_dir, database, evaluation_run_dir,
                  paper_ids=candidate_ids, mode=mode, mode_context=mode_context)
                  if evaluation_run_dir is not None and candidate_ids and not dry_run else None)
    return {"schema_version": 1, "mode": mode, "topic_id": bundle["topic"]["id"],
            "window": {"start": since, "end": until},
            "variants": [{"group": group, "name": name, "terms": terms}
                         for group, name, terms in variants],
            "discovery": result, "status": result["status"],
            "candidate_paper_ids": candidate_ids, "evaluation": evaluation,
            "weekly_state_changed": False, "delivery_attempted": False}


def recall_audit(config_dir: Path, database: Path, seeds: list[dict]) -> dict:
    config_dir, database = ensure_private_path(config_dir), ensure_private_path(database)
    topic = load_bundle(config_dir)["topic"]["id"]
    papers = AcademicStore(database).topic_papers(topic) if database.is_file() else []
    recovered, missing = [], []
    for seed in seeds:
        if not any(seed.get(field) for field in ("doi", "pmid", "title")):
            raise ValueError("each recall seed needs DOI, PMID or title")
        found = any(
            (seed.get("doi") and str(p.doi or p.identifiers.get("doi") or "").casefold() == seed["doi"].casefold())
            or (seed.get("pmid") and p.identifiers.get("pmid") == seed["pmid"])
            or (seed.get("title") and re.sub(r"\W+", " ", p.title.casefold()).strip()
                == re.sub(r"\W+", " ", seed["title"].casefold()).strip())
            for p in papers)
        (recovered if found else missing).append(seed)
    return {"topic_id": topic, "seed_count": len(seeds),
            "naturally_recovered": len(recovered),
            "recall_rate": len(recovered) / len(seeds) if seeds else None,
            "recovered_seeds": recovered, "missing_seeds": missing}
