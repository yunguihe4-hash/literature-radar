"""Europe PMC REST search adapter with CORE/LITE and local date semantics."""
from __future__ import annotations

from typing import Any, Mapping

from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.common import date_only, plan, source_record, terms, utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure


class EuropePMCProvider:
    supports_semantic_groups = True
    provider_id = "europepmc"
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    sync_capability = "CREATION_DATE and UPDATE_DATE index windows"

    def __init__(self, client: HTTPClient | None = None, *, page_size: int = 25):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=1)
        self.page_size = min(max(page_size, 1), 1000)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config.get("result_type", "core").lower() not in ("core", "lite"):
            raise ValueError("europepmc result_type must be core or lite")
        if config.get("date_field", "UPDATE_DATE") not in ("UPDATE_DATE", "CREATION_DATE", "FIRST_PDATE"):
            raise ValueError("europepmc date_field must be UPDATE_DATE, CREATION_DATE or FIRST_PDATE")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        options = topic.get("extensions", {}).get("provider_options", {}).get(self.provider_id, {})
        self.validate_config(options)
        return (plan(self.provider_id, topic, focus,
                     {"query": " OR ".join(f'({term})' for term in terms(topic, focus)),
                      "page_size": self.page_size,
                      "result_type": options.get("result_type", "core").lower(),
                      "date_field": options.get("date_field", "UPDATE_DATE")}, date_window),)

    def fetch_page(self, query: QueryPlan, cursor: Any = None) -> PageResult:
        expression = "(" + query.query_parameters["query"] + ")"
        window = query.date_window
        if window and (window.start or window.end):
            left = window.start or "*"
            right = window.end or "*"
            expression += f" AND {query.query_parameters['date_field']}:[{left} TO {right}]"
        data = self.client.get_json(self.endpoint, {
            "query": expression, "format": "json",
            "resultType": query.query_parameters["result_type"],
            "pageSize": self.page_size, "cursorMark": cursor if cursor is not None else "*"})
        items = (data.get("resultList") or {}).get("result") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RequestFailure(self.provider_id, "malformed_response")
        now = utcnow()
        rows = tuple(RawRecord(self.provider_id, item, now) for item in items)
        next_cursor = data.get("nextCursorMark")
        more = bool(rows) and next_cursor is not None and next_cursor != (cursor if cursor is not None else "*")
        return PageResult(rows, next_cursor if more else None, more, RequestMetadata(self.provider_id, now))

    def fetch_by_identifier(self, identifier: str, *, kind: str = "doi"):
        """Original AI_OS identifier lookup; no date-window discovery query."""
        if kind == "pmid":
            expression = f"EXT_ID:{identifier} AND SRC:MED"
        elif kind == "pmcid":
            expression = f"PMCID:{identifier}"
        elif kind == "doi":
            expression = 'DOI:"' + identifier.replace('"', '') + '"'
        else:
            raise ValueError("Europe PMC lookup requires DOI, PMID or PMCID")
        data = self.client.get_json(self.endpoint, {
            "query": expression, "format": "json", "resultType": "core", "pageSize": 2})
        if not isinstance(data, dict) or "errCode" in data:
            raise RequestFailure(self.provider_id, "malformed_or_error_response")
        items = (data.get("resultList") or {}).get("result")
        if not isinstance(items, list):
            raise RequestFailure(self.provider_id, "malformed_response")
        return self.normalize(RawRecord(self.provider_id, items[0], utcnow())) if items else None

    def normalize(self, raw: RawRecord):
        value = raw.payload
        author_list = value.get("authorList") or {}
        if not isinstance(author_list, dict):
            author_list = {}
        authors = [item.get("fullName") for item in (author_list.get("author") or [])
                   if isinstance(item, dict)]
        journal_container = value.get("journalInfo") or {}
        if not isinstance(journal_container, dict):
            journal_container = {}
        journal_info = journal_container.get("journal") or {}
        if not isinstance(journal_info, dict):
            journal_info = {}
        journal = value.get("journalTitle") or journal_info.get("title")
        fields = {"title": value.get("title"), "abstract": value.get("abstractText"),
                  "authors": [x for x in authors if x], "journal": journal,
                  "issn": journal_info.get("issn"),
                  "publication_date": date_only(value.get("firstPublicationDate")),
                  "online_date": date_only(value.get("electronicPublicationDate")),
                  "indexed_date": date_only(value.get("firstIndexDate")),
                  "created_date": date_only(value.get("creationDate")),
                  "updated_date": date_only(value.get("updateDate")),
                  "document_type": value.get("pubType"),
                  "language": value.get("language"),
                  "landing_url": None, "preprint_source": value.get("source")}
        source_id = f"{value.get('source', 'MED')}:{value.get('id', '')}"
        return source_record(raw, source_id=source_id, fields=fields,
                             identifiers={"doi": value.get("doi"), "pmid": value.get("pmid"),
                                          "pmcid": value.get("pmcid")})
