"""Serializable paper and source records shared by the public engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any, Mapping


def _date_value(value: str | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO date or date-time")
    try:
        if "T" in value or " " in value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif re.fullmatch(r"\d{4}", value):
            if int(value) < 1:
                raise ValueError()
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            if year < 1 or not 1 <= month <= 12:
                raise ValueError()
        else:
            date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date or date-time") from exc


@dataclass(frozen=True)
class FieldProvenance:
    provider_id: str
    source_id: str
    retrieved_at: str
    source_field: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id or not self.source_id:
            raise ValueError("field provenance requires provider_id and source_id")
        _date_value(self.retrieved_at, "retrieved_at")

    def as_dict(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "source_id": self.source_id,
                "retrieved_at": self.retrieved_at, "source_field": self.source_field}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FieldProvenance:
        return cls(**dict(value))


@dataclass(frozen=True)
class SourceRecord:
    provider_id: str
    source_id: str
    retrieved_at: str
    identifiers: dict[str, str] = field(default_factory=dict)
    normalized_metadata: dict[str, Any] = field(default_factory=dict)
    source_updated_at: str | None = None
    provenance: dict[str, FieldProvenance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.source_id:
            raise ValueError("source record requires provider_id and source_id")
        _date_value(self.source_updated_at, "source_updated_at")
        _date_value(self.retrieved_at, "retrieved_at")
        if not self.retrieved_at:
            raise ValueError("source record requires retrieved_at")

    def as_dict(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "source_id": self.source_id,
                "identifiers": dict(self.identifiers),
                "normalized_metadata": dict(self.normalized_metadata),
                "source_updated_at": self.source_updated_at,
                "retrieved_at": self.retrieved_at,
                "provenance": {key: item.as_dict() for key, item in self.provenance.items()}}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceRecord:
        data = dict(value)
        data["provenance"] = {key: FieldProvenance.from_dict(item)
                              for key, item in (data.get("provenance") or {}).items()}
        return cls(**data)


@dataclass(frozen=True)
class Paper:
    # Keep the first six fields and their positional order for Stage 1 callers.
    source_id: str
    title: str
    abstract: str | None = None
    source: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)
    published: str | None = None  # Legacy field; never inferred from newer dates.
    paper_id: str | None = None
    authors: tuple[str, ...] = ()
    journal: str | None = None
    document_type: str | None = None
    language: str | None = None
    landing_url: str | None = None
    publication_date: str | None = None
    online_date: str | None = None
    indexed_date: str | None = None
    created_date: str | None = None
    updated_date: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    source_records: tuple[SourceRecord, ...] = ()
    provenance: dict[str, FieldProvenance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip() or not isinstance(self.title, str) or not self.title.strip() or not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("paper source_id, title, and source must be nonempty")
        if self.abstract is not None and not isinstance(self.abstract, str):
            raise TypeError("paper abstract must be a string or null")
        for name in ("publication_date", "online_date", "indexed_date", "created_date",
                     "updated_date", "first_seen_at", "last_seen_at"):
            _date_value(getattr(self, name), name)

    def as_dict(self) -> dict[str, Any]:
        base = {"source_id": self.source_id, "title": self.title,
                "abstract": self.abstract, "source": self.source,
                "identifiers": dict(self.identifiers), "published": self.published}
        extended_names = ("paper_id", "journal", "document_type", "language",
                          "landing_url", "publication_date", "online_date", "indexed_date",
                          "created_date", "updated_date", "first_seen_at", "last_seen_at",
                          "doi", "pmid", "pmcid", "openalex_id")
        if self.abstract is None or self.authors or self.source_records or self.provenance or any(getattr(self, name) is not None for name in extended_names):
            base["model_version"] = 2
            base.update({name: getattr(self, name) for name in extended_names})
            base["authors"] = list(self.authors)
            base["source_records"] = [record.as_dict() for record in self.source_records]
            base["provenance"] = {key: item.as_dict() for key, item in self.provenance.items()}
        return base

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Paper:
        data = dict(value)
        version = data.pop("model_version", 1)
        if version not in (1, 2):
            raise ValueError(f"unsupported Paper model_version: {version}")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown Paper fields: {', '.join(sorted(unknown))}")
        data["identifiers"] = dict(data.get("identifiers") or {})
        data["authors"] = tuple(data.get("authors") or ())
        data["source_records"] = tuple(SourceRecord.from_dict(item)
                                       for item in (data.get("source_records") or ()))
        data["provenance"] = {key: FieldProvenance.from_dict(item)
                              for key, item in (data.get("provenance") or {}).items()}
        return cls(**data)
