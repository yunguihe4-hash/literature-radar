"""Offline-capable composition of discovery, alignment and delivery."""
from __future__ import annotations
import json
from pathlib import Path
from literature_radar.config import load_bundle, load_attention_memory
from literature_radar.discovery.engine import DiscoveryProvider, discover
from literature_radar.candidates.processing import deduplicate, identity
from literature_radar.evaluation.engine import score_evidence
from literature_radar.intelligence.engine import align
from literature_radar.attention.ranking import assess
from literature_radar.database.store import PaperStore
from literature_radar.delivery.engine import Notifier, deliver

def run(config_dir: Path, provider: DiscoveryProvider, output_path: Path, *, database_path: Path | None = None, memory_path: Path | None = None, notifier: Notifier | None = None, run_directory: Path | None = None) -> dict:
    bundle = load_bundle(config_dir)
    memory = None
    if memory_path is not None:
        memory = load_attention_memory(memory_path, bundle)
    papers = deduplicate(discover(bundle, provider))
    if run_directory is not None:
        run_dir = Path(run_directory)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "candidates.json").write_text(json.dumps([p.as_dict() for p in papers], ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    if database_path is not None:
        PaperStore(database_path).save(bundle["topic"]["id"], papers)
    rows = []
    for paper in papers:
        evidence_score, evidence = score_evidence(paper, bundle["rules"])
        alignment = align(paper, bundle["profile"], bundle["ontology"])
        attention = assess(evidence_score, alignment, bundle["phase"], bundle["rules"], memory)
        rows.append({"key": identity(paper), "paper": paper.as_dict(), "evidence": evidence,
                     "alignment": alignment, "attention": attention})
    rows.sort(key=lambda item: (-item["attention"]["score"], item["key"]))
    result = {"schema_version": 1, "topic_id": bundle["topic"]["id"], "count": len(rows), "papers": rows}
    deliver(output_path, result, notifier)
    return result

# Explicit online entry point; the legacy run() path remains offline.
from literature_radar.discovery.incremental import discover_incremental
