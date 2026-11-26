"""Per-Topic, human-reviewed Attention calibration every 30 distinct decisions."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.private_paths import ensure_private_path

BATCH_SIZE = 30


def is_human_feedback(entry: dict) -> bool:
    """Exclude discovery and bootstrap bookkeeping from the training ledger."""
    if entry.get("decision") not in {"must_read", "keep", "later", "discard"}:
        return False
    source = entry.get("extensions", {}).get("source", "")
    return source not in {"automatic", "bootstrap", "discovery", "weekly", "system"}


def _latest(config_dir: Path) -> dict[str, dict]:
    topic = load_bundle(config_dir)["topic"]["id"]
    latest = {}
    for entry in load_feedback(config_dir / "feedback.json")["entries"]:
        if entry["topic_id"] == topic and is_human_feedback(entry):
            latest[entry["paper_key"]] = entry
    return latest


def _first_feedback_times(config_dir: Path, topic: str) -> dict[str, datetime]:
    first = {}
    for entry in load_feedback(config_dir / "feedback.json")["entries"]:
        if entry["topic_id"] == topic and is_human_feedback(entry):
            key = entry["paper_key"]
            when = datetime.fromisoformat(entry["recorded_at"].replace("Z", "+00:00"))
            first[key] = min(first.get(key, when), when)
    return first


def batch_progress(config_dir: Path) -> dict:
    config_dir = ensure_private_path(config_dir)
    topic = load_bundle(config_dir)["topic"]["id"]
    reviewed = len(_latest(config_dir))
    state_path = config_dir / "attention-batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"batches": []}
    batches = state.get("batches", [])
    if state.get("topic_id", topic) != topic or not isinstance(batches, list):
        raise ValueError("Attention batch state belongs to another Topic or is invalid")
    normalized = []
    for number, batch in enumerate(batches, 1):
        if not isinstance(batch, dict) or batch.get("number") != number or batch.get("threshold") != number * BATCH_SIZE:
            raise ValueError("Attention batches are not contiguous 30-paper batches")
        expected_id = f"{topic}:{number:03d}"
        if batch.get("batch_id", expected_id) != expected_id:
            raise ValueError("Attention batch ID conflicts with Topic or number")
        normalized.append({**batch, "batch_id": expected_id})
    batches = normalized
    completed = [batch for batch in batches if batch.get("status") == "COMPLETED" and
                 batch.get("training_version") and batch.get("completed_at")]
    processed = len(completed) * BATCH_SIZE
    return {"topic_id": topic, "valid_feedback": reviewed,
            "prepared_batches": len(batches), "completed_batches": len(completed),
            "pending_batches": len(batches) - len(completed),
            "processed_feedback": processed,
            "unprocessed_feedback": max(0, reviewed - processed),
            "last_calibration_time": completed[-1]["completed_at"] if completed else None,
            "next_threshold": (len(completed) + 1) * BATCH_SIZE,
            "remainder": reviewed % BATCH_SIZE, "batches": batches}


def prepare_due_batches(config_dir: Path, *, cards_root: Path | None = None) -> dict:
    """Prepare evidence-based proposals; never apply Memory or Attention rules."""
    config_dir = ensure_private_path(config_dir)
    progress = batch_progress(config_dir)
    topic = progress["topic_id"]
    latest = _latest(config_dir)
    first = _first_feedback_times(config_dir, topic)
    ordered = sorted(latest.values(), key=lambda item: (first[item["paper_key"]], item["paper_key"]))
    levels = {}
    if cards_root is not None and Path(cards_root).is_dir():
        from literature_radar.delivery.cards import frontmatter
        for path in Path(cards_root).glob("*/*.md"):
            meta = frontmatter(path.read_text(encoding="utf-8"))
            if meta.get("topic_id") == topic:
                levels[meta.get("paper_key")] = meta.get("current_attention")
    batches = list(progress["batches"])
    created = []
    claimed = set()
    for batch in batches:
        proposal = json.loads(Path(batch["proposal"]).read_text(encoding="utf-8"))
        keys = proposal.get("paper_keys", [])
        if (proposal.get("topic_id") != topic or proposal.get("batch_id") != batch["batch_id"] or
                len(keys) != BATCH_SIZE or len(set(keys)) != BATCH_SIZE or claimed.intersection(keys)):
            raise ValueError("Existing Attention batch proposal is invalid or overlaps another batch")
        claimed.update(keys)
    unclaimed = [item for item in ordered if item["paper_key"] not in claimed]
    for number in range(len(batches) + 1, len(ordered) // BATCH_SIZE + 1):
        group = unclaimed[:BATCH_SIZE]
        if len(group) != BATCH_SIZE:
            raise ValueError("Existing Attention proposals claim current Feedback inconsistently")
        unclaimed = unclaimed[BATCH_SIZE:]
        distribution = Counter(item["decision"] for item in group)
        by_attention = {level: Counter(item["decision"] for item in group
                                       if levels.get(item["paper_key"]) == level)
                        for level in ("HIGH", "MEDIUM", "LOW")}
        batch_id = f"{topic}:{number:03d}"
        proposal = {"schema_version": 1, "topic_id": topic, "batch_id": batch_id, "batch_number": number,
                    "threshold": number * BATCH_SIZE, "status": "AWAITING_HUMAN_REVIEW",
                    "paper_keys": [item["paper_key"] for item in group],
                    "decisions": dict(distribution),
                    "attention_decisions": {level: dict(counts) for level, counts in by_attention.items()},
                    "automatic_memory_update": False,
                    "review_note": "根据实际人工反馈审查 Attention；确认前不修改正式规则或 Memory。"}
        path = config_dir / f"attention-batch-{number:03d}.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != proposal:
                raise ValueError("Existing Attention batch proposal differs; refusing overwrite")
        else:
            _atomic_json(path, proposal)
        batches.append({"number": number, "threshold": number * BATCH_SIZE,
                        "batch_id": batch_id,
                        "status": proposal["status"], "proposal": str(path)})
        created.append(str(path))
    if created:
        _atomic_json(config_dir / "attention-batch-state.json",
                     {"schema_version": 1, "topic_id": topic, "batches": batches})
    return {**batch_progress(config_dir), "created_proposals": created}
