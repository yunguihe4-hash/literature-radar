"""Paper-level scientific change events, independent of source-record counts."""
from __future__ import annotations

import hashlib
import json

from literature_radar.models import Paper

NEW_PAPER = "NEW_PAPER"
MATERIAL_UPDATE = "MATERIAL_UPDATE"
DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
REVIEW_CONFLICT = "REVIEW_CONFLICT"
UNCHANGED = "UNCHANGED"


def content_version(paper: Paper, *, abstract_hash: str | None = None) -> str:
    """Exclude retrieval timestamps, citations and source-only observations."""
    # Bibliographic corrections and indexing dates are retained in the Paper,
    # but they do not by themselves establish changed scientific content.
    payload = {name: getattr(paper, name) for name in ("title", "document_type")}
    payload["abstract_hash"] = (hashlib.sha256(paper.abstract.encode()).hexdigest()
                                if paper.abstract is not None else abstract_hash)
    payload["versions"] = sorted({str(record.normalized_metadata["version"])
                                  for record in paper.source_records
                                  if record.normalized_metadata.get("version") is not None})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
