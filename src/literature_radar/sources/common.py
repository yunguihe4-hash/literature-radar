"""Shared normalization helpers for fictional tests and real adapters."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

from literature_radar.models import FieldProvenance, SourceRecord
from literature_radar.providers.models import DateWindow, QueryPlan, RawRecord


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def terms(topic: Mapping[str, Any], focus: Mapping[str, Any]) -> list[str]:
    result = list(dict.fromkeys([*topic["search_terms"], *focus["search_terms"]]))
    if not result:
        raise ValueError("search terms cannot be empty")
    return result


def plan(provider_id: str, topic: Mapping[str, Any], focus: Mapping[str, Any],
         parameters: dict[str, Any], window: DateWindow | None) -> QueryPlan:
    parameters = {**parameters, "focus_version": focus.get("version")}
    return QueryPlan(provider_id, f"{topic['id']}:{provider_id}", parameters, window)


def plain_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip() or None


def source_record(raw: RawRecord, *, source_id: str, fields: Mapping[str, Any],
                  identifiers: Mapping[str, str | None],
                  source_fields: Mapping[str, str] | None = None) -> SourceRecord:
    if not source_id:
        raise ValueError("missing source_id")
    if not fields.get("title"):
        raise ValueError("missing title")
    identifiers = {key: str(value) for key, value in identifiers.items() if value}
    provenance = {key: FieldProvenance(raw.provider_id, source_id, raw.retrieved_at,
                                      (source_fields or {}).get(key, key))
                  for key, value in fields.items() if value is not None}
    provenance.update({f"identifiers.{key}": FieldProvenance(
        raw.provider_id, source_id, raw.retrieved_at,
        (source_fields or {}).get(key, key)) for key in identifiers})
    return SourceRecord(raw.provider_id, source_id, raw.retrieved_at,
                        dict(identifiers), dict(fields), fields.get("updated_date"), provenance)


def date_parts(value: Any) -> str | None:
    try:
        parts = value["date-parts"][0]
        if 1 <= len(parts) <= 3:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) >= 2 else None
            day = int(parts[2]) if len(parts) == 3 else None
            if year < 1000 or month is not None and not 1 <= month <= 12 or day is not None and not 1 <= day <= 31:
                return None
            if day is not None:
                date(year, month, day)
            return f"{year:04d}" + (f"-{month:02d}" if month is not None else "") + (f"-{day:02d}" if day is not None else "")
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def date_only(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]) else None
