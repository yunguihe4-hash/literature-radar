"""Interfaces and data structures for independent discovery providers."""
from literature_radar.providers.base import Provider, SupportsHealthCheck, SupportsIdentifierLookup
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.providers.registry import ProviderRegistry

__all__ = ("Provider", "SupportsHealthCheck", "SupportsIdentifierLookup", "DateWindow",
           "PageResult", "QueryPlan", "RawRecord", "RequestMetadata", "ProviderRegistry")
