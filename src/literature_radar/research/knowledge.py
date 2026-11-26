"""Explicit, human-confirmed export of evaluated specialty literature.

Generated notes use the original AI_OS note headings and graph builder. Existing
notes are never overwritten, so annotations remain under human control.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from literature_radar.private_paths import ensure_private_path
from literature_radar.research.knowledge_graph import KnowledgeGraphGenerator

INVALID_FILENAME = re.compile(r'[\\/:*?"<>|#^]')


def safe_filename(title: str) -> str:
    name = INVALID_FILENAME.sub("-", title).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name[:180].rstrip(" .") or "untitled-paper") + ".md"


def _yaml(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=False)


def _note(candidate: dict, evaluation: dict, snapshot: dict) -> str:
    topic, run_id = snapshot["topic_id"], snapshot["run_id"]
    title = candidate["title"]
    authors = candidate.get("authors") or []
    methods = evaluation["key_methods"]
    findings = evaluation["key_findings"]
    tags = ["literature", f"topic/{topic}",
            f"recommendation/{evaluation['recommendation']}"]
    author_lines = ["authors:", *[f"  - {_yaml(author)}" for author in authors]] if authors else ["authors: []"]
    year = (candidate.get("publication_date") or candidate.get("online_date") or "")[:4]
    frontmatter = ["---", f"title: {_yaml(title)}", *author_lines,
                   f"year: {_yaml(year)}", f"journal: {_yaml(candidate.get('journal'))}",
                   f"doi: {_yaml(candidate.get('doi'))}", f"topic: {_yaml(topic)}",
                   f"score: {evaluation['overall_score']:.2f}", "tags:",
                   *[f"  - {_yaml(tag)}" for tag in tags],
                   f"radar_paper_id: {_yaml(candidate['paper_id'])}",
                   f"radar_run_id: {_yaml(run_id)}", "---", ""]
    lines = frontmatter + ["# Summary", "", evaluation["chinese_summary"] or "—", "",
                           "## Why important", "", evaluation["recommendation_reason"] or "—", "",
                           "## Methods", "", *([f"- {item}" for item in methods] or ["- —"]), "",
                           "## Key findings", "", *([f"- {item}" for item in findings] or ["- —"]), "",
                           "## Relation to current project", "",
                           evaluation["direct_relevance"] or "—", "",
                           "_Generated from stored metadata and human evaluation; no full-text access is implied._", ""]
    return "\n".join(lines)


def export_knowledge(run_dir: Path, output_root: Path, *, confirmed: bool = False,
                     build_graph: bool = False) -> dict:
    if not confirmed:
        raise ValueError("Final human review confirmation is required for Knowledge export")
    run_dir, output_root = ensure_private_path(run_dir), ensure_private_path(output_root)
    snapshot = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    result = json.loads((run_dir / "evaluations.json").read_text(encoding="utf-8"))
    if (result.get("status") != "EVALUATED_AWAITING_FINAL_HUMAN_REVIEW" or
            result.get("run_id") != snapshot.get("run_id") or
            result.get("source_snapshot_sha256") != snapshot.get("sha256") or
            len(result.get("evaluations", [])) != len(snapshot.get("candidates", []))):
        raise ValueError("Research evaluation is incomplete or mismatched")
    by_id = {item["paper_id"]: item for item in snapshot["candidates"]}
    scored = {item["paper_id"]: item for item in result["evaluations"]}
    selected = result.get("selected_paper_ids", [])
    if any(paper_id not in by_id or paper_id not in scored for paper_id in selected):
        raise ValueError("Selected paper is not in the evaluated run")
    papers_dir = output_root / snapshot["topic_id"] / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    written, preserved = [], []
    for paper_id in selected:
        candidate, evaluation = by_id[paper_id], scored[paper_id]
        path = papers_dir / safe_filename(candidate["title"])
        marker = f"radar_paper_id: {_yaml(paper_id)}"
        if path.exists() and marker not in path.read_text(encoding="utf-8"):
            path = papers_dir / safe_filename(f"{candidate['title']} -- {paper_id}")
        if path.exists():
            if marker not in path.read_text(encoding="utf-8"):
                raise ValueError("Knowledge note filename collision")
            preserved.append(str(path))
            continue
        path.write_text(_note(candidate, evaluation, snapshot), encoding="utf-8")
        written.append(str(path))
    index_path = output_root / snapshot["topic_id"] / "index.md"
    graph = (str(index_path) if index_path.exists() else
             str(KnowledgeGraphGenerator(output_root).build(snapshot["topic_id"]))) if build_graph else None
    return {"run_id": snapshot["run_id"], "topic_id": snapshot["topic_id"],
            "written": written, "preserved": preserved, "graph": graph,
            "status": "EXPORTED_AFTER_HUMAN_CONFIRMATION"}
