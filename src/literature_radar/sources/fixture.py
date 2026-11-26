"""Deterministic offline provider for synthetic fixtures and integration tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping

from literature_radar.models import FieldProvenance, Paper, SourceRecord
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata


class FixturePageError(RuntimeError):
    """Intentional synthetic page failure."""


class FixtureProvider:
    sync_capability = "offline_fixture"
    def __init__(self, path: Path, *, provider_id: str = "fixture",
                 page_size: int = 100, fail_on_pages: Collection[int] = ()):
        if not provider_id:
            raise ValueError("provider_id must be nonempty")
        if page_size < 1:
            raise ValueError("page_size must be positive")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("fixture must be a JSON array")
        self.records = [Paper.from_dict(item) for item in data]
        self.provider_id = provider_id
        self.page_size = page_size
        self.fail_on_pages = frozenset(fail_on_pages)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        source = config.get("source")
        terms = config.get("terms")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("fixture config source must be a nonempty string")
        if not isinstance(terms, list) or not terms or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError("fixture config terms must be a nonempty list of strings")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        terms = list(dict.fromkeys([*topic["search_terms"], *focus["search_terms"]]))
        plans = []
        for source in topic["sources"]:
            config = {"source": source, "terms": terms, "focus_version": focus.get("version")}
            self.validate_config(config)
            plans.append(QueryPlan(self.provider_id, f"{topic['id']}:{source}",
                                   config, date_window))
        return tuple(plans)

    def fetch_page(self, plan: QueryPlan, cursor: Any = None) -> PageResult:
        if plan.provider_id != self.provider_id:
            raise ValueError("query plan belongs to a different provider")
        offset = 0 if cursor is None else cursor
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("fixture cursor must be a nonnegative integer")
        page_number = offset // self.page_size
        if page_number in self.fail_on_pages:
            raise FixturePageError(f"synthetic page {page_number} failed")
        source = plan.query_parameters["source"]
        terms = plan.query_parameters["terms"]
        matches = [paper for paper in self.records if paper.source == source
                   and any(term.casefold() in f"{paper.title} {paper.abstract or ''}".casefold()
                           for term in terms)]
        now = datetime.now(timezone.utc).isoformat()
        batch = tuple(RawRecord(self.provider_id, paper.as_dict(), now)
                      for paper in matches[offset:offset + self.page_size])
        end = offset + len(batch)
        more = end < len(matches)
        return PageResult(batch, end if more else None, more,
                          RequestMetadata(self.provider_id, now))

    def normalize(self, raw: RawRecord) -> SourceRecord:
        if raw.provider_id != self.provider_id:
            raise ValueError("raw record belongs to a different provider")
        paper = Paper.from_dict(raw.payload)
        metadata = {key: value for key, value in raw.payload.items()
                    if key not in {"source_id", "source", "identifiers", "model_version"}}
        provenance = {key: FieldProvenance(self.provider_id, paper.source_id, raw.retrieved_at, key)
                      for key in metadata}
        provenance.update({f"identifiers.{key}": FieldProvenance(
            self.provider_id, paper.source_id, raw.retrieved_at, f"identifiers.{key}")
            for key in paper.identifiers})
        return SourceRecord(provider_id=self.provider_id, source_id=paper.source_id,
                            retrieved_at=raw.retrieved_at, identifiers=dict(paper.identifiers),
                            normalized_metadata=metadata, source_updated_at=paper.updated_date,
                            provenance=provenance)

    def search(self, terms: list[str], source: str, limit: int) -> list[Paper]:
        """Preserve the original Stage 1 discovery interface and results."""
        matches = []
        for paper in self.records:
            text = f"{paper.title} {paper.abstract or ''}".casefold()
            if paper.source == source and any(term.casefold() in text for term in terms):
                matches.append(paper)
        return matches[:limit]
