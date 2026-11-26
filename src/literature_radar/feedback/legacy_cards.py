"""Read old AI_OS checkbox cards into the current per-Topic Feedback ledger.

This compatibility collector never edits a Card. The Bridge remains the source
for cards in the new layout; this scans only the old ``Literature`` subtree.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle, validate_feedback_document
from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.cards import card_key, frontmatter
from literature_radar.delivery.review_ui import LEGACY
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.private_paths import ensure_private_path

MARKER = re.compile(r"(?m)^\s*-\s*\[([^\]]*)\].*?<!--\s*feedback:([a-z_]+)\s*-->", re.I)
PRIMARY = frozenset(LEGACY)


def collect_legacy_cards(config_dir: Path, database: Path, vault_root: Path,
                         *, observed_at: str | None = None, dry_run: bool = False) -> dict:
    config_dir = ensure_private_path(config_dir)
    database = ensure_private_path(database)
    topic = load_bundle(config_dir)["topic"]["id"]
    vault_root = Path(vault_root).resolve()
    root = vault_root / "Literature"
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    datetime.fromisoformat(observed.replace("Z", "+00:00"))
    papers = AcademicStore(database).topic_papers(topic) if database.is_file() else []
    by_key = {}
    for paper in papers:
        doi = paper.doi or paper.identifiers.get("doi")
        key = card_key(topic, paper.paper_id, doi)
        by_key[key] = key
    feedback_file = config_dir / "feedback.json"
    ledger = load_feedback(feedback_file)
    if any(entry["topic_id"] != topic for entry in ledger["entries"]):
        raise ValueError("Feedback ledger contains another Topic")
    latest = {entry["paper_key"]: entry for entry in ledger["entries"]}
    additions, seen = [], set()
    counts = {"reviewed": 0, "unreviewed": 0, "conflicts": 0,
              "unmapped": 0, "duplicates": 0}
    if root.is_dir():
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            text = path.read_text(encoding="utf-8")
            try:
                meta = frontmatter(text)
            except ValueError:
                continue
            if meta.get("type") != "literature-review-card" or meta.get("topic_id", meta.get("topic")) != topic:
                continue
            old_key = meta.get("paper_key")
            if not old_key:
                raise ValueError(f"Legacy review Card lacks paper_key: {path}")
            if old_key in seen:
                raise ValueError(f"Duplicate legacy Card identity: {old_key}")
            seen.add(old_key)
            selected = list(dict.fromkeys(marker.casefold() for check, marker in MARKER.findall(text)
                                          if check.strip().casefold() == "x" and marker.casefold() in PRIMARY))
            if not selected:
                counts["unreviewed"] += 1
                continue
            if len(selected) > 1:
                counts["conflicts"] += 1
                continue
            counts["reviewed"] += 1
            key = by_key.get(old_key)
            if key is None and meta.get("doi"):
                normalized = str(meta["doi"]).casefold().removeprefix("https://doi.org/")
                key = by_key.get(card_key(topic, "", normalized))
            if key is None:
                counts["unmapped"] += 1
                continue
            decision = LEGACY[selected[0]]
            previous = latest.get(key)
            if previous is not None and previous["decision"] == decision:
                counts["duplicates"] += 1
                continue
            item = {"topic_id": topic, "paper_key": key, "decision": decision,
                    "reason": "Imported from an AI_OS review Card checkbox",
                    "recorded_at": observed,
                    "extensions": {"source": "ai_os_card", "legacy_marker": selected[0],
                                   "legacy_paper_key": old_key,
                                   "source_run_id": meta.get("source_run") or meta.get("source_run_id") or "",
                                   "card_path": str(path.relative_to(vault_root))}}
            additions.append(item)
            latest[key] = item
    if additions and not dry_run:
        updated = {"schema_version": ledger["schema_version"],
                   "entries": [*ledger["entries"], *additions]}
        validate_feedback_document(updated, feedback_file)
        _atomic_json(feedback_file, updated)
    return {"topic_id": topic, "status": "DRY_RUN" if dry_run else "SUCCESS",
            "imported": len(additions), **counts}
