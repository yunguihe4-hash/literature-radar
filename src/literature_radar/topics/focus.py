"""Immutable Focus snapshots for run provenance and explicit Topic revisions."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.feedback.review import _atomic_json
from literature_radar.private_paths import ensure_private_path


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_once(path: Path, value: dict) -> None:
    payload = _canonical(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Immutable Focus version already differs")
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def set_focus(config_dir: Path, search_terms: list[str], *, reason: str) -> dict:
    config_dir = ensure_private_path(config_dir)
    if not reason.strip() or not search_terms or any(not term.strip() for term in search_terms):
        raise ValueError("Focus requires search terms and a revision reason")
    bundle = load_bundle(config_dir)
    current = bundle["focus"]
    if not re.fullmatch(r"v\d{3,}", current["version"]):
        raise ValueError("Invalid current Focus version")
    previous = config_dir / "focuses" / f"{current['version']}.json"
    _write_once(previous, current)
    number = max([int(current["version"][1:]), *(
        int(path.stem[1:]) for path in previous.parent.glob("v*.json")
        if re.fullmatch(r"v\d{3,}", path.stem))]) + 1
    version = f"v{number:03d}"
    updated = {**current, "version": version,
               "search_terms": list(dict.fromkeys(term.strip() for term in search_terms)),
               "created_at": datetime.now(timezone.utc).isoformat(),
               "extensions": {**current.get("extensions", {}), "revision_reason": reason.strip()}}
    _write_once(previous.parent / f"{version}.json", updated)
    _atomic_json(config_dir / "focus.json", updated)
    load_bundle(config_dir)
    return {"topic_id": bundle["topic"]["id"], "version": version,
            "sha256": hashlib.sha256(_canonical(updated)).hexdigest()}


def focus_history(config_dir: Path) -> list[dict]:
    config_dir = ensure_private_path(config_dir)
    current = load_bundle(config_dir)["focus"]
    versions = {current["version"]: current}
    for path in (config_dir / "focuses").glob("v*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != path.stem or value.get("topic_id") != current["topic_id"]:
            raise ValueError("Focus history identity mismatch")
        if path.stem in versions and versions[path.stem] != value:
            raise ValueError("Current Focus differs from immutable snapshot")
        versions[path.stem] = value
    return [{"version": version, "sha256": hashlib.sha256(_canonical(value)).hexdigest(),
             "current": version == current["version"]}
            for version, value in sorted(versions.items())]
