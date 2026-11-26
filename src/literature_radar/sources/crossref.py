"""Crossref Works adapter with bounded created/update/index windows."""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Mapping
from urllib.parse import quote

from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.common import date_parts, plan, plain_text, source_record, terms, utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure


class CrossrefProvider:
    provider_id = "crossref"
    endpoint = "https://api.crossref.org/works"
    sync_capability = "created/update/index deposit windows"

    def __init__(self, client: HTTPClient | None = None, *, page_size: int = 25):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=1)
        self.page_size = min(max(page_size, 1), 1000)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config.get("date_field", "index") not in ("index", "update", "created", "pub"):
            raise ValueError("crossref date_field must be index, update, created or pub")
        if config.get("query_field", "all") not in ("all", "title"):
            raise ValueError("crossref query_field must be all or title")
        chunk_days = config.get("date_chunk_days")
        if chunk_days is not None and (isinstance(chunk_days, bool) or
                                       not isinstance(chunk_days, int) or not 1 <= chunk_days <= 14):
            raise ValueError("crossref date_chunk_days must be 1 to 14")
        if config.get("pagination_mode", "cursor") not in ("cursor", "offset"):
            raise ValueError("crossref pagination_mode must be cursor or offset")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        options = topic.get("extensions", {}).get("provider_options", {}).get(self.provider_id, {})
        self.validate_config(options)
        parameters = {"query": " ".join(terms(topic, focus)), "page_size": self.page_size,
                      "query_field": options.get("query_field", "all"),
                      "date_field": options.get("date_field", "index")}
        if options.get("pagination_mode") == "offset":
            parameters["pagination_mode"] = "offset"
        base = plan(self.provider_id, topic, focus, parameters, date_window)
        chunk_days = options.get("date_chunk_days")
        if not (chunk_days and date_window and date_window.start and date_window.end):
            return (base,)
        first, last = date.fromisoformat(date_window.start), date.fromisoformat(date_window.end)
        # Long Bootstrap windows retain their original single query. Weekly's
        # short backcheck can use shallow cursors without changing coverage.
        if last < first or (last - first).days > 31:
            return (base,)
        chunks = []
        left = first
        while left <= last:
            right = min(last, left + timedelta(days=chunk_days - 1))
            window = DateWindow(left.isoformat(), right.isoformat(), date_window.field)
            chunks.append(QueryPlan(self.provider_id,
                                    f"{topic['id']}:{self.provider_id}:{left.isoformat()}",
                                    dict(base.query_parameters), window))
            left = right + timedelta(days=1)
        return tuple(chunks)

    @staticmethod
    def equivalent_cursor_plan(offset_plan: QueryPlan) -> QueryPlan:
        """Reconstruct the legacy cursor signature for an otherwise identical plan.

        A completed cursor checkpoint can be carried forward only after the
        caller verifies its committed source provenance. Partial cursors cannot
        be translated into offsets because the result order may differ.
        """
        if offset_plan.provider_id != "crossref" or offset_plan.query_parameters.get("pagination_mode") != "offset":
            raise ValueError("expected a Crossref offset query plan")
        parameters = dict(offset_plan.query_parameters)
        del parameters["pagination_mode"]
        return QueryPlan(offset_plan.provider_id, offset_plan.query_id,
                         parameters, offset_plan.date_window)

    def _mailto(self) -> str | None:
        value = os.getenv("CROSSREF_MAILTO")
        return value if value and "@" in value and " " not in value else None

    def fetch_page(self, query: QueryPlan, cursor: Any = None) -> PageResult:
        query_field = "query.title" if query.query_parameters.get("query_field") == "title" else "query"
        offset_mode = query.query_parameters.get("pagination_mode") == "offset"
        offset = 0 if cursor is None else cursor
        if offset_mode and (isinstance(offset, bool) or not isinstance(offset, int) or
                            not 0 <= offset <= 10000):
            raise RequestFailure(self.provider_id, "invalid_offset")
        params = {query_field: query.query_parameters["query"], "rows": self.page_size,
                  "mailto": self._mailto()}
        if offset_mode:
            # Date-sized result sets stay below Crossref's 10,000 offset limit.
            # The rolling 14-day backcheck covers records added during a scan.
            params["offset"] = offset
        else:
            params["cursor"] = cursor if cursor is not None else "*"
        window = query.date_window
        if window:
            field = query.query_parameters["date_field"]
            filters = []
            if window.start: filters.append(f"from-{field}-date:{window.start}")
            if window.end: filters.append(f"until-{field}-date:{window.end}")
            if filters: params["filter"] = ",".join(filters)
        data = self.client.get_json(self.endpoint, params)
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise RequestFailure(self.provider_id, "malformed_response")
        now = utcnow()
        rows = tuple(RawRecord(self.provider_id, item, now) for item in message["items"])
        if offset_mode:
            total = message.get("total-results")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise RequestFailure(self.provider_id, "malformed_response")
            if total > 10000:
                raise RequestFailure(self.provider_id, "offset_limit_exceeded")
            next_offset = offset + len(rows)
            if next_offset < total and (not rows or next_offset > 10000):
                raise RequestFailure(self.provider_id, "offset_limit_exceeded")
            more = next_offset < total
            return PageResult(rows, next_offset if more else None, more,
                              RequestMetadata(self.provider_id, now))
        next_cursor = message.get("next-cursor")
        if len(rows) == self.page_size and next_cursor == (cursor if cursor is not None else "*"):
            raise RequestFailure(self.provider_id, "stalled_cursor")
        more = len(rows) == self.page_size and bool(next_cursor)
        return PageResult(rows, next_cursor if more else None, more, RequestMetadata(self.provider_id, now))

    def normalize(self, raw: RawRecord):
        value = raw.payload
        authors = [" ".join(str(item.get(part, "")).strip() for part in ("given", "family")).strip()
                   for item in value.get("author") or []]
        fields = {"title": (value.get("title") or [None])[0], "abstract": plain_text(value.get("abstract")),
                  "authors": [x for x in authors if x],
                  "journal": (value.get("container-title") or [None])[0],
                  "publication_date": date_parts(value.get("published")),
                  "online_date": date_parts(value.get("published-online")),
                  "indexed_date": (value.get("indexed") or {}).get("date-time"),
                  "created_date": (value.get("created") or {}).get("date-time"),
                  "updated_date": (value.get("deposited") or {}).get("date-time"),
                  "document_type": value.get("type"), "language": value.get("language"),
                  "landing_url": value.get("URL"), "issn": value.get("ISSN"),
                  "issn_l": value.get("ISSN-L"),
                  "publisher": value.get("publisher"), "license": value.get("license")}
        return source_record(raw, source_id=value.get("DOI") or "", fields=fields,
                             identifiers={"doi": value.get("DOI")},
                             source_fields={"title": "title.0", "journal": "container-title.0",
                                            "updated_date": "deposited.date-time",
                                            "indexed_date": "indexed.date-time", "doi": "DOI"})

    def fetch_by_identifier(self, identifier: str):
        data = self.client.get_json(self.endpoint + "/" + quote(identifier, safe=""))
        value = data.get("message") if isinstance(data, dict) else None
        if not isinstance(value, dict):
            raise RequestFailure(self.provider_id, "malformed_response")
        return self.normalize(RawRecord(self.provider_id, value, utcnow()))
