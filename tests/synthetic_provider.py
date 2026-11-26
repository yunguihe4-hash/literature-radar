"""Deterministic fictional provider for incremental pipeline tests."""
from literature_radar.providers.models import PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.models import SourceRecord, FieldProvenance

STAMP = "2026-01-01T00:00:00+00:00"

class SyntheticProvider:
    sync_capability = "offline_synthetic"
    def __init__(self, provider_id, pages, *, fail_at=None):
        self.provider_id = provider_id
        self.pages = pages
        self.fail_at = fail_at
        self.calls = []
        self.fetched_windows = []

    def plan_queries(self, topic, focus, date_window=None):
        return (QueryPlan(self.provider_id, topic["id"] + ":" + self.provider_id,
                          {"terms": topic["search_terms"], "focus_version": focus["version"]}, date_window),)

    def fetch_page(self, query, cursor=None):
        index = 0 if cursor is None else int(cursor)
        self.calls.append(index)
        self.fetched_windows.append(query.date_window)
        if self.fail_at is not None and index == self.fail_at:
            raise TimeoutError("synthetic timeout")
        rows = self.pages[index] if index < len(self.pages) else []
        more = index + 1 < len(self.pages)
        return PageResult(tuple(RawRecord(self.provider_id, row, STAMP) for row in rows),
                          index + 1 if more else None, more, RequestMetadata(self.provider_id, STAMP))

    def normalize(self, raw):
        row = raw.payload
        if row.get("malformed"):
            raise ValueError("synthetic malformed record")
        fields = {"title": row["title"], "abstract": row.get("abstract"),
                  "authors": row.get("authors", []), "publication_date": row.get("publication_date")}
        provenance = {key: FieldProvenance(self.provider_id, row["source_id"], STAMP, key)
                      for key, value in fields.items() if value is not None}
        return SourceRecord(self.provider_id, row["source_id"], STAMP,
                            {key: row[key] for key in ("doi", "pmid", "pmcid", "openalex_id") if row.get(key)},
                            fields, None, provenance)

def fictional(source, doi="10.0000/fictional", **extra):
    return {"source_id": source, "title": "Imaginary acoustic material", "doi": doi,
            "abstract": "Synthetic acoustics only", **extra}
