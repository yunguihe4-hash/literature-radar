"""Auditable, per-Topic Attention Memory updates from complete human feedback batches.

A batch is frozen at its first training attempt. Feedback and the existing proposal
are never edited. The transaction record is written before Memory, so a retry can
finish a partially committed batch without applying the evidence twice.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle, validate_bundle
from literature_radar.feedback.batches import BATCH_SIZE, batch_progress, prepare_due_batches, is_human_feedback
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import DECISIONS, load_feedback
from literature_radar.private_paths import ensure_private_path


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_once(path: Path, value: dict) -> None:
    if path.exists():
        if _read(path) != value:
            raise ValueError(f"Immutable Attention artifact differs: {path}")
    else:
        _atomic_json(path, value)


def _latest(config_dir: Path, topic: str) -> dict[str, dict]:
    latest = {}
    for entry in load_feedback(config_dir / "feedback.json")["entries"]:
        if entry["topic_id"] == topic and is_human_feedback(entry):
            latest[entry["paper_key"]] = entry
    return latest


def _card_features(config_dir: Path, topic: str, keys: set[str], bundle: dict) -> dict[str, dict]:
    """Read stable card identity and explicit research-question IDs, if available."""
    binding = config_dir.parent / "obsidian.json"
    if not binding.is_file():
        return {}
    root = Path(_read(binding).get("delivery_root", ""))
    if not root.is_dir():
        return {}
    known = {item["id"] for section in ("questions", "methods", "applications")
             for item in bundle["profile"][section]}
    found = {}
    for section in ("Bootstrap", "Weekly"):
        for path in (root / section / topic).glob("*/*.md"):
            text = path.read_text(encoding="utf-8")
            meta = re.search(r'(?m)^paper_key:\s*["\']?(.+?)["\']?\s*$', text)
            if not meta:
                continue
            key = meta.group(1).strip().strip('"\'')
            if key not in keys or key in found:
                continue
            match = re.search(r"(?m)^## 研究问题\s*\n\s*([^\n]+)", text)
            identifiers = sorted(set(re.findall(r"[a-z][a-z0-9-]+", match.group(1))) & known) if match else []
            found[key] = {"path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "identifiers": identifiers}
    return found


def _feedback_features(entry: dict, bundle: dict, card: dict | None) -> list[str]:
    known = {item["id"] for section in ("questions", "methods", "applications")
             for item in bundle["profile"][section]}
    values = entry.get("extensions", {})
    found = set(card.get("identifiers", []) if card else [])
    for field in ("important_question", "important_method"):
        value = values.get(field)
        if isinstance(value, str):
            for section in ("questions", "methods"):
                for item in bundle["profile"][section]:
                    if value.casefold() in {item["id"].casefold(), item["label"].casefold(),
                                             *(alias.casefold() for alias in item.get("aliases", []))}:
                        found.add(item["id"])
    return sorted(found & known)


def _candidate_memory(before: dict, entries: list[dict], cards: dict[str, dict], bundle: dict,
                      batch_id: str) -> tuple[dict, dict]:
    after = copy.deepcopy(before)
    ext = after.setdefault("extensions", {})
    training = copy.deepcopy(ext.get("feedback_training", {}))
    votes = copy.deepcopy(training.get("identifier_votes", {}))
    totals = Counter(training.get("decision_totals", {}))
    evidence = {}
    for entry in entries:
        decision = entry["decision"]
        totals[decision] += 1
        ids = _feedback_features(entry, bundle, cards.get(entry["paper_key"]))
        evidence[entry["paper_key"]] = {"decision": decision, "identifiers": ids,
                                        "card": cards.get(entry["paper_key"])}
        polarity = "positive" if decision in ("must_read", "keep") else "negative"
        for identifier in ids:
            pair = votes.setdefault(identifier, {"positive": 0, "negative": 0})
            pair[polarity] += 1
    old_auto = set(training.get("learned_identifiers", []))
    new_auto = {identifier for identifier, pair in votes.items()
                if pair["positive"] >= 2 and pair["positive"] - pair["negative"] >= 2}
    manual = set(before["preferred_concept_ids"]) - old_auto
    after["preferred_concept_ids"] = sorted(manual | new_auto)
    training.update({"last_batch_id": batch_id,
                     "processed_feedback": int(training.get("processed_feedback", 0)) + BATCH_SIZE,
                     "decision_totals": dict(sorted(totals.items())),
                     "identifier_votes": dict(sorted(votes.items())),
                     "learned_identifiers": sorted(new_auto)})
    ext["feedback_training"] = training
    validate_bundle({**bundle, "attention-memory": after})
    return after, evidence


def _batch_transaction(config_dir: Path, batch: dict, latest: dict[str, dict], bundle: dict) -> dict:
    topic = bundle["topic"]["id"]
    number = batch["number"]
    version_dir = config_dir / "attention-versions"
    record_path = version_dir / f"batch-{number:03d}-record.json"
    memory_path = config_dir / "attention-memory.json"
    before_path = version_dir / f"batch-{number:03d}-before.json"
    after_path = version_dir / f"batch-{number:03d}-after.json"
    before = _read(memory_path)
    if record_path.exists():
        record = _read(record_path)
        if record.get("batch_id") != batch["batch_id"] or record.get("topic_id") != topic:
            raise ValueError("Existing training record has a different Topic or batch")
        expected_before, expected_after = _read(before_path), _read(after_path)
        if _digest(expected_before) != record["memory_before_sha256"] or _digest(expected_after) != record["memory_after_sha256"]:
            raise ValueError("Attention version snapshot failed integrity check")
        if _digest(before) not in (record["memory_before_sha256"], record["memory_after_sha256"]):
            raise ValueError("Memory changed since interrupted training; refusing overwrite")
        after = expected_after
    else:
        proposal = _read(Path(batch["proposal"]))
        keys = proposal.get("paper_keys", [])
        if (proposal.get("topic_id") != topic or proposal.get("batch_id") != batch["batch_id"] or
                len(keys) != BATCH_SIZE or len(set(keys)) != BATCH_SIZE):
            raise ValueError("Batch proposal identity or feedback count is invalid")
        if any(key not in latest for key in keys):
            raise ValueError("Batch feedback missing; preserving existing Memory")
        entries = [copy.deepcopy(latest[key]) for key in keys]
        if any(entry["topic_id"] != topic or entry["decision"] not in DECISIONS for entry in entries):
            raise ValueError("Batch includes invalid or foreign Feedback")
        cards = _card_features(config_dir, topic, set(keys), bundle)
        after, evidence = _candidate_memory(before, entries, cards, bundle, batch["batch_id"])
        version = f"{topic}:{number:03d}:{_digest(after)[:12]}"
        record = {"schema_version": 1, "topic_id": topic, "batch_id": batch["batch_id"],
                  "hash_method": "sha256 of canonical UTF-8 JSON (sorted keys, compact separators)",
                  "batch_number": number, "feedback_start": (number - 1) * BATCH_SIZE + 1,
                  "feedback_end": number * BATCH_SIZE, "feedback_count": BATCH_SIZE,
                  "feedback_first_at": min(entry["recorded_at"] for entry in entries),
                  "feedback_last_at": max(entry["recorded_at"] for entry in entries),
                  "trained_at": datetime.now(timezone.utc).isoformat(),
                  "feedback": entries, "feedback_sha256": _digest({"entries": entries}),
                  "feature_evidence": evidence,
                  "memory_before_sha256": _digest(before), "memory_after_sha256": _digest(after),
                  "memory_before_path": str(before_path), "memory_after_path": str(after_path),
                  "memory_diff": {"preferred_concept_ids_before": before["preferred_concept_ids"],
                                  "preferred_concept_ids_after": after["preferred_concept_ids"],
                                  "feedback_training_before": before.get("extensions", {}).get("feedback_training"),
                                  "feedback_training_after": after["extensions"]["feedback_training"]},
                  "training_result": "UPDATED_PREFERENCES" if before["preferred_concept_ids"] != after["preferred_concept_ids"]
                                     else "STATISTICS_ONLY",
                  "training_version": version,
                  "rollback_basis": {"restore": str(before_path), "requires_current_sha256": _digest(after)}}
        _write_once(before_path, before)
        _write_once(after_path, after)
        _write_once(record_path, record)
    if _digest(before) == record["memory_before_sha256"]:
        _atomic_json(memory_path, after)
        # A failed validation is a real failure. The original snapshot remains available.
        load_bundle(config_dir)
    return record


def train_due_batches(config_dir: Path) -> dict:
    """Apply all complete 30-decision batches for one Topic, oldest first.

    A failure raises to the caller; Weekly may continue with the last valid Memory.
    Retrying the call safely finishes an interrupted transaction.
    """
    config_dir = ensure_private_path(config_dir)
    bundle = load_bundle(config_dir)
    topic = bundle["topic"]["id"]
    progress = prepare_due_batches(config_dir)
    if not progress["pending_batches"]:
        return {**progress, "trained": []}
    latest = _latest(config_dir, topic)
    trained = []
    state_path = config_dir / "attention-batch-state.json"
    for batch in progress["batches"]:
        if batch["status"] == "COMPLETED":
            continue
        if batch["status"] == "ROLLED_BACK":
            break
        if batch["status"] != "AWAITING_HUMAN_REVIEW":
            raise ValueError(f"Unsupported Attention batch status: {batch['status']}")
        record = _batch_transaction(config_dir, batch, latest, bundle)
        state = _read(state_path)
        current = state["batches"][batch["number"] - 1]
        if current["batch_id"] != record["batch_id"]:
            raise ValueError("Attention batch state changed during training")
        current.update({"status": "COMPLETED", "training_version": record["training_version"],
                        "completed_at": record["trained_at"], "feedback_start": record["feedback_start"],
                        "feedback_end": record["feedback_end"], "feedback_count": BATCH_SIZE,
                        "record": str(config_dir / "attention-versions" / f"batch-{batch['number']:03d}-record.json"),
                        "memory_before_sha256": record["memory_before_sha256"],
                        "memory_after_sha256": record["memory_after_sha256"]})
        _atomic_json(state_path, state)
        trained.append(record["batch_id"])
        bundle = load_bundle(config_dir)
    return {**batch_progress(config_dir), "trained": trained}


def rollback_last_batch(config_dir: Path) -> dict:
    """Restore the snapshot before the most recent completed batch, with a hash guard."""
    config_dir = ensure_private_path(config_dir)
    progress = batch_progress(config_dir)
    completed = [batch for batch in progress["batches"] if batch["status"] == "COMPLETED"]
    if not completed or completed[-1]["number"] != len(progress["batches"]):
        raise ValueError("Only the latest completed batch can be rolled back")
    batch = completed[-1]
    record = _read(Path(batch["record"]))
    memory_path = config_dir / "attention-memory.json"
    current = _read(memory_path)
    if _digest(current) != record["memory_after_sha256"]:
        raise ValueError("Current Memory differs from batch result; refusing rollback")
    before = _read(Path(record["memory_before_path"]))
    if _digest(before) != record["memory_before_sha256"]:
        raise ValueError("Rollback snapshot failed integrity check")
    _atomic_json(memory_path, before)
    state_path = config_dir / "attention-batch-state.json"
    state = _read(state_path)
    state["batches"][batch["number"] - 1].update(status="ROLLED_BACK",
                                                    rolled_back_at=datetime.now(timezone.utc).isoformat())
    _atomic_json(state_path, state)
    return {"topic_id": progress["topic_id"], "batch_id": batch["batch_id"],
            "restored_sha256": record["memory_before_sha256"]}
