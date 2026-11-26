"""Conservative cross-source identifiers and provenance-aware field selection."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Mapping
from urllib.parse import unquote

from literature_radar.models import Paper, SourceRecord


class IdentityConflict(ValueError):
    pass


def normalize_doi(value: str | None) -> str | None:
    if not value: return None
    text = unquote(str(value)).strip()
    text = re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.I)
    text = text.strip().strip(".,;()[]{}<>").casefold()
    return text if re.fullmatch(r"10\.\d{4,9}/\S+", text) else None


def identifiers(record: SourceRecord) -> dict[str, str]:
    result = {}
    for kind, value in record.identifiers.items():
        if not value: continue
        if kind == "doi":
            normalized = normalize_doi(value)
        elif kind == "pmid":
            normalized = re.sub(r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/", "", str(value).strip()).strip("/")
            if not normalized.isdigit(): normalized = None
        elif kind == "pmcid":
            match = re.search(r"(?:^|/)(PMC\d+)(?:/?$)", str(value).strip(), flags=re.I)
            normalized = match.group(1).upper() if match else None
        elif kind == "openalex_id":
            normalized = str(value).strip().rsplit("/", 1)[-1].upper()
            if not re.fullmatch(r"W\d+", normalized): normalized = None
        else:
            continue
        if normalized: result[kind] = normalized
    return result


def _nonempty(value) -> bool:
    return value is not None and value != "" and value != [] and value != ()


def _rank(provider: str, field: str, priorities: Mapping[str, list[str]]) -> int:
    return (priorities.get(field) or []).index(provider) if provider in (priorities.get(field) or []) else 1000


def _newer_same_provider(existing: Paper, field: str, record: SourceRecord) -> bool:
    previous = existing.provenance.get(field)
    if previous is None:
        return any((item.provider_id, item.source_id) == (record.provider_id, record.source_id)
                   for item in existing.source_records)
    if previous.provider_id != record.provider_id:
        return False
    if previous.source_id == record.source_id:
        return True
    old = next((item for item in existing.source_records
                if (item.provider_id, item.source_id) == (previous.provider_id, previous.source_id)), None)
    if old is None:
        return False
    old_version = old.normalized_metadata.get("version")
    new_version = record.normalized_metadata.get("version")
    if old_version is not None and new_version is not None:
        try:
            return int(new_version) > int(old_version)
        except (TypeError, ValueError):
            return False
    if old.source_updated_at and record.source_updated_at:
        return record.source_updated_at > old.source_updated_at
    return False


def merge(existing: Paper | None, record: SourceRecord, paper_id: str,
          priorities: Mapping[str, list[str]] | None = None) -> Paper:
    priorities = priorities or {}
    values = record.normalized_metadata
    ids = identifiers(record)
    if existing is not None:
        old_doi = normalize_doi(existing.doi or existing.identifiers.get("doi"))
        if old_doi and ids.get("doi") and old_doi != ids["doi"]:
            raise IdentityConflict("conflicting DOI for matched identity")
        for kind in ("pmid", "pmcid", "openalex_id"):
            old_value = existing.identifiers.get(kind) or getattr(existing, kind)
            if old_value and ids.get(kind) and old_value != ids[kind]:
                raise IdentityConflict(f"conflicting {kind} for matched identity")
    updates = {}
    for field in ("title", "abstract", "authors", "journal", "document_type", "language",
                  "landing_url", "publication_date", "online_date", "indexed_date",
                  "created_date", "updated_date"):
        incoming = values.get(field)
        if not _nonempty(incoming): continue
        if field == "authors":
            incoming = tuple(incoming) if isinstance(incoming, list) else tuple([incoming])
        current = getattr(existing, field) if existing else None
        previous = existing.provenance.get(field) if existing else None
        replace_field = not _nonempty(current)
        if field == "title" and isinstance(current, str) and current.startswith("[Metadata unavailable; DOI "):
            replace_field = True
        if field == "abstract" and existing and previous and current is None:
            # A missing stored abstract may be a deliberate no-cache policy.
            # Its provenance still defines the selected source's priority.
            replace_field = False
        if existing and not replace_field:
            replace_field = _newer_same_provider(existing, field, record) or bool(
                previous and _rank(record.provider_id, field, priorities) <
                _rank(previous.provider_id, field, priorities))
        if replace_field:
            updates[field] = incoming
    if existing is None:
        if not values.get("title"):
            raise ValueError("source record is missing title")
        paper = Paper(record.source_id, values["title"], None, record.provider_id,
                      paper_id=paper_id)
    else:
        paper = existing
    provenance = dict(paper.provenance)
    for field in updates:
        if field in record.provenance: provenance[field] = record.provenance[field]
    combined_ids = dict(paper.identifiers)
    combined_ids.update(ids)
    records = { (item.provider_id, item.source_id): item for item in paper.source_records }
    records[(record.provider_id, record.source_id)] = record
    return replace(paper, **updates, identifiers=combined_ids,
                   doi=ids.get("doi") or paper.doi, pmid=ids.get("pmid") or paper.pmid,
                   pmcid=ids.get("pmcid") or paper.pmcid,
                   openalex_id=ids.get("openalex_id") or paper.openalex_id,
                   source_records=tuple(records.values()), provenance=provenance)
