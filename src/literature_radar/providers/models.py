"""JSON-safe request plans and page responses; no API syntax is imposed."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _reject_credentials(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in {"api_key", "apikey", "token", "password", "authorization", "secret"}:
                raise ValueError("query plan must not contain credentials")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)


@dataclass(frozen=True)
class DateWindow:
    start: str | None = None
    end: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"start": self.start, "end": self.end, "field": self.field}


@dataclass(frozen=True)
class QueryPlan:
    provider_id: str
    query_id: str
    query_parameters: Mapping[str, Any]
    date_window: DateWindow | None = None
    query_signature: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.query_id:
            raise ValueError("query plan requires provider_id and query_id")
        _reject_credentials(self.query_parameters)
        canonical = _json({"provider_id": self.provider_id, "query_id": self.query_id,
                           "query_parameters": self.query_parameters,
                           "date_window": self.date_window.as_dict() if self.date_window else None})
        object.__setattr__(self, "query_parameters", _freeze(self.query_parameters))
        object.__setattr__(self, "query_signature", hashlib.sha256(canonical.encode("utf-8")).hexdigest())


@dataclass(frozen=True)
class RawRecord:
    provider_id: str
    payload: dict[str, Any]
    retrieved_at: str


@dataclass(frozen=True)
class RequestMetadata:
    provider_id: str
    retrieved_at: str
    request_id: str | None = None


@dataclass(frozen=True)
class PageResult:
    records: tuple[RawRecord, ...]
    next_cursor: Any
    has_more: bool
    request_metadata: RequestMetadata

    def __post_init__(self) -> None:
        if self.has_more and self.next_cursor is None:
            raise ValueError("has_more requires a next_cursor")
        _json(self.next_cursor)
