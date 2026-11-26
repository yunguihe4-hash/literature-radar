"""Explicit, idempotent human feedback import from Obsidian review cards."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle, validate_feedback_document
from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.cards import MARKERS, card_key, frontmatter
from literature_radar.delivery.review_ui import DECISIONS, review_state
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.private_paths import ensure_private_path
from literature_radar.workflow.state import advance, read_state

CHOICES = {"important": "must_read", "useful": "keep", "neutral": "later", "irrelevant": "discard"}
CHECKBOX = re.compile(r"(?m)^- \[([ xX])\] .+?<!-- feedback:(important|useful|neutral|irrelevant) -->\s*$")
REQUIRED = {"type", "topic", "topic_id", "paper_key", "paper_id", "doi",
            "source_run", "first_seen", "first_recommended", "first_attention",
            "last_seen", "current_attention"}


def parse_decision(text: str) -> tuple[str | None, str | None]:
    metadata = frontmatter(text)
    if "review_status" in metadata or "feedback_decision" in metadata:
        state, decision = review_state(text)
        if state == "invalid":
            raise ValueError("invalid review Properties")
        if state == "pending":
            return None, None
        notes = text.split("## 我的笔记", 1)[1].strip() if "## 我的笔记" in text else ""
        return decision, notes.split("\n## ", 1)[0].strip() or f"用户在 Obsidian 卡片中选择「{DECISIONS[decision]}」"
    selected = [code for mark, code in CHECKBOX.findall(text) if mark.lower() == "x"]
    if len(selected) > 1:
        raise ValueError("multiple feedback choices checked in one card")
    if not selected:
        return None, None
    code = selected[0]
    notes = text.split("## 我的笔记", 1)[1].strip() if "## 我的笔记" in text else ""
    reason = notes.split("\n## ", 1)[0].strip() or f"用户在 Obsidian 卡片中勾选「{MARKERS[code]}」"
    return CHOICES[code], reason


def _manual_extensions(text: str) -> dict[str, str]:
    notes = text.split("## 我的笔记", 1)[1].split("\n## ", 1)[0] if "## 我的笔记" in text else ""
    fields = {"重要方法": "important_method", "重要问题": "important_question",
              "迁移类型": "transfer_type"}
    result = {"source": "obsidian_card"}
    for line in notes.splitlines():
        label, sep, value = line.strip().partition("：")
        if sep and label in fields and value.strip():
            result[fields[label]] = value.strip()
    return result


def import_cards(config_dir: Path, cards_root: Path, database: Path,
                 feedback_file: Path | None = None, *, allow_revisions: bool = False) -> dict:
    config_dir = ensure_private_path(config_dir)
    cards_root = ensure_private_path(cards_root)
    database = ensure_private_path(database)
    feedback_file = ensure_private_path(feedback_file or config_dir / "feedback.json")
    topic_config = load_bundle(config_dir)["topic"]
    topic_id = topic_config["id"]
    papers = {paper.paper_id: paper for paper in AcademicStore(database).topic_papers(topic_id)}
    existing = load_feedback(feedback_file)
    if any(entry["topic_id"] != topic_id for entry in existing["entries"]):
        raise ValueError("feedback file contains another Topic")
    prior = {entry["paper_key"]: entry["decision"] for entry in existing["entries"]}
    submissions = {}
    pending = conflicts = ignored = 0
    if not cards_root.is_dir():
        raise ValueError("review card directory does not exist")
    for path in sorted(cards_root.rglob("*.md")):
        if path.is_symlink():
            raise ValueError("symlink review card is forbidden")
        text = path.read_text(encoding="utf-8")
        try: metadata = frontmatter(text)
        except ValueError: continue
        if metadata.get("type") != "literature-review-card":
            continue
        if metadata.get("topic_id") != topic_id:
            ignored += 1
            continue
        if not REQUIRED.issubset(metadata) or metadata["topic"] not in {topic_id, topic_config["display_name"]}:
            raise ValueError(f"incomplete or conflicting card Properties: {path}")
        if path.parent.parent.name != topic_id:
            raise ValueError(f"card Topic does not match directory: {path}")
        paper_id = metadata["paper_id"]
        paper = papers.get(paper_id)
        if paper is None:
            raise ValueError(f"card paper_id is absent from this Topic: {path}")
        actual_doi = (paper.doi or paper.identifiers.get("doi") or "").casefold()
        card_doi = metadata["doi"].casefold()
        if actual_doi != card_doi or metadata["paper_key"] != card_key(topic_id, paper_id, actual_doi):
            raise ValueError(f"card DOI or paper_key differs from database identity: {path}")
        try: decision, reason = parse_decision(text)
        except ValueError:
            conflicts += 1
            raise ValueError(f"invalid or multiple feedback choices in {path}") from None
        if decision is None:
            pending += 1
            continue
        key = metadata["paper_key"]
        extensions = _manual_extensions(text)
        recorded_at = metadata.get("feedback_updated_at") or datetime.now(timezone.utc).isoformat()
        if key in submissions and submissions[key] != (decision, reason, extensions, recorded_at):
            raise ValueError(f"conflicting duplicate cards for {key}")
        submissions[key] = (decision, reason, extensions, recorded_at)
    additions = []
    for key, (decision, reason, extensions, recorded_at) in submissions.items():
        if key in prior:
            if prior[key] != decision:
                if not allow_revisions:
                    raise ValueError(f"card decision conflicts with previously imported feedback for {key}")
            else:
                continue
        prior[key] = decision
        additions.append({"topic_id": topic_id, "paper_key": key,
                          "decision": decision, "reason": reason,
                          "recorded_at": recorded_at,
                          "extensions": extensions})
    updated = {"schema_version": existing["schema_version"],
               "entries": [*existing["entries"], *additions]}
    validate_feedback_document(updated, feedback_file)
    if additions:
        _atomic_json(feedback_file, updated)
        if read_state(config_dir)["state"] == "BOOTSTRAP_REVIEW_READY":
            advance(config_dir, "FEEDBACK_IMPORTED")
    return {"topic_id": topic_id, "imported": len(additions),
            "pending": pending, "ignored_other_topics": ignored,
            "conflicts": conflicts, "total_feedback_entries": len(updated["entries"])}
