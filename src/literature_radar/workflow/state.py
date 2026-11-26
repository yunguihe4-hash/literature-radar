"""Small, explicit Topic lifecycle file; no automatic scheduler activation."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


STATES = ("TOPIC_INITIALIZED", "BOOTSTRAP_PENDING", "BOOTSTRAP_RUNNING",
          "BOOTSTRAP_REVIEW_READY", "FEEDBACK_IMPORTED", "MEMORY_REVIEW",
          "WEEKLY_MONITORING")


def read_state(config_dir: Path) -> dict:
    path = Path(config_dir) / "workflow-state.json"
    if not path.exists():
        topic_id = json.loads((Path(config_dir) / "topic.json").read_text())["id"]
        return {"schema_version": 1, "topic_id": topic_id,
                "state": "BOOTSTRAP_PENDING", "history": ["TOPIC_INITIALIZED", "BOOTSTRAP_PENDING"]}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("state") not in STATES:
        raise ValueError("Invalid Topic workflow state")
    return value


def write_state(config_dir: Path, value: dict) -> None:
    if value.get("state") not in STATES:
        raise ValueError("Unknown Topic workflow state")
    path = Path(config_dir) / "workflow-state.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".workflow-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def advance(config_dir: Path, target: str, **details) -> dict:
    value = read_state(config_dir)
    if target not in STATES:
        raise ValueError("Unknown Topic workflow state")
    if value["state"] != target:
        value["history"] = [*value.get("history", []), target]
    value["state"] = target
    value.update(details)
    write_state(config_dir, value)
    return value
