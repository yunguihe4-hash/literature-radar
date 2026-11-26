"""Licensed JCR-only journal evidence for an explicit human review workflow."""
from __future__ import annotations

import csv
import difflib
import json
import math
import re
from datetime import date
from pathlib import Path

from literature_radar.journal.registry import _xlsx_rows

FIELDS = ("journal_title", "issn", "issn_l", "jcr_year", "impact_factor",
          "jcr_quartile", "subject_category", "source", "license_or_usage_basis",
          "retrieved_at")
FLAGSHIPS = frozenset(("cell", "nature", "science"))
_ISSN = re.compile(r"^\d{4}-\d{3}[\dX]$", re.I)


def _title(value: str | None) -> str:
    return " ".join(re.findall(r"\w+", (value or "").casefold()))


def _issn(value: str | None) -> str | None:
    if not value:
        return None
    result = value.strip().upper()
    if not _ISSN.fullmatch(result):
        raise ValueError("Invalid ISSN syntax")
    digits = result.replace("-", "")
    checksum = sum((10 if digit == "X" else int(digit)) * weight
                   for digit, weight in zip(digits, range(8, 0, -1)))
    if checksum % 11:
        raise ValueError("Invalid ISSN check digit")
    return result


def validate_jcr_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("JCR row must be an object")
    title = str(row.get("journal_title") or "").strip()
    if not title:
        raise ValueError("JCR journal_title is required")
    issns = row.get("issn") or ""
    if isinstance(issns, list):
        issns = ";".join(issns)
    identifiers = tuple(dict.fromkeys(_issn(part) for part in str(issns).split(";") if part.strip()))
    issn_l = _issn(row.get("issn_l"))
    year = str(row.get("jcr_year") or "")
    if not year.isdigit() or not 1900 <= int(year) <= 2200:
        raise ValueError("JCR year must be a four-digit edition year")
    quartile = str(row.get("jcr_quartile") or "").strip().upper()
    if quartile not in {"Q1", "Q2", "Q3", "Q4"}:
        raise ValueError("JCR quartile must be Q1-Q4")
    impact = row.get("impact_factor")
    if impact in (None, ""):
        impact = None
    else:
        try:
            impact = float(impact)
        except (TypeError, ValueError) as exc:
            raise ValueError("Impact Factor must be numeric") from exc
        if not math.isfinite(impact) or impact < 0:
            raise ValueError("Impact Factor must be finite and nonnegative")
    category = str(row.get("subject_category") or "").strip()
    source = str(row.get("source") or "").strip()
    usage = str(row.get("license_or_usage_basis") or "").strip()
    retrieved = str(row.get("retrieved_at") or "").strip()
    if not all((category, source, usage, retrieved)):
        raise ValueError("JCR category, source, usage basis and retrieval date are required")
    try:
        date.fromisoformat(retrieved)
    except ValueError as exc:
        raise ValueError("retrieved_at must be an ISO date") from exc
    return {"journal_title": title, "issn": identifiers, "issn_l": issn_l,
            "jcr_year": int(year), "impact_factor": impact,
            "jcr_quartile": quartile, "subject_category": category,
            "source": source, "license_or_usage_basis": usage,
            "retrieved_at": retrieved}


def load_jcr_catalog(path: Path | None) -> list[dict]:
    if path is None:
        return []
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif path.suffix.lower() == ".xlsx":
        rows = _xlsx_rows(path)
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("entries") if isinstance(data, dict) else data
    else:
        raise ValueError("JCR catalog must be CSV, XLSX or JSON")
    if not isinstance(rows, list):
        raise ValueError("JCR catalog must contain rows")
    normalized = [validate_jcr_row(row) for row in rows]
    seen = {}
    for row in normalized:
        identity = row["issn_l"] or (row["issn"][0] if row["issn"] else _title(row["journal_title"]))
        key = (identity, row["jcr_year"], _title(row["subject_category"]))
        if key in seen:
            if seen[key] != row:
                raise ValueError("Conflicting JCR records for the same identity, year and category")
            raise ValueError("Duplicate JCR record")
        seen[key] = row
    return normalized


def flagship_tier(journal_title: str | None) -> str:
    return "TIER_1_FLAGSHIP" if (journal_title or "").strip().casefold() in FLAGSHIPS else "NOT_FLAGSHIP"


def match_jcr(catalog: list[dict], *, journal_title: str | None,
              issn: list[str] | None = None, issn_l: str | None = None) -> dict:
    baseline = {"status": "UNVERIFIED", "match_basis": None, "match_confidence": "NONE",
                "journal_title": journal_title, "jcr_year": None, "impact_factor": None,
                "jcr_quartile": None, "subject_categories": [], "source": None,
                "flagship_tier": flagship_tier(journal_title), "review_suggestions": []}
    if not catalog:
        return baseline
    input_issns = {item.strip().upper() for item in issn or [] if item}
    input_l = issn_l.strip().upper() if issn_l else None
    choices = [("ISSN", [row for row in catalog if input_issns & set(row["issn"])]),
               ("ISSN_L", [row for row in catalog if input_l and input_l == row["issn_l"]]),
               ("TITLE_EXACT", [row for row in catalog if _title(journal_title) and
                                _title(journal_title) == _title(row["journal_title"])])]
    basis, matched = next(((name, rows) for name, rows in choices if rows), (None, []))
    if not matched:
        known = sorted({row["journal_title"] for row in catalog})
        baseline["review_suggestions"] = difflib.get_close_matches(journal_title or "", known, n=3, cutoff=0.85)
        return baseline
    titles = {_title(row["journal_title"]) for row in matched}
    if len(titles) != 1 or (journal_title and _title(journal_title) not in titles):
        return {**baseline, "status": "CONFLICT", "match_basis": basis,
                "review_suggestions": sorted({row["journal_title"] for row in matched})}
    year = max(row["jcr_year"] for row in matched)
    selected = [row for row in matched if row["jcr_year"] == year]
    impacts = {row["impact_factor"] for row in selected}
    if len(impacts) != 1:
        return {**baseline, "status": "CONFLICT", "match_basis": basis}
    conservative = max(selected, key=lambda row: int(row["jcr_quartile"][1]))
    return {**baseline, "status": "VERIFIED" if conservative["impact_factor"] is not None else "IMPACT_FACTOR_MISSING",
            "match_basis": basis, "match_confidence": "HIGH" if basis == "ISSN" else "MEDIUM",
            "journal_title": conservative["journal_title"], "jcr_year": year,
            "impact_factor": conservative["impact_factor"],
            "jcr_quartile": conservative["jcr_quartile"],
            "subject_categories": sorted({row["subject_category"] for row in selected}),
            "source": conservative["source"]}
