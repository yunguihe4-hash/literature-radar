"""Construct only explicitly selected live providers."""
from __future__ import annotations

import os
from typing import Mapping, Any

from literature_radar.sources.biorxiv import BioRxivProvider
from literature_radar.sources.crossref import CrossrefProvider
from literature_radar.sources.europepmc import EuropePMCProvider
from literature_radar.sources.http import HTTPClient
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.sources.pubmed import PubMedProvider

PROVIDERS = {
    "openalex": OpenAlexProvider, "crossref": CrossrefProvider,
    "europepmc": EuropePMCProvider, "pubmed": PubMedProvider,
    "biorxiv": BioRxivProvider,
}


def make_provider(name: str, *, page_size: int = 25, options: Mapping[str, Any] | None = None):
    try:
        cls = PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider_id: {name}") from exc
    if name == "openalex":
        rate = 1.0
    elif name == "pubmed":
        rate = 8.0 if os.getenv("NCBI_API_KEY") else 2.5
    else:
        rate = 1.0
    mailto = os.getenv("CROSSREF_MAILTO")
    if name == "crossref" and mailto and "@" in mailto and " " not in mailto:
        rate = 2.5
    options = options or {}
    requested_page_size = options.get("page_size", page_size)
    if (isinstance(requested_page_size, bool) or not isinstance(requested_page_size, int)
            or not 1 <= requested_page_size <= 1000):
        raise ValueError(f"{name} page_size must be an integer between 1 and 1000")
    requested = options.get("rate_per_second", rate)
    ceiling = {"openalex": 30 if os.getenv("OPENALEX_API_KEY") else 10,
               "crossref": 3 if mailto and "@" in mailto and " " not in mailto else 1,
               "pubmed": 10 if os.getenv("NCBI_API_KEY") else 3}.get(name, 10)
    if isinstance(requested, bool) or not isinstance(requested, (int, float)) or not 0 < requested <= ceiling:
        raise ValueError(f"{name} rate_per_second must be positive and at most {ceiling}")
    # Live CORE pages from Europe PMC and large Crossref/bioRxiv pages have
    # timed out inside the previous 25-second request cap. Keep retries bounded
    # by discovery's per-query deadline while allowing a slow page to finish.
    timeout_defaults = {
        "europepmc": (45.0, 60.0, 3),
        "crossref": (30.0, 45.0, 3),
        "biorxiv": (30.0, 45.0, 3),
    }
    default_read, default_total, default_retries = timeout_defaults.get(name, (15.0, 25.0, 2))
    connect = options.get("connect_timeout", 5.0)
    read = options.get("read_timeout", default_read)
    total = options.get("total_timeout", default_total)
    retries = options.get("max_retries", default_retries)
    contact_email = (options.get("contact_email") or os.getenv("LITERATURE_RADAR_CONTACT_EMAIL")
                     or (mailto if name == "crossref" else None))
    return cls(HTTPClient(name, rate_per_second=requested, connect_timeout=connect,
                          read_timeout=read, total_timeout=total, max_retries=retries,
                          contact_email=contact_email, proxy_url=options.get("proxy_url"),
                          use_environment_proxy=options.get("use_environment_proxy", False)),
               page_size=requested_page_size)
