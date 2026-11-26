"""Explicit TSV decisions and confirmed, repeat-supported Memory proposals."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.config import load_bundle, load_attention_memory, validate_feedback_document
from literature_radar.feedback.store import DECISIONS, load_feedback
from literature_radar.workflow.state import advance, read_state
from literature_radar.private_paths import ensure_private_path

FIELDS = ("paper_id", "decision", "scientific_reason", "important_method",
          "important_question", "transfer_type")
TRANSFER_TYPES = {"direct_reference", "method_transfer", "concept_transfer", "application_transfer"}


def _atomic_json(path: Path, value: dict) -> None:
    path = ensure_private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
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


def import_review(config_dir: Path, review_tsv: Path, feedback_file: Path) -> dict:
    config_dir = ensure_private_path(config_dir)
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    state = read_state(config_dir)
    if state["state"] not in ("BOOTSTRAP_REVIEW_READY", "FEEDBACK_IMPORTED"):
        raise ValueError("Complete the Bootstrap review before importing initial Feedback")
    baseline_ids = None
    if state.get("report"):
        report = json.loads(Path(state["report"]).read_text(encoding="utf-8"))
        if report.get("topic_id") != topic_id:
            raise ValueError("Bootstrap report belongs to a different Topic")
        baseline_ids = {item["paper_id"] for item in report["papers"]}
    with Path(review_tsv).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or any(field not in reader.fieldnames for field in FIELDS):
            raise ValueError("Review TSV is missing required columns")
        rows = list(reader)
    feedback_file = ensure_private_path(feedback_file)
    existing = load_feedback(feedback_file)
    if any(item["topic_id"] != topic_id for item in existing["entries"]):
        raise ValueError("Feedback file contains a different Topic; use a per-Topic file")
    seen = {(item["paper_key"], item["decision"], item["reason"])
            for item in existing["entries"] if item["topic_id"] == topic_id}
    additions = []
    submitted = {}
    for row in rows:
        paper_id = (row.get("paper_id") or "").strip()
        decision = (row.get("decision") or "").strip()
        reason = (row.get("scientific_reason") or "").strip()
        if not decision:
            continue
        if not paper_id or decision not in DECISIONS or not reason:
            raise ValueError("A reviewed row needs paper_id, valid decision and scientific_reason")
        if baseline_ids is not None and paper_id not in baseline_ids:
            raise ValueError("Reviewed paper_id is absent from the Bootstrap report")
        if paper_id in submitted and submitted[paper_id] != (decision, reason):
            raise ValueError("Review TSV has conflicting decisions for one paper_id")
        submitted[paper_id] = (decision, reason)
        transfer = (row.get("transfer_type") or "").strip()
        if transfer and transfer not in TRANSFER_TYPES:
            raise ValueError("Invalid reviewed transfer_type")
        key = (paper_id, decision, reason)
        if key in seen:
            continue
        seen.add(key)
        extensions = {key: (row.get(key) or "").strip() for key in
                      ("important_method", "important_question", "transfer_type")
                      if (row.get(key) or "").strip()}
        additions.append({"topic_id": topic_id, "paper_key": paper_id,
                          "decision": decision, "reason": reason,
                          "recorded_at": datetime.now(timezone.utc).isoformat(),
                          **({"extensions": extensions} if extensions else {})})
    updated = {"schema_version": 1, "entries": [*existing["entries"], *additions]}
    validate_feedback_document(updated, feedback_file)
    if additions:
        _atomic_json(Path(feedback_file), updated)
        advance(config_dir, "FEEDBACK_IMPORTED")
    return {"topic_id": topic_id, "imported": len(additions),
            "total": len({item["paper_key"] for item in updated["entries"] if item["topic_id"] == topic_id})}


def feedback_summary(config_dir: Path, feedback_file: Path) -> dict:
    topic_id = load_bundle(config_dir)["topic"]["id"]
    latest = {item["paper_key"]: item for item in load_feedback(feedback_file)["entries"]
              if item["topic_id"] == topic_id}
    items = list(latest.values())
    counts = Counter(item["decision"] for item in items)
    return {"topic_id": topic_id, "total": len(items),
            "decisions": {key: counts[key] for key in ("must_read", "keep", "later", "discard")}}


def _resolve_id(value: str, bundle: dict, section: str) -> str | None:
    value = value.casefold()
    for entry in bundle["profile"][section]:
        if value in {entry["id"].casefold(), entry["label"].casefold(),
                     *(alias.casefold() for alias in entry.get("aliases", []))}:
            return entry["id"]
    return None


def propose_memory(config_dir: Path, feedback_file: Path, output: Path,
                   *, minimum_papers: int = 2) -> dict:
    config_dir = ensure_private_path(config_dir)
    if minimum_papers < 2:
        raise ValueError("Memory proposal requires repeated feedback from at least two papers")
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    if read_state(config_dir)["state"] not in ("FEEDBACK_IMPORTED", "MEMORY_REVIEW"):
        raise ValueError("Import human feedback before proposing Memory")
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    latest = {item["paper_key"]: item for item in load_feedback(feedback_file)["entries"]
              if item["topic_id"] == topic_id}
    for item in latest.values():
        if item["topic_id"] != topic_id or item["decision"] not in ("must_read", "keep"):
            continue
        values = item.get("extensions", {})
        for key, section in (("important_method", "methods"), ("important_question", "questions")):
            identifier = _resolve_id(values.get(key, ""), bundle, section) if values.get(key) else None
            if identifier:
                grouped[("concept_id", identifier)].add(item["paper_key"])
        transfer = values.get("transfer_type")
        if transfer in TRANSFER_TYPES:
            grouped[("transfer_type", transfer)].add(item["paper_key"])
    candidates = [{"kind": kind, "value": value, "supporting_paper_ids": sorted(papers),
                   "support_count": len(papers)}
                  for (kind, value), papers in sorted(grouped.items())
                  if len(papers) >= minimum_papers]
    proposal = {"schema_version": 1, "topic_id": topic_id,
                "status": "AWAITING_EXPLICIT_CONFIRMATION", "candidates": candidates}
    output = Path(output)
    output = ensure_private_path(output)
    if output.exists():
        raise FileExistsError("Memory proposal already exists")
    _atomic_json(output, proposal)
    advance(config_dir, "MEMORY_REVIEW", memory_proposal=str(output))
    return proposal


def apply_memory(config_dir: Path, proposal_file: Path, *, confirmed: bool = False) -> dict:
    config_dir = ensure_private_path(config_dir)
    if not confirmed:
        raise ValueError("Memory apply requires --confirm")
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    if read_state(config_dir)["state"] != "MEMORY_REVIEW":
        raise ValueError("Memory proposal must be reviewed before apply")
    proposal = json.loads(Path(proposal_file).read_text(encoding="utf-8"))
    if proposal.get("topic_id") != topic_id or proposal.get("status") != "AWAITING_EXPLICIT_CONFIRMATION":
        raise ValueError("Memory proposal does not belong to this Topic or is invalid")
    memory_file = Path(config_dir) / "attention-memory.json"
    memory = load_attention_memory(memory_file, bundle)
    known = {entry["id"] for section in ("questions", "methods", "applications")
             for entry in bundle["profile"][section]} | {entry["id"] for entry in bundle["ontology"]["concepts"]}
    chosen = proposal.get("confirmed", [])
    if not isinstance(chosen, list) or any(not isinstance(item, dict) for item in chosen):
        raise ValueError("Proposal needs a confirmed array of selected candidates")
    candidates = {(item["kind"], item["value"]): item for item in proposal["candidates"]}
    concept_ids = set(memory["preferred_concept_ids"])
    transfers = set(memory.get("extensions", {}).get("confirmed_transfer_types", []))
    for item in chosen:
        key = (item.get("kind"), item.get("value"))
        if key not in candidates or candidates[key]["support_count"] < 2:
            raise ValueError("Confirmed candidate lacks repeated human support")
        if key[0] == "concept_id":
            if key[1] not in known:
                raise ValueError("Unknown Memory concept")
            concept_ids.add(key[1])
        elif key[0] == "transfer_type" and key[1] in TRANSFER_TYPES:
            transfers.add(key[1])
        else:
            raise ValueError("Invalid Memory candidate")
    memory["preferred_concept_ids"] = sorted(concept_ids)
    memory.setdefault("extensions", {})["confirmed_transfer_types"] = sorted(transfers)
    _atomic_json(memory_file, memory)
    load_attention_memory(memory_file, load_bundle(config_dir))
    advance(config_dir, "MEMORY_REVIEW", memory_review_confirmed=True,
            confirmed_candidate_count=len(chosen))
    return {"topic_id": topic_id, "preferred_concept_ids": sorted(concept_ids),
            "confirmed_transfer_types": sorted(transfers)}
