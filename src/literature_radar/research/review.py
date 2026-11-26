"""Strict human evaluation exchange for optional research modes.

The six dimensions, default weights and field validation follow the final
AI_OS evaluation module. Paper IDs are strings in the independent database.
This exchange never makes a scientific judgment or writes Knowledge itself.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.private_paths import ensure_private_path
from literature_radar.topics.focus import _write_once

DIMENSIONS = ("topic_relevance", "focus_relevance", "methodological_value",
              "evidence_quality", "novelty_value", "application_value")
DEFAULT_WEIGHTS = {
    "baseline": dict(zip(DIMENSIONS, (.25, .15, .20, .20, .10, .10))),
    "weekly": dict(zip(DIMENSIONS, (.20, .30, .15, .15, .15, .05))),
}
RECOMMENDATIONS = {"core", "recommended", "monitor", "exclude"}
PRIORITIES = {"must-read", "high", "medium", "low"}
REQUIRED = {"paper_id", "topic_id", "focus_version", *DIMENSIONS,
            "recommendation", "recommendation_reason", "chinese_summary",
            "key_methods", "key_findings", "direct_relevance", "limitations",
            "reading_priority"}


class EvaluationValidationError(ValueError):
    pass


def scoring_weights(focus: dict, mode: str) -> dict[str, float]:
    base_mode = "baseline" if mode in {"targeted", "deep-baseline", "citation-expand",
                                          "citation-reevaluation"} else mode
    if base_mode not in DEFAULT_WEIGHTS:
        raise EvaluationValidationError("unsupported evaluation mode")
    configured = focus.get("extensions", {}).get("strict_scoring", {}).get(base_mode)
    weights = dict(DEFAULT_WEIGHTS[base_mode] if configured is None else configured)
    if set(weights) != set(DIMENSIONS):
        raise EvaluationValidationError("scoring must define exactly six dimensions")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
           for value in weights.values()) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise EvaluationValidationError("scoring weights must be nonnegative and sum to 1")
    return {name: float(value) for name, value in weights.items()}


def calculate_score(item: dict, weights: dict[str, float]) -> float:
    return round(sum(float(item[name]) * weights[name] for name in DIMENSIONS), 2)


def validate_evaluation(item: object, *, candidate_ids: set[str],
                        topic_id: str, focus_version: str) -> dict:
    if not isinstance(item, dict):
        raise EvaluationValidationError("each evaluation must be an object")
    missing, extra = REQUIRED - set(item), set(item) - REQUIRED - {"overall_score"}
    if missing or extra:
        raise EvaluationValidationError(f"evaluation keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    if not isinstance(item["paper_id"], str) or item["paper_id"] not in candidate_ids:
        raise EvaluationValidationError("paper_id is not in this run")
    if item["topic_id"] != topic_id or item["focus_version"] != focus_version:
        raise EvaluationValidationError("Topic or Focus does not match this run")
    for name in DIMENSIONS:
        value = item[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise EvaluationValidationError(f"{name} must be a number from 0 to 100")
    if item["recommendation"] not in RECOMMENDATIONS or item["reading_priority"] not in PRIORITIES:
        raise EvaluationValidationError("invalid recommendation or reading priority")
    for name in ("recommendation_reason", "chinese_summary", "direct_relevance", "limitations"):
        if not isinstance(item[name], str):
            raise EvaluationValidationError(f"{name} must be a string")
    for name in ("key_methods", "key_findings"):
        if not isinstance(item[name], list) or any(not isinstance(value, str) for value in item[name]):
            raise EvaluationValidationError(f"{name} must be an array of strings")
    return {key: value for key, value in item.items() if key != "overall_score"}


def prepare_review(config_dir: Path, database: Path, run_dir: Path, *,
                   paper_ids: list[str], mode: str = "targeted",
                   lineage: dict | None = None,
                   mode_context: dict | None = None) -> dict:
    config_dir, database, run_dir = (ensure_private_path(path) for path in
                                     (config_dir, database, run_dir))
    bundle = load_bundle(config_dir)
    topic, focus = bundle["topic"], bundle["focus"]
    scoring_weights(focus, mode)
    if not paper_ids or len(paper_ids) != len(set(paper_ids)):
        raise EvaluationValidationError("select distinct existing Topic papers")
    known = {paper.paper_id: paper for paper in AcademicStore(database).topic_papers(topic["id"])}
    if any(paper_id not in known for paper_id in paper_ids):
        raise EvaluationValidationError("candidate does not belong to Topic")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise EvaluationValidationError("run directory must be new and empty")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidates, filtered = [], []
    for paper_id in paper_ids:
        paper = known[paper_id]
        candidate = {"paper_id": paper_id, "title": paper.title,
                           "authors": list(paper.authors), "abstract": paper.abstract,
                           "journal": paper.journal, "doi": paper.doi,
                           "publication_date": paper.publication_date,
                           "online_date": paper.online_date,
                           "is_retracted": any(item.normalized_metadata.get("is_retracted") is True
                                               for item in paper.source_records),
                           "sources": sorted({item.provider_id for item in paper.source_records})}
        reason = ("retracted" if candidate["is_retracted"] else
                  "insufficient-metadata" if not candidate["abstract"] and
                  not candidate["journal"] and not paper.identifiers else None)
        if reason:
            filtered.append({"paper_id": paper_id, "reason": reason})
        else:
            candidates.append(candidate)
    run_id = run_dir.name
    snapshot = {"schema_version": 1, "run_id": run_id, "mode": mode,
                "topic_id": topic["id"], "focus_version": focus["version"],
                "database_path": str(database),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "topic": topic, "focus": focus, "candidates": candidates,
                "filtered": filtered}
    if lineage is not None:
        snapshot["lineage"] = lineage
    if mode_context is not None:
        snapshot["mode_context"] = mode_context
    snapshot["sha256"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    request = {"run_id": run_id, "topic_id": topic["id"], "focus_version": focus["version"],
               "mode": mode, "dimensions": list(DIMENSIONS), "weights": scoring_weights(focus, mode),
               "candidate_count": len(candidates), "candidates": candidates,
               "filtered": filtered,
               "instructions": ["Evaluate each paper only from supplied metadata and abstract.",
                                "Do not invent findings or identifiers; do not claim full-text reading.",
                                "Return strict evaluation JSON with all required fields; Python computes overall_score."]}
    if lineage is not None:
        request["lineage"] = lineage
        request["instructions"].append(
            "Old evaluation scores are audit context only; score each paper anew from current metadata.")
    if mode_context is not None:
        request["mode_context"] = mode_context
    _write_once(run_dir / "snapshot.json", snapshot)
    _write_once(run_dir / "evaluation-request.json", request)
    return {"run_id": run_id, "topic_id": topic["id"], "candidate_count": len(candidates),
            "filtered_count": len(filtered),
            "request": str(run_dir / "evaluation-request.json"), "status": "AWAITING_HUMAN_EVALUATION"}


def import_review(run_dir: Path, evaluation_file: Path,
                  *, database: Path | None = None) -> dict:
    run_dir, evaluation_file = ensure_private_path(run_dir), ensure_private_path(evaluation_file)
    snapshot = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    payload = json.loads(evaluation_file.read_text(encoding="utf-8"))
    raw = payload if isinstance(payload, list) else payload.get("evaluations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise EvaluationValidationError("evaluations must be a JSON array")
    ids = {item["paper_id"] for item in snapshot["candidates"]}
    if len(raw) != len(ids):
        raise EvaluationValidationError("every candidate must be evaluated once")
    rows = [validate_evaluation(item, candidate_ids=ids,
                                topic_id=snapshot["topic_id"],
                                focus_version=snapshot["focus_version"]) for item in raw]
    if {item["paper_id"] for item in rows} != ids:
        raise EvaluationValidationError("duplicate or missing paper evaluation")
    weights = scoring_weights(snapshot["focus"], snapshot["mode"])
    ranked = sorted(({**item, "overall_score": calculate_score(item, weights)} for item in rows),
                    key=lambda item: (-item["overall_score"], item["paper_id"]))
    selected = [item["paper_id"] for item in ranked if
                item["recommendation"] in {"core", "recommended"} and item["overall_score"] >= 60]
    output = {"schema_version": 1, "run_id": snapshot["run_id"],
              "topic_id": snapshot["topic_id"], "focus_version": snapshot["focus_version"],
              "source_snapshot_sha256": snapshot["sha256"], "evaluations": ranked,
              "selected_paper_ids": selected, "status": "EVALUATED_AWAITING_FINAL_HUMAN_REVIEW",
              "knowledge_exported": False}
    if "lineage" in snapshot:
        output["lineage"] = snapshot["lineage"]
    changes = None
    if "lineage" in snapshot:
        old = snapshot["lineage"]["old_evaluations"]
        changes = [{"paper_id": row["paper_id"],
                    "old_score": old[row["paper_id"]]["overall_score"],
                    "new_score": row["overall_score"],
                    "score_change": round(row["overall_score"] -
                                          old[row["paper_id"]]["overall_score"], 2),
                    "old_recommendation": old[row["paper_id"]]["recommendation"],
                    "new_recommendation": row["recommendation"]}
                   for row in ranked]
    _write_once(run_dir / "evaluations.json", output)
    database_path = (ensure_private_path(database) if database is not None else
                     ensure_private_path(Path(snapshot["database_path"]))
                     if snapshot.get("database_path") else None)
    if database_path is not None:
        if not database_path.is_file():
            raise EvaluationValidationError("review database no longer exists")
        AcademicStore(database_path).commit_research_evaluations(snapshot, output, changes)
    if changes is not None:
        _write_once(run_dir / "score-changes.json", {
            "source_run_id": snapshot["lineage"]["source_run_id"],
            "reevaluation_run_id": snapshot["run_id"], "changes": changes})
    return {"run_id": snapshot["run_id"], "evaluated_count": len(ranked),
            "selected_count": len(selected), "status": output["status"]}


def prepare_reevaluation(source_run_dir: Path, config_dir: Path, database: Path,
                         new_run_dir: Path) -> dict:
    """Original metadata-only gate: new abstract or two significant fields."""
    source_run_dir = ensure_private_path(source_run_dir)
    old = json.loads((source_run_dir / "snapshot.json").read_text(encoding="utf-8"))
    evaluations = json.loads((source_run_dir / "evaluations.json").read_text(encoding="utf-8"))
    if old.get("mode") != "citation-expand" or evaluations.get("run_id") != old.get("run_id"):
        raise EvaluationValidationError("source is not a completed citation evaluation")
    if evaluations.get("source_snapshot_sha256") != old.get("sha256"):
        raise EvaluationValidationError("source evaluation snapshot differs")
    topic = load_bundle(ensure_private_path(config_dir))["topic"]["id"]
    if topic != old["topic_id"]:
        raise EvaluationValidationError("Topic differs from citation source")
    current = {paper.paper_id: paper for paper in AcademicStore(ensure_private_path(database)).topic_papers(topic)}
    previous = {item["paper_id"]: item for item in evaluations["evaluations"]}
    selected, changes = [], {}
    for candidate in old["candidates"]:
        paper_id = candidate["paper_id"]
        if candidate.get("abstract") or paper_id not in current or paper_id not in previous:
            continue
        paper = current[paper_id]
        now = {"title": paper.title, "authors": list(paper.authors),
               "abstract": paper.abstract, "journal": paper.journal,
               "publication_date": paper.publication_date,
               "online_date": paper.online_date, "doi": paper.doi}
        fields = [key for key in now if now[key] != candidate.get(key)]
        if now["abstract"] or len(fields) >= 2:
            selected.append(paper_id)
            changes[paper_id] = fields
    lineage = {"source_run_id": old["run_id"],
               "selection_rule": "old metadata-only AND (new abstract OR at least two significant fields)",
               "fields_changed": changes,
               "old_evaluations": {paper_id: previous[paper_id] for paper_id in selected}}
    if not selected:
        return {"source_run_id": old["run_id"], "status": "NO_REEVALUATION_CANDIDATES",
                "candidate_count": 0}
    result = prepare_review(config_dir, database, new_run_dir,
                            paper_ids=selected, mode="citation-reevaluation",
                            lineage=lineage)
    return {**result, "source_run_id": old["run_id"],
            "selection_rule": lineage["selection_rule"]}
