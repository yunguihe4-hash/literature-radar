"""OpenAlex Works adapter. Free mode uses publication-window reconciliation."""
from __future__ import annotations

import os
from typing import Any, Mapping

from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.common import date_only, plan, source_record, terms, utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure
from literature_radar.candidates.identity import normalize_doi


class OpenAlexProvider:
    supports_semantic_groups = True
    provider_id = "openalex"
    endpoint = "https://api.openalex.org/works"
    sync_capability = "publication_window_reconciliation; complete update sync requires paid filters"

    def __init__(self, client: HTTPClient | None = None, *, page_size: int = 25):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=1)
        self.page_size = min(max(page_size, 1), 100)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config.get("sync_mode", "publication") not in ("publication", "paid_updated"):
            raise ValueError("openalex sync_mode must be publication or paid_updated")
        if config.get("sync_mode") == "paid_updated" and not os.getenv("OPENALEX_API_KEY"):
            raise ValueError("openalex paid_updated requires OPENALEX_API_KEY and paid access")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        options = topic.get("extensions", {}).get("provider_options", {}).get(self.provider_id, {})
        self.validate_config(options)
        return (plan(self.provider_id, topic, focus,
                     {"search": " OR ".join(f'"{term}"' for term in terms(topic, focus)),
                      "page_size": self.page_size,
                      "sync_mode": options.get("sync_mode", "publication"),
                      "paging": "cursor" if os.getenv("OPENALEX_API_KEY") else "page"},
                     date_window),)

    def fetch_page(self, query: QueryPlan, cursor: Any = None) -> PageResult:
        key = os.getenv("OPENALEX_API_KEY")
        paging = query.query_parameters["paging"]
        if paging == "cursor" and cursor is not None and not key:
            raise RequestFailure(self.provider_id, "authentication_required_for_cursor", 401)
        window = query.date_window
        mode = query.query_parameters["sync_mode"]
        filters = []
        if window:
            field = "updated_date" if mode == "paid_updated" else "publication_date"
            if window.start: filters.append(f"from_{field}:{window.start}")
            if window.end: filters.append(f"to_{field}:{window.end}")
        page_number = 1 if cursor is None else cursor
        if paging == "page" and (not isinstance(page_number, int) or page_number < 1 or page_number * self.page_size > 10000):
            raise RequestFailure(self.provider_id, "basic_paging_limit")
        search_field = query.query_parameters.get("search_field", "search")
        if search_field not in ("search", "search.title_and_abstract"):
            raise ValueError("unsupported OpenAlex search field")
        params = {search_field: query.query_parameters["search"], "per_page": self.page_size,
                  "cursor": (cursor if cursor is not None else "*") if paging == "cursor" else None,
                  "page": page_number if paging == "page" else None,
                  "filter": ",".join(filters) if filters else None}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = self.client.get_json(self.endpoint, params, headers)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RequestFailure(self.provider_id, "malformed_response")
        now = utcnow()
        rows = tuple(RawRecord(self.provider_id, item, now) for item in data["results"])
        meta = data.get("meta") or {}
        if paging == "cursor":
            next_cursor = meta.get("next_cursor")
            if rows and next_cursor == (cursor if cursor is not None else "*"):
                raise RequestFailure(self.provider_id, "stalled_cursor")
            more = bool(next_cursor and rows)
        else:
            count = meta.get("count")
            if isinstance(count, int) and not rows and (page_number - 1) * self.page_size < count:
                raise RequestFailure(self.provider_id, "incomplete_page")
            if isinstance(count, int) and page_number * self.page_size >= 10000 and count > 10000:
                raise RequestFailure(self.provider_id, "basic_paging_limit")
            more = bool(rows) and (page_number * self.page_size < 10000) and (
                page_number * self.page_size < count if isinstance(count, int) else len(rows) == self.page_size)
            next_cursor = page_number + 1 if more else None
        return PageResult(rows, next_cursor if more else None, more, RequestMetadata(self.provider_id, now))

    def normalize(self, raw: RawRecord):
        value = raw.payload
        abstract = None
        inverted = value.get("abstract_inverted_index")
        if isinstance(inverted, dict) and inverted:
            positions = [(position, word) for word, indexes in inverted.items()
                         for position in indexes if isinstance(position, int) and position >= 0]
            if positions:
                abstract = " ".join(word for _, word in sorted(positions))
        ids = value.get("ids") or {}
        authors = [item.get("author", {}).get("display_name") or item.get("raw_author_name")
                   for item in value.get("authorships") or []]
        location = value.get("primary_location") or {}
        source = location.get("source") or {}
        fields = {"title": value.get("title") or value.get("display_name"),
                  "abstract": abstract, "authors": [x for x in authors if x],
                  "journal": source.get("display_name"), "issn": source.get("issn"),
                  "issn_l": source.get("issn_l"),
                  "publication_date": date_only(value.get("publication_date")),
                  "online_date": None, "indexed_date": None,
                  "created_date": date_only(value.get("created_date")),
                  "updated_date": date_only(value.get("updated_date")),
                  "document_type": value.get("type"), "language": value.get("language"),
                  "landing_url": location.get("landing_page_url"),
                  "license": location.get("license"),
                  "citation_count": value.get("cited_by_count")}
        identifiers = {"doi": value.get("doi") or ids.get("doi"), "pmid": ids.get("pmid"),
                       "pmcid": ids.get("pmcid"), "openalex_id": value.get("id")}
        return source_record(raw, source_id=value.get("id") or "", fields=fields,
                             identifiers=identifiers,
                             source_fields={"title": "title", "abstract": "abstract_inverted_index",
                                            "journal": "primary_location.source.display_name",
                                            "doi": "doi", "pmid": "ids.pmid", "pmcid": "ids.pmcid",
                                            "openalex_id": "id"})

    def fetch_by_identifier(self, identifier: str):
        key = os.getenv("OPENALEX_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        doi = normalize_doi(identifier)
        if doi:
            response = self.client.get_json(self.endpoint,
                {"filter": f"doi:{doi}", "per-page": 2}, headers=headers)
            rows = response.get("results") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise RequestFailure(self.provider_id, "malformed_response")
            if not rows:
                return None
            data = rows[0]
        else:
            work_id = identifier.rsplit("/", 1)[-1]
            data = self.client.get_json(self.endpoint + "/" + work_id,
                                        headers=headers)
        return self.normalize(RawRecord(self.provider_id, data, utcnow()))
