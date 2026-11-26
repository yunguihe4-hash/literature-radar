"""Append explicitly chosen local feedback with validated atomic replacement."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import SchemaValidationError, _read_json, validate_feedback_document

DECISIONS = frozenset({"keep", "must_read", "later", "discard"})


def load_feedback(path: Path) -> dict:
    """Read existing feedback without altering invalid data."""
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "entries": []}
    return validate_feedback_document(_read_json(path), path)


def add_feedback(path: Path, topic_id: str, paper_key: str, decision: str, reason: str) -> dict:
    path = Path(path)
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise SchemaValidationError("invalid feedback decision", file=path, field="feedback.entries.new.decision")
    payload = load_feedback(path)
    entry = {
        "topic_id": topic_id,
        "paper_key": paper_key,
        "decision": decision,
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = {"schema_version": payload["schema_version"], "entries": [*payload["entries"], entry]}
    validate_feedback_document(updated, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        validate_feedback_document(_read_json(Path(temporary)), Path(temporary))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return entry
