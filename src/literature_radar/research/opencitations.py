"""Original AI_OS OpenCitations v2 depth-one DOI edge lookup."""
from __future__ import annotations

from urllib.parse import quote

from literature_radar.candidates.identity import normalize_doi
from literature_radar.models import SourceRecord
from literature_radar.sources.common import utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure


class OpenCitationsProvider:
    provider_id = "opencitations"

    def __init__(self, client: HTTPClient | None = None):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=1)

    def expand(self, seed_doi: str, relation: str, limit: int) -> tuple[list[SourceRecord], list[dict], int]:
        doi = normalize_doi(seed_doi)
        if doi is None or relation not in ("references", "citations") or not 1 <= limit <= 200:
            raise ValueError("OpenCitations needs a DOI, relation and bounded limit")
        endpoint = quote("doi:" + doi, safe=":")
        rows = self.client.get_json(
            f"https://api.opencitations.net/index/v2/{relation}/{endpoint}")
        if not isinstance(rows, list):
            raise RequestFailure(self.provider_id, "malformed_response")
        records, retained, seen = [], [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise RequestFailure(self.provider_id, "malformed_response")
            value = row.get("cited") if relation == "references" else row.get("citing")
            related = next((normalize_doi(token[4:]) for token in str(value or "").split()
                            if token.casefold().startswith("doi:")), None)
            if related is None or related == doi or related in seen:
                continue
            seen.add(related)
            source_id = f"{relation}:{doi}:{related}"
            stamp = utcnow()
            records.append(SourceRecord(self.provider_id, source_id, stamp,
                                        {"doi": related},
                                        {"title": f"[Metadata unavailable; DOI {related}]",
                                         "landing_url": f"https://doi.org/{related}",
                                         "citation_seed_doi": doi,
                                         "citation_relation": relation,
                                         "citation_edge": row}))
            retained.append(row)
            if len(records) >= limit:
                break
        return records, retained, len(rows)
