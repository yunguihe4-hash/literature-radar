"""Deduplicate discovery records without altering source data."""
from __future__ import annotations
import re
from literature_radar.models import Paper

def identity(paper: Paper) -> str:
    doi = paper.identifiers.get("doi", "").strip().casefold()
    if doi:
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
        return f"doi:{doi}"
    return f"source:{paper.source}:{paper.source_id}"

def deduplicate(papers: list[Paper]) -> list[Paper]:
    selected: dict[str, Paper] = {}
    for paper in papers:
        key = identity(paper)
        previous = selected.get(key)
        if previous is None or len(paper.abstract or "") > len(previous.abstract or ""):
            selected[key] = paper
    return list(selected.values())
