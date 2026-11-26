"""bioRxiv date-window details and published-version relationship adapter."""
from __future__ import annotations

from typing import Any, Mapping

from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.common import date_only, source_record, terms, utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure


class BioRxivProvider:
    provider_id = "biorxiv"
    base = "https://api.biorxiv.org"
    sync_capability = "posted-date window; versions and published DOI relationship"

    def __init__(self, client: HTTPClient | None = None, *, page_size: int = 30):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=1)
        self.page_size = 30  # Official details endpoint fixes this page size.

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config.get("server", "biorxiv").lower() != "biorxiv":
            raise ValueError("biorxiv server must be biorxiv")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        if not date_window or not date_window.start or not date_window.end:
            raise ValueError("biorxiv requires a closed date window")
        options = topic.get("extensions", {}).get("provider_options", {}).get(self.provider_id, {})
        self.validate_config(options)
        parameters = {"terms": terms(topic, focus), "focus_version": focus.get("version"), "kind": "details",
                      "page_size": 30}
        details = QueryPlan(self.provider_id, f"{topic['id']}:biorxiv:details", parameters, date_window)
        pubs = QueryPlan(self.provider_id, f"{topic['id']}:biorxiv:pubs",
                         {**parameters, "kind": "pubs", "page_size": 100}, date_window)
        return details, pubs

    def fetch_page(self, query: QueryPlan, cursor: Any = None) -> PageResult:
        offset = 0 if cursor is None else cursor
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("biorxiv cursor must be a nonnegative offset")
        kind = query.query_parameters["kind"]
        endpoint = (f"{self.base}/{kind}/biorxiv/"
                    f"{query.date_window.start}/{query.date_window.end}/{offset}/json")
        data = self.client.get_json(endpoint)
        if not isinstance(data, dict) or not isinstance(data.get("collection"), list):
            raise RequestFailure(self.provider_id, "malformed_response")
        collection = data["collection"]
        total = None
        for item in data.get("messages") or []:
            if isinstance(item, dict):
                try:
                    if item.get("total") is not None:
                        total = int(item["total"])
                except (TypeError, ValueError): pass
        if total is None:
            raise RequestFailure(self.provider_id, "malformed_response")
        if total is not None and total < 0:
            raise RequestFailure(self.provider_id, "malformed_response")
        if total is not None and not collection and offset < total:
            raise RequestFailure(self.provider_id, "incomplete_page")
        now = utcnow()
        filtered = []
        for value in collection:
            content = (f"{value.get('title') or value.get('preprint_title') or ''} "
                       f"{value.get('abstract') or value.get('preprint_abstract') or ''}").casefold()
            if any(term.casefold() in content for term in query.query_parameters["terms"]):
                filtered.append(value)
        rows = tuple(RawRecord(self.provider_id, {**value, "_kind": kind}, now) for value in filtered)
        size = 30 if kind == "details" else 100
        more = (offset + len(collection) < total and bool(collection)) if total is not None else len(collection) == size
        return PageResult(rows, offset + len(collection) if more else None, more,
                          RequestMetadata(self.provider_id, now))

    def normalize(self, raw: RawRecord):
        value = raw.payload
        kind = value.get("_kind", "details")
        doi = (value.get("doi") if kind == "details" else
               value.get("preprint_doi") or value.get("biorxiv_doi"))
        version = value.get("version")
        title = value.get("title") if kind == "details" else value.get("preprint_title")
        author_value = value.get("authors") or value.get("preprint_authors") or ""
        if isinstance(author_value, str):
            authors = [item.strip() for item in author_value.split(";") if item.strip()]
        elif isinstance(author_value, list):
            authors = [str(item).strip() for item in author_value if str(item).strip()]
        else:
            authors = []
        fields = {"title": title,
                  "abstract": value.get("abstract") if kind == "details" else value.get("preprint_abstract"),
                  "authors": authors,
                  "journal": None, "publication_date": None,
                  "online_date": date_only(value.get("date") or value.get("preprint_date")),
                  "indexed_date": None, "created_date": None,
                  "updated_date": date_only(value.get("date") or value.get("preprint_date")),
                  "document_type": "preprint", "language": None,
                  "landing_url": f"https://www.biorxiv.org/content/{doi}" if doi else None,
                  "version": str(version) if version is not None else None,
                  "category": value.get("category") or value.get("preprint_category"),
                  "license": value.get("license"),
                  "published_journal": value.get("published_journal") if kind == "pubs" else None,
                  "published_doi": value.get("published") or value.get("published_doi")}
        source_id = f"{doi}#v{version}" if version is not None else f"{doi}#{kind}"
        return source_record(raw, source_id=source_id if doi else "", fields=fields,
                             identifiers={"doi": doi},
                             source_fields={"abstract": "abstract" if kind == "details" else "preprint_abstract",
                                            "doi": "doi" if kind == "details" else "preprint_doi",
                                            "published_doi": "published" if kind == "details" else "published_doi"})

    def fetch_by_identifier(self, identifier: str):
        data = self.client.get_json(f"{self.base}/details/biorxiv/{identifier}/na/json")
        rows = data.get("collection") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        return self.normalize(RawRecord(self.provider_id, {**rows[-1], "_kind": "details"}, utcnow()))
