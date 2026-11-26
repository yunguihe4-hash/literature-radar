"""Provider contracts. Persistence, deduplication and delivery stay upstream."""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable
from literature_radar.models import SourceRecord
from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord


@runtime_checkable
class Provider(Protocol):
    provider_id: str

    def validate_config(self, config: Mapping[str, Any]) -> None: ...

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]: ...

    def fetch_page(self, plan: QueryPlan, cursor: Any = None) -> PageResult: ...

    def normalize(self, raw: RawRecord) -> SourceRecord: ...


class SupportsIdentifierLookup(Protocol):
    def fetch_by_identifier(self, identifier: str) -> SourceRecord | None: ...


class SupportsHealthCheck(Protocol):
    def health_check(self) -> bool: ...
