"""Topic-local adapter for the original AI_OS 20-feedback ridge Trainer."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.attention.legacy_trainer_math import (
    CLASS_THRESHOLDS, FEATURE_NAMES, LABEL_VALUE, MIN_NEW_FEEDBACK, POSITIVE,
    RIDGE, baseline_score, candidate_predictions, evaluation_metrics, fit, gate,
    predict, score_to_attention,
)
from literature_radar.config import load_bundle
from literature_radar.feedback.batches import is_human_feedback
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback

LABELS = {"must_read": "important", "keep": "useful",
          "later": "neutral", "discard": "irrelevant"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _root(config_dir: Path) -> Path:
    return Path(config_dir) / "attention-calibration"


def record_evidence(config_dir: Path, run_id: str, rows: list[dict]) -> int:
    """Freeze the seven original rank fields before human feedback arrives."""
    root = _root(config_dir) / "evidence" / run_id
    count = 0
    for row in rows:
        rank = row.get("attention_rank")
        if not rank or set(rank) != set(FEATURE_NAMES):
            continue
        key = row["paper_key"]
        path = root / (_hash(key) + ".json")
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("paper_key") != key or previous.get("topic_id") != row["topic_id"]:
                raise ValueError("Attention evidence identity conflict")
            continue
        _atomic_json(path, {"schema_version": 1, "topic_id": row["topic_id"],
                            "run_id": run_id, "paper_key": key,
                            "attention_rank": rank,
                            "intrinsic_attention": row["intrinsic_attention"]})
        count += 1
    return count


def _evidence(config_dir: Path, key: str, preferred_run: str | None) -> dict | None:
    root = _root(config_dir) / "evidence"
    filename = _hash(key) + ".json"
    if preferred_run:
        path = root / preferred_run / filename
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if value.get("paper_key") == key else None
    for path in sorted(root.glob("*/" + filename)):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("paper_key") == key:
            return value
    return None


def _vector(rank: dict) -> list[float]:
    if set(rank) != set(FEATURE_NAMES):
        raise ValueError("Attention feature schema mismatch")
    values = []
    for name in FEATURE_NAMES:
        value = rank[name]
        if name == "keyword_evidence":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("keyword_evidence must be numeric")
            values.append(float(value) / 100.0)
        else:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            values.append(float(value))
    return values


def calibrated_attention(config_dir: Path, original: dict) -> dict:
    """An active, validated model may change final Attention; intrinsic stays frozen."""
    path = _root(config_dir) / "active.json"
    if not path.exists():
        return original
    active = json.loads(path.read_text(encoding="utf-8"))
    if (active.get("topic_id") != load_bundle(config_dir)["topic"]["id"] or
            active.get("feature_names") != list(FEATURE_NAMES) or
            active.get("thresholds") != CLASS_THRESHOLDS or
            not isinstance(active.get("weights"), list) or
            len(active["weights"]) != len(FEATURE_NAMES)):
        raise ValueError("Active Attention calibration is invalid")
    score = predict(_vector(original["attention_rank"]), float(active["intercept"]),
                    [float(weight) for weight in active["weights"]])
    level = score_to_attention(score, active["thresholds"])
    return {**original, "calibration_score": score,
            "calibrated_attention": level, "final_attention": level,
            "topic_calibration_version": active["active_version"]}


def train_due(config_dir: Path, *, observed_at: str | None = None,
              minimum_new_feedback: int = MIN_NEW_FEEDBACK) -> dict:
    """Use the original AI_OS 20-new-feedback trigger per Topic."""
    if minimum_new_feedback < MIN_NEW_FEEDBACK:
        raise ValueError("Training trigger cannot be below the original 20-feedback gate")
    config_dir = Path(config_dir)
    topic = load_bundle(config_dir)["topic"]["id"]
    root = _root(config_dir)
    state_path = root / "trainer-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "schema_version": 1, "topic_id": topic, "last_training_attempt_feedback_count": 0,
        "last_activated_feedback_count": 0, "active_version": None,
        "last_training_status": "NEVER_TRAINED"}
    if state.get("topic_id") != topic:
        raise ValueError("Trainer state belongs to another Topic")
    latest = {}
    for entry in load_feedback(config_dir / "feedback.json")["entries"]:
        if entry["topic_id"] == topic and is_human_feedback(entry):
            latest[entry["paper_key"]] = entry
    entries = sorted(latest.values(), key=lambda entry: entry["paper_key"])
    current = len(entries)
    prior_count = int(state["last_training_attempt_feedback_count"])
    pending = max(0, current - prior_count)
    state.update(reviewed_count=current, trainable_relevance_count=current,
                 pending_trainable_feedback=pending)
    result = {"topic_id": topic, "trainable_feedback": current,
              "pending": pending, "eligible": pending >= minimum_new_feedback,
              "status": "NOT_ELIGIBLE", "active_version": state.get("active_version")}
    if pending < minimum_new_feedback:
        _atomic_json(state_path, state)
        return result
    labels = [LABELS[entry["decision"]] for entry in entries]
    if len(set(labels)) < 2 or sum(label in POSITIVE for label in labels) < 2 or sum(
            label not in POSITIVE for label in labels) < 2:
        state.update(last_training_attempt_feedback_count=current,
                     pending_trainable_feedback=0,
                     last_training_status="INSUFFICIENT_LABEL_DIVERSITY")
        _atomic_json(state_path, state)
        return {**result, "status": "INSUFFICIENT_LABEL_DIVERSITY"}
    dataset, errors = [], []
    for entry, label in zip(entries, labels):
        key = entry["paper_key"]
        evidence = _evidence(config_dir, key, entry.get("extensions", {}).get("source_run_id"))
        if evidence is None:
            errors.append({"paper_key": key, "error": "missing frozen Attention evidence"})
            continue
        try:
            vector = _vector(evidence["attention_rank"])
        except (KeyError, ValueError) as exc:
            errors.append({"paper_key": key, "error": str(exc)})
            continue
        dataset.append({"paper_key": key, "feedback": label,
                        "source_run_id": evidence["run_id"],
                        "feature_snapshot": evidence,
                        "feature_hash": _hash(json.dumps(evidence, sort_keys=True)),
                        "vector": vector})
    if errors:
        state["last_training_status"] = "DATA_NOT_REPLAYABLE"
        _atomic_json(state_path, state)
        return {**result, "status": "DATA_NOT_REPLAYABLE", "feature_errors": errors}
    vectors = [item["vector"] for item in dataset]
    targets = [LABEL_VALUE[label] for label in labels]
    old_active = root / "active.json"
    if old_active.exists():
        active = json.loads(old_active.read_text(encoding="utf-8"))
        if active.get("feature_names") != list(FEATURE_NAMES) or active.get("thresholds") != CLASS_THRESHOLDS:
            raise ValueError("Active Trainer schema mismatch")
        old_scores = [predict(vector, float(active["intercept"]),
                              [float(weight) for weight in active["weights"]]) for vector in vectors]
        old_classes = [score_to_attention(score) for score in old_scores]
    else:
        old_scores = [baseline_score(vector) for vector in vectors]
        old_classes = [item["feature_snapshot"]["intrinsic_attention"] for item in dataset]
    versions = root / "versions"
    number = 1 + max((int(path.stem[1:]) for path in versions.glob("v[0-9][0-9][0-9].json")), default=0)
    version = f"v{number:03d}"
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    try:
        predicted = candidate_predictions(vectors, targets)
        candidate_metrics = evaluation_metrics(predicted,
                                               [score_to_attention(score) for score in predicted], labels)
        old_metrics = evaluation_metrics(old_scores, old_classes, labels)
        intercept, weights = fit(vectors, targets)
        activated, reasons = gate(candidate_metrics, old_metrics)
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        state.update(last_training_attempt_feedback_count=current,
                     pending_trainable_feedback=0, last_training_status="TRAINED_NOT_ACTIVATED",
                     last_training_error=str(exc))
        _atomic_json(state_path, state)
        return {**result, "status": "TRAINED_NOT_ACTIVATED", "error": str(exc)}
    status = "ACTIVATED" if activated else "REJECTED"
    manifest = [{key: item[key] for key in ("paper_key", "feedback", "source_run_id",
                                          "feature_snapshot", "feature_hash")} for item in dataset]
    artifact = {"schema_version": 1, "topic_id": topic, "version": version,
                "created_at": observed, "status": status,
                "algorithm": "ridge_ordinal_calibration_v1", "ridge": RIDGE,
                "feature_names": list(FEATURE_NAMES), "intercept": intercept,
                "weights": weights, "thresholds": CLASS_THRESHOLDS,
                "training_feedback_count": current, "training_dataset_manifest": manifest,
                "training_dataset_hash": _hash(json.dumps(manifest, sort_keys=True)),
                "previous_active_version": state.get("active_version")}
    evaluation = {"schema_version": 1, "topic_id": topic, "version": version,
                  "evaluation_method": "leave_one_out", "candidate": candidate_metrics,
                  "active_or_builtin": old_metrics, "gate_passed": activated,
                  "gate_reasons": reasons}
    _atomic_json(versions / f"{version}.json", artifact)
    _atomic_json(versions / f"{version}-evaluation.json", evaluation)
    if activated:
        _atomic_json(root / "active.json", {
            "schema_version": 1, "topic_id": topic, "active_version": version,
            "activated_at": observed, "feature_names": list(FEATURE_NAMES),
            "intercept": intercept, "weights": weights, "thresholds": CLASS_THRESHOLDS})
    state.update(last_training_attempt_feedback_count=current,
                 last_activated_feedback_count=current if activated else state["last_activated_feedback_count"],
                 pending_trainable_feedback=0, active_version=version if activated else state.get("active_version"),
                 last_training_at=observed, last_training_status=status,
                 last_training_error=None if activated else "; ".join(reasons))
    _atomic_json(state_path, state)
    result.update(status=status, version=version, active_version=state.get("active_version"),
                  evaluation=evaluation, dataset_size=len(dataset))
    return result


def training_status(config_dir: Path) -> dict:
    root = _root(config_dir)
    path = root / "trainer-state.json"
    if not path.is_file():
        return {"status": "NEVER_TRAINED", "active_version": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("topic_id") != load_bundle(config_dir)["topic"]["id"]:
        raise ValueError("Trainer state belongs to another Topic")
    return state


def training_history(config_dir: Path) -> dict:
    topic = load_bundle(config_dir)["topic"]["id"]
    versions = []
    for path in sorted((_root(config_dir) / "versions").glob("v[0-9][0-9][0-9].json")):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        evaluation_path = path.with_name(path.stem + "-evaluation.json")
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        if artifact.get("topic_id") != topic or evaluation.get("topic_id") != topic:
            raise ValueError("Training History contains another Topic")
        versions.append({"version": artifact["version"], "status": artifact["status"],
                         "created_at": artifact["created_at"],
                         "training_feedback_count": artifact["training_feedback_count"],
                         "gate_passed": evaluation["gate_passed"],
                         "gate_reasons": evaluation["gate_reasons"]})
    return {"topic_id": topic, "active_version": training_status(config_dir)["active_version"],
            "versions": versions}


def rollback_calibration(config_dir: Path, version: str) -> dict:
    topic = load_bundle(config_dir)["topic"]["id"]
    if not version.startswith("v") or not version[1:].isdigit():
        raise ValueError("Invalid calibration version")
    root = _root(config_dir)
    artifact_path = root / "versions" / f"{version}.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if (artifact.get("topic_id") != topic or artifact.get("feature_names") != list(FEATURE_NAMES)
            or artifact.get("thresholds") != CLASS_THRESHOLDS or
            not isinstance(artifact.get("weights"), list) or
            len(artifact["weights"]) != len(FEATURE_NAMES)):
        raise ValueError("Rollback artifact schema mismatch")
    now = datetime.now(timezone.utc).isoformat()
    active = {"schema_version": 1, "topic_id": topic, "active_version": version,
              "activated_at": now, "rollback": True,
              "feature_names": list(FEATURE_NAMES),
              "intercept": artifact["intercept"], "weights": artifact["weights"],
              "thresholds": CLASS_THRESHOLDS}
    _atomic_json(root / "active.json", active)
    state = training_status(config_dir)
    state.update(active_version=version, last_training_at=now,
                 last_training_status="ROLLED_BACK")
    _atomic_json(root / "trainer-state.json", state)
    return active
