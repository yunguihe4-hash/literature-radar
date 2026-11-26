"""Build query terms from Topic and Focus and call an injected provider."""
from __future__ import annotations
from typing import Protocol
from literature_radar.models import Paper

class DiscoveryProvider(Protocol):
    def search(self, terms: list[str], source: str, limit: int) -> list[Paper]: ...

def discover(bundle: dict, provider: DiscoveryProvider, limit: int = 100) -> list[Paper]:
    if limit < 1:
        raise ValueError("discovery limit must be positive")
    terms = list(dict.fromkeys(bundle["topic"]["search_terms"] + bundle["focus"]["search_terms"]))
    papers = []
    for source in bundle["topic"]["sources"]:
        papers.extend(provider.search(terms, source, limit))
    return papers
