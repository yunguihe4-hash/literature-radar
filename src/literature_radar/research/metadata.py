"""Identifier-only, source-isolated metadata recovery for existing Topic papers.

Lookup tasks mirror the final AI_OS Crossref/PubMed/Europe PMC/OpenAlex order.
The independent database identity layer rejects conflicting lookup results.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.providers.models import PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.crossref import CrossrefProvider
from literature_radar.sources.pubmed import PubMedProvider
from literature_radar.sources.europepmc import EuropePMCProvider
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.candidates.identity import normalize_doi
from literature_radar.private_paths import ensure_private_path

SOURCES = ("crossref", "pubmed", "openalex", "europepmc")


def _identifiers(paper) -> dict[str, str]:
    values = dict(paper.identifiers)
    for name, value in (("doi", paper.doi), ("pmid", paper.pmid),
                        ("pmcid", paper.pmcid), ("openalex_id", paper.openalex_id)):
        if value and name not in values:
            values[name] = value
    if values.get("doi"):
        values["doi"] = normalize_doi(values["doi"])
    return {key: value for key, value in values.items() if value}


def _task(source: str, ids: dict[str, str]) -> tuple[str, str] | None:
    if source == "crossref" and ids.get("doi"):
        return "doi", ids["doi"]
    if source == "pubmed" and ids.get("pmid"):
        return "pmid", ids["pmid"]
    if source == "openalex":
        if ids.get("openalex_id"):
            return "openalex_id", ids["openalex_id"]
        if ids.get("doi"):
            return "doi", ids["doi"]
    if source == "europepmc":
        for kind in ("pmid", "pmcid", "doi"):
            if ids.get(kind):
                return kind, ids[kind]
    return None


def _matching(record, known: dict[str, str]) -> bool:
    incoming = record.identifiers
    matched = False
    for kind in ("doi", "pmid", "pmcid", "openalex_id"):
        left, right = known.get(kind), incoming.get(kind)
        if kind == "doi":
            left, right = normalize_doi(left), normalize_doi(right)
        if left and right:
            if left != right:
                raise ValueError(f"metadata lookup conflicts with existing {kind}")
            matched = True
    return matched


def enrich_metadata(config_dir: Path, database: Path, *, paper_ids: list[str],
                    provider: CrossrefProvider | None = None,
                    provider_names: list[str] | None = None,
                    providers: Mapping[str, object] | None = None) -> dict:
    """Resume successful lookups independently; never launch keyword discovery."""
    config_dir, database = ensure_private_path(config_dir), ensure_private_path(database)
    topic = load_bundle(config_dir)["topic"]["id"]
    store = AcademicStore(database, cache_abstracts=True)
    known = {paper.paper_id: paper for paper in store.topic_papers(topic)}
    selected = list(dict.fromkeys(paper_ids))
    if not selected or any(item not in known for item in selected):
        raise ValueError("Metadata recovery needs existing Topic paper IDs")
    names = provider_names or (["crossref"] if provider is not None else list(SOURCES))
    if not names or len(names) != len(set(names)) or any(name not in SOURCES for name in names):
        raise ValueError("select supported metadata sources once each")
    supplied = dict(providers or {})
    if provider is not None:
        supplied["crossref"] = provider
    defaults = {"crossref": CrossrefProvider, "pubmed": PubMedProvider,
                "openalex": OpenAlexProvider, "europepmc": EuropePMCProvider}
    results = []
    for paper_id in selected:
        paper = known[paper_id]
        ids = _identifiers(paper)
        for source in names:
            task = _task(source, ids)
            result = {"paper_id": paper_id, "provider": source,
                      "status": "PENDING", "metadata_only": True}
            results.append(result)
            if task is None:
                result["status"] = "SKIPPED_NO_IDENTIFIER"
                continue
            kind, value = task
            result["identifier_type"] = kind
            digest = hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:16]
            query = QueryPlan(source, f"{topic}:{source}:metadata:{digest}",
                              {kind: value}, None)
            checkpoint = store.checkpoint(source, topic, query.query_signature)
            if checkpoint and checkpoint["status"] == "SUCCESS":
                result["status"] = "REUSED"
                continue
            try:
                adapter = supplied.get(source) or defaults[source]()
                record = (adapter.fetch_by_identifier(value, kind=kind) if source == "europepmc"
                          else adapter.fetch_by_identifier(value))
                if record is not None and not _matching(record, ids):
                    raise ValueError("metadata lookup has no matching strong identifier")
                stamp = datetime.now(timezone.utc).isoformat()
                raw = (() if record is None else
                       (RawRecord(source, {kind: value}, stamp),))
                page = PageResult(raw, None, False, RequestMetadata(source, stamp))
                store.commit_page(topic, query, page, [] if record is None else [record],
                                  expected_page_number=checkpoint["page_number"] if checkpoint else 0,
                                  expected_cursor=None, queue_events=False)
                result["status"] = "NO_MATCH" if record is None else "SUCCESS"
            except Exception as exc:
                try:
                    store.mark_failed(topic, query)
                except Exception:
                    pass
                result.update(status="FAILED", error_category=type(exc).__name__,
                              error=str(exc)[:300])
    failures = sum(item["status"] == "FAILED" for item in results)
    successes = sum(item["status"] in ("SUCCESS", "REUSED", "NO_MATCH") for item in results)
    return {"mode": "enrich-metadata", "topic_id": topic,
            "status": "PARTIAL_SUCCESS" if failures and successes else
                      "FAILED" if failures else "SUCCESS",
            "results": results, "weekly_state_changed": False,
            "delivery_attempted": False}
