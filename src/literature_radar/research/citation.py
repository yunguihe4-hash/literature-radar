"""Bounded one-hop OpenAlex and OpenCitations discovery, outside Weekly."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.providers.models import PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.research.opencitations import OpenCitationsProvider
from literature_radar.research.review import prepare_review
from literature_radar.candidates.identity import normalize_doi
from literature_radar.private_paths import ensure_private_path

SOURCES = ("openalex", "opencitations")


def _wid(value: str) -> str:
    result = value.rsplit("/", 1)[-1].strip()
    if not result.startswith("W") or not result[1:].isdigit():
        raise ValueError("seed has no valid OpenAlex work ID")
    return result


def expand_citations(config_dir: Path, database: Path, *, paper_ids: list[str] | None = None,
                     direction: str = "both", max_per_seed: int = 100,
                     provider: OpenAlexProvider | None = None,
                     opencitations_provider: OpenCitationsProvider | None = None,
                     provider_names: list[str] | None = None,
                     evaluation_run_dir: Path | None = None) -> dict:
    if direction not in {"references", "citations", "both"} or not 1 <= max_per_seed <= 200:
        raise ValueError("invalid citation expansion bounds")
    config_dir, database = ensure_private_path(config_dir), ensure_private_path(database)
    topic = load_bundle(config_dir)["topic"]["id"]
    store = AcademicStore(database, cache_abstracts=True)
    known = {paper.paper_id: paper for paper in store.topic_papers(topic)}
    selected = list(dict.fromkeys(paper_ids if paper_ids is not None else known))
    if not selected or any(paper_id not in known for paper_id in selected):
        raise ValueError("Citation seeds must be existing Topic papers")
    names = provider_names or (["openalex"] if provider is not None else list(SOURCES))
    if not names or len(names) != len(set(names)) or any(name not in SOURCES for name in names):
        raise ValueError("select supported citation sources once each")
    adapters = {"openalex": provider, "opencitations": opencitations_provider}
    results = []
    for paper_id in selected:
        paper = known[paper_id]
        for relation in (("references", "citations") if direction == "both" else (direction,)):
            for name in names:
                operation = {"seed_paper_id": paper_id, "relation": relation,
                             "provider": name, "status": "PENDING", "records": 0}
                results.append(operation)
                doi = normalize_doi(paper.doi or paper.identifiers.get("doi"))
                work_id = paper.identifiers.get("openalex_id") or paper.openalex_id
                if name == "openalex" and not work_id:
                    operation["status"] = "SKIPPED_NO_OPENALEX_ID"
                    continue
                if name == "opencitations" and not doi:
                    operation["status"] = "SKIPPED_NO_DOI"
                    continue
                digest = hashlib.sha256(paper_id.encode()).hexdigest()[:16]
                query = QueryPlan(name, f"{topic}:{name}:citation:{digest}:{relation}",
                                  {"seed_paper_id": paper_id, "relation": relation,
                                   "max_per_seed": max_per_seed}, None)
                operation["query_signature"] = query.query_signature
                checkpoint = store.checkpoint(name, topic, query.query_signature)
                if checkpoint and checkpoint["status"] == "SUCCESS":
                    operation["status"] = "REUSED"
                    continue
                try:
                    stamp = datetime.now(timezone.utc).isoformat()
                    if name == "openalex":
                        source = adapters[name] or OpenAlexProvider()
                        wid = _wid(work_id)
                        if relation == "references":
                            seed = source.client.get_json(
                                f"https://api.openalex.org/works/{wid}", {"select": "referenced_works"})
                            ids = [_wid(value) for value in (seed.get("referenced_works") or [])][:max_per_seed]
                            works = (source.client.get_json("https://api.openalex.org/works",
                                     {"filter": "openalex_id:" + "|".join(ids),
                                      "per-page": len(ids)}) if ids else {"results": []})
                        else:
                            works = source.client.get_json("https://api.openalex.org/works",
                                     {"filter": f"cites:{wid}", "per-page": max_per_seed,
                                      "sort": "cited_by_count:desc"})
                        payloads = works.get("results") if isinstance(works, dict) else None
                        if not isinstance(payloads, list):
                            raise ValueError("OpenAlex citation response lacks results")
                        raw = tuple(RawRecord(name, value, stamp) for value in payloads)
                        normalized = [source.normalize(item) for item in raw]
                    else:
                        source = adapters[name] or OpenCitationsProvider()
                        normalized, payloads, total = source.expand(doi, relation, max_per_seed)
                        raw = tuple(RawRecord(name, value, stamp) for value in payloads)
                        operation["provider_total_edges"] = total
                    page = PageResult(raw, None, False, RequestMetadata(name, stamp))
                    page_number = checkpoint["page_number"] if checkpoint else 0
                    papers, conflicts = store.commit_page(topic, query, page, normalized,
                        expected_page_number=page_number, expected_cursor=None,
                        citation_edge=(paper_id, relation), queue_events=False)
                    operation.update(status="SUCCESS", records=len(papers), conflicts=conflicts)
                except Exception as exc:
                    try:
                        store.mark_failed(topic, query)
                    except Exception:
                        pass
                    operation.update(status="FAILED", error_category=type(exc).__name__,
                                     error=str(exc)[:300])
    failures = sum(item["status"] == "FAILED" for item in results)
    successes = sum(item["status"] in ("SUCCESS", "REUSED") for item in results)
    signatures = [item["query_signature"] for item in results
                  if item["status"] in ("SUCCESS", "REUSED")]
    candidate_ids = store.citation_related(topic, signatures)
    connections = store.citation_connections(topic, signatures)
    by_related = {}
    for edge in connections:
        entry = by_related.setdefault(edge["related_paper_id"],
                                      {"paper_id": edge["related_paper_id"],
                                       "seed_paper_ids": set(), "relations": set(),
                                       "providers": set()})
        entry["seed_paper_ids"].add(edge["seed_paper_id"])
        entry["relations"].add(edge["relation"])
        entry["providers"].add(edge["provider_id"])
    multi_seed = [{"paper_id": entry["paper_id"],
                   "seed_count": len(entry["seed_paper_ids"]),
                   "seed_paper_ids": sorted(entry["seed_paper_ids"]),
                   "relations": sorted(entry["relations"]),
                   "providers": sorted(entry["providers"])}
                  for entry in by_related.values() if len(entry["seed_paper_ids"]) > 1]
    multi_seed.sort(key=lambda entry: (-entry["seed_count"], entry["paper_id"]))
    evaluation = (prepare_review(config_dir, database, evaluation_run_dir,
                  paper_ids=candidate_ids, mode="citation-expand",
                  mode_context={"seed_papers": [
                      {"paper_id": paper_id, "title": known[paper_id].title,
                       "doi": known[paper_id].doi} for paper_id in selected],
                      "operations": results,
                      "multi_seed": multi_seed})
                  if evaluation_run_dir is not None and candidate_ids else None)
    return {"topic_id": topic, "mode": "citation-expand", "depth": 1,
            "status": "PARTIAL_SUCCESS" if failures and successes else
                      "FAILED" if failures else "SUCCESS",
            "seeds": len(selected), "operations": results,
            "candidate_paper_ids": candidate_ids, "multi_seed": multi_seed,
            "evaluation": evaluation,
            "weekly_state_changed": False, "delivery_attempted": False}
