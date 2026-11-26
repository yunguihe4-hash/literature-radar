"""Strict AI_OS recall benchmark files against one private Topic's known papers."""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.candidates.identity import normalize_doi
from literature_radar.config import load_bundle
from literature_radar.private_paths import ensure_private_path

CATEGORIES = ("methods", "resources", "species", "applications")
PAPER_FIELDS = ("title", "doi", "pmid", "category", "reason", "importance", "keywords")


def _scalar(text: str) -> str | None:
    value = text.strip()
    if value in {"null", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, str):
            return parsed
    raise ValueError("benchmark scalars must be quoted strings or null")


def load_benchmark_file(path: Path, expected_category: str) -> list[dict]:
    """Preserve the original small, strict YAML subset without a YAML dependency."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "seed_papers:":
        raise ValueError(f"{path}: expected top-level seed_papers")
    papers, current, in_keywords = [], None, False
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if line.startswith("  - title:"):
            if current is not None:
                papers.append(current)
            current = {"title": _scalar(line.split(":", 1)[1]), "keywords": []}
            in_keywords = False
        elif line.startswith("    keywords:"):
            if current is None:
                raise ValueError(f"{path}:{line_number}: keywords before paper")
            in_keywords = True
        elif line.startswith("      - ") and in_keywords:
            current["keywords"].append(_scalar(line[8:]))
        elif line.startswith("    ") and ":" in line:
            if current is None:
                raise ValueError(f"{path}:{line_number}: field before paper")
            key, value = line.strip().split(":", 1)
            if key not in PAPER_FIELDS or key in {"title", "keywords"}:
                raise ValueError(f"{path}:{line_number}: unsupported or misplaced field {key!r}")
            current[key] = _scalar(value)
            in_keywords = False
        else:
            raise ValueError(f"{path}:{line_number}: unsupported YAML structure")
    if current is not None:
        papers.append(current)
    for index, paper in enumerate(papers, start=1):
        missing = [field for field in PAPER_FIELDS if field not in paper]
        if missing:
            raise ValueError(f"{path}: paper {index} missing fields: {', '.join(missing)}")
        if paper["category"] != expected_category:
            raise ValueError(f"{path}: paper {index} category mismatch")
        if not paper["title"] or not paper["reason"] or not paper["importance"] or not paper["keywords"]:
            raise ValueError(f"{path}: paper {index} has an empty required value")
    return papers


def _title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})
    value = re.sub(r"[^\w\s]", " ", value.casefold(), flags=re.UNICODE)
    return " ".join(re.sub(r"_+", " ", value).split())


def evaluate_benchmark(config_dir: Path, database: Path, benchmark_dir: Path,
                       output: Path) -> dict:
    """Read-only audit; an existing report is never replaced with different data."""
    config_dir = ensure_private_path(config_dir)
    database = ensure_private_path(database)
    benchmark_dir = ensure_private_path(benchmark_dir)
    output = ensure_private_path(output)
    topic_id = load_bundle(config_dir)["topic"]["id"]
    if not database.is_file():
        raise ValueError("benchmark database does not exist")
    seeds = []
    for category in CATEGORIES:
        path = benchmark_dir / f"{category}.yaml"
        if not path.is_file():
            raise ValueError(f"missing benchmark file: {path}")
        seeds.extend(load_benchmark_file(path, category))
    if not seeds:
        raise ValueError("benchmark contains no papers")
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        has_specialty = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialty_topic_papers'").fetchone()
        if has_specialty:
            rows = connection.execute(
                "SELECT DISTINCT p.paper_id,p.payload FROM papers_v2 p "
                "LEFT JOIN topic_paper_state s ON s.paper_id=p.paper_id AND s.topic_id=? "
                "LEFT JOIN specialty_topic_papers x ON x.paper_id=p.paper_id AND x.topic_id=? "
                "WHERE s.paper_id IS NOT NULL OR x.paper_id IS NOT NULL",
                (topic_id, topic_id)).fetchall()
        else:
            rows = connection.execute(
                "SELECT p.paper_id,p.payload FROM papers_v2 p "
                "JOIN topic_paper_state s ON s.paper_id=p.paper_id WHERE s.topic_id=?",
                (topic_id,)).fetchall()
    known = [(paper_id, json.loads(payload)) for paper_id, payload in rows]
    results = []
    for seed in seeds:
        match_method, paper_ids = None, []
        for method in ("doi", "pmid", "normalized_title"):
            if method == "doi" and seed["doi"]:
                target = normalize_doi(seed["doi"])
                paper_ids = [paper_id for paper_id, paper in known
                             if target and normalize_doi((paper.get("identifiers") or {}).get("doi")
                                                         or paper.get("doi")) == target]
            elif method == "pmid" and seed["pmid"]:
                target = str(seed["pmid"]).strip()
                paper_ids = [paper_id for paper_id, paper in known
                             if str((paper.get("identifiers") or {}).get("pmid") or "").strip() == target]
            elif method == "normalized_title":
                target = _title(seed["title"])
                paper_ids = [paper_id for paper_id, paper in known
                             if _title(paper.get("title") or "") == target]
            if paper_ids:
                match_method = method
                break
        results.append({"title": seed["title"], "doi": seed["doi"], "pmid": seed["pmid"],
                        "category": seed["category"], "status": "found" if paper_ids else "missing",
                        "match_method": match_method, "paper_ids": sorted(paper_ids)})
    by_category = {}
    for category in CATEGORIES:
        category_rows = [row for row in results if row["category"] == category]
        found = sum(row["status"] == "found" for row in category_rows)
        total = len(category_rows)
        by_category[category] = {"total_papers": total, "found": found,
                                 "missing": total - found,
                                 "recall_rate": round(found / total, 6) if total else 0.0}
    found = sum(row["status"] == "found" for row in results)
    report = {"topic_id": topic_id,
              "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "benchmark_dir": str(benchmark_dir.resolve()),
              "database": str(database.resolve()), "scope": "topic",
              "total_papers": len(results), "found": found, "missing": len(results) - found,
              "recall_rate": round(found / len(results), 6),
              "by_category": by_category, "papers": results}
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("benchmark output exists; refusing to overwrite") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return report
