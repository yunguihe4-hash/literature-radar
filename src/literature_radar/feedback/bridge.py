"""Import append-only Obsidian Bridge events into private, Topic-scoped Feedback."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from literature_radar.config import load_bundle, validate_feedback_document
from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.cards import card_key, frontmatter
from literature_radar.delivery.obsidian import _managed_card_location, _read_binding, deliver_cards
from literature_radar.delivery.review_ui import DECISIONS, review_state, set_properties
from literature_radar.feedback.batches import prepare_due_batches
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.private_paths import ensure_private_path


def sync_outbox(binding_file: Path, config_dirs: list[Path], database: Path) -> dict:
    binding_file = ensure_private_path(binding_file)
    database = ensure_private_path(database)
    binding = _read_binding(config=binding_file)
    if binding is None:
        raise ValueError("OBSIDIAN_NOT_CONNECTED")
    root = Path(binding["delivery_root"])
    outbox = Path(binding["vault_root"]) / ".obsidian/plugins/literature-radar-bridge/outbox.json"
    if not outbox.is_file():
        return {"status": "NO_OUTBOX", "imported": 0, "topics": {}}
    raw = json.loads(outbox.read_text(encoding="utf-8"))
    events = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(events, list):
        raise ValueError("Bridge Outbox is invalid")
    configs = {load_bundle(path)["topic"]["id"]: ensure_private_path(path) for path in config_dirs}
    if len(configs) != len(config_dirs):
        raise ValueError("Duplicate Topic config")
    papers = {topic: {paper.paper_id: paper for paper in AcademicStore(database).topic_papers(topic)}
              for topic in configs}
    imported = 0
    topics = {}
    failures = []
    touched = {}
    for event in events:
        try:
            if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
                raise ValueError("Missing event ID")
            topic = event.get("topic_id")
            if topic not in configs:
                continue
            relative = Path(str(event.get("path", "")))
            location = _managed_card_location(str(relative))
            if location is None or location[1] != topic:
                raise ValueError("Outbox path leaves its Topic and Attention directory")
            card = root / relative
            if card.is_symlink() or not card.is_file() or not card.resolve().is_relative_to(root.resolve()):
                raise ValueError("Outbox card is missing or unsafe")
            meta = frontmatter(card.read_text(encoding="utf-8"))
            if location[2] not in {meta.get("first_attention"), meta.get("current_attention")}:
                raise ValueError("Outbox card status differs from its directory")
            for field in ("topic_id", "paper_key", "paper_id", "doi"):
                if meta.get(field) != event.get(field):
                    raise ValueError("Outbox identity differs from card")
            paper = papers[topic].get(meta["paper_id"])
            if paper is None:
                raise ValueError("Outbox paper is absent from Topic database")
            doi = str(paper.doi or paper.identifiers.get("doi") or "").casefold()
            if meta["doi"].casefold() != doi or meta["paper_key"] != card_key(topic, paper.paper_id, doi):
                raise ValueError("Outbox DOI or paper_key differs from database")
            decision = event.get("decision")
            if decision not in DECISIONS:
                raise ValueError("Outbox decision is invalid")
            recorded_at = str(event.get("recorded_at", ""))
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            config = configs[topic]
            feedback_file = config / "feedback.json"
            current = load_feedback(feedback_file)
            entries = current["entries"]
            if any(item["topic_id"] != topic for item in entries):
                raise ValueError("Feedback file contains another Topic")
            prior_event = next((item for item in entries if
                                item.get("extensions", {}).get("bridge_event_id") == event["id"]), None)
            if prior_event is not None:
                if (prior_event["paper_key"] != meta["paper_key"] or
                        prior_event["decision"] != decision):
                    raise ValueError("Bridge event ID conflicts with imported feedback")
                touched[card] = (decision, recorded_at)
                continue
            previous = next((item for item in reversed(entries) if item["topic_id"] == topic and
                             item["paper_key"] == meta["paper_key"]), None)
            if previous is None or previous["decision"] != decision:
                updated = {"schema_version": current["schema_version"], "entries": [*entries, {
                    "topic_id": topic, "paper_key": meta["paper_key"], "decision": decision,
                    "reason": f"用户在 Obsidian 卡片中选择「{DECISIONS[decision]}」",
                    "recorded_at": recorded_at,
                    "extensions": {"source": "obsidian_bridge", "bridge_event_id": event["id"],
                                   "source_run_id": meta.get("source_run", "")}}]}
                validate_feedback_document(updated, feedback_file)
                _atomic_json(feedback_file, updated)
                imported += 1
                topics[topic] = topics.get(topic, 0) + 1
            touched[card] = (decision, recorded_at)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append({"event_id": event.get("id") if isinstance(event, dict) else None,
                             "error": str(exc)})
    for card, (decision, timestamp) in touched.items():
        text = card.read_text(encoding="utf-8")
        state, current = review_state(text)
        if state == "reviewed" and current == decision:
            updated = set_properties(text, {"feedback_sync_status": "synced",
                                            "feedback_updated_at": timestamp,
                                            "feedback_synced_decision": decision})
            if updated != text:
                from literature_radar.delivery.obsidian import _atomic_bytes
                _atomic_bytes(card, updated.encode("utf-8"))
    batches = {}
    for topic, config in configs.items():
        batches[topic] = prepare_due_batches(config, cards_root=root / "Bootstrap" / topic)
    if imported or touched:
        deliver_cards([], section="Bootstrap", config=binding_file)
    _atomic_json(outbox.parent / "sync-status.json", {
        "schema_version": 1, "status": "SUCCESS" if not failures else "PARTIAL",
        "updated_at": datetime.now().astimezone().isoformat(),
        "failed_event_ids": [item["event_id"] for item in failures if item["event_id"]],
    })
    return {"status": "SUCCESS" if not failures else "PARTIAL", "imported": imported,
            "topics": topics, "failures": failures, "batches": batches}
