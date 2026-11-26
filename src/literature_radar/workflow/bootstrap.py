"""Per-Topic initial research; retain only the selected reading papers."""
from __future__ import annotations

import csv
import calendar
import io
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.cards import card_key, write_card
from literature_radar.delivery.obsidian import deliver_generated
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.private_paths import ensure_private_path
from literature_radar.workflow.state import advance, read_state

PROVIDERS = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")


def _is_preprint(paper) -> bool:
    return str(paper.document_type or "").casefold() in ("preprint", "posted-content", "posted content")


def five_year_window(today: date) -> tuple[str, str]:
    try:
        start = today.replace(year=today.year - 5)
    except ValueError:
        start = today.replace(year=today.year - 5, day=28)
    return start.isoformat(), today.isoformat()


def _publication_date(paper) -> tuple[str | None, str]:
    if _is_preprint(paper):
        return paper.online_date or paper.publication_date, "preprint_release"
    if paper.publication_date:
        return paper.publication_date, "formal_publication"
    if paper.online_date:
        return paper.online_date, "first_online"
    return None, "unverified"


def _date_bounds(value: str) -> tuple[date, date]:
    parts = value.split("-")
    if len(parts) == 1:
        year = int(parts[0])
        return date(year, 1, 1), date(year, 12, 31)
    if len(parts) == 2:
        year, month = map(int, parts)
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    day = date.fromisoformat(value[:10])
    return day, day


def _issn(paper) -> tuple[list[str], str | None]:
    issn, issn_l = [], None
    for record in paper.source_records:
        value = record.normalized_metadata.get("issn")
        if isinstance(value, list):
            issn.extend(item for item in value if isinstance(item, str))
        elif isinstance(value, str):
            issn.append(value)
        value = record.normalized_metadata.get("issn_l")
        if isinstance(value, str):
            issn_l = value
    return list(dict.fromkeys(issn)), issn_l


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _report_rows(config_dir: Path, database: Path, window: tuple[str, str],
                 registry_path: Path | None, limit: int) -> tuple[list[dict], dict]:
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    store = AcademicStore(database, cache_abstracts=True)
    papers = store.topic_papers(topic_id)
    feedback = {"preferred_concept_ids": []}
    if "attention-memory" in bundle:
        feedback = bundle["attention-memory"]
    links = store.preprint_links()
    published_dois = {str(paper.doi or paper.identifiers.get("doi") or "").casefold()
                      for paper in papers if not _is_preprint(paper)}
    rows = []
    stats = {"observed": len(papers), "outside_window": 0, "date_unverified": 0,
             "irrelevant_d": 0, "preprint_version_duplicate": 0}
    for paper in papers:
        when, date_kind = _publication_date(paper)
        if not when:
            stats["date_unverified"] += 1
            continue
        try:
            earliest, latest = _date_bounds(when)
        except ValueError:
            stats["date_unverified"] += 1
            continue
        if not (date.fromisoformat(window[0]) <= earliest and latest <= date.fromisoformat(window[1])):
            stats["outside_window"] += 1
            continue
        if _is_preprint(paper) and any(
                preprint_id == paper.paper_id and doi.casefold() in published_dois
                for preprint_id, doi in links):
            stats["preprint_version_duplicate"] += 1
            continue
        assessment = recommend(paper, bundle, feedback)
        relevance = assessment["attention_ranking"]["category"]
        if relevance == "D":
            stats["irrelevant_d"] += 1
            continue
        reason = assessment["reason"]
        transfer = next((item for item in reason["structured_transfers"]
                         if item["status"] == "EVIDENCE_MATCHED"), None)
        evidence = ({"field": transfer["evidence_field"], "excerpt": transfer["evidence_excerpt"],
                     "source": transfer["evidence_source"]}
                    if transfer and relevance in ("B", "C") else
                    next((item for item in reason["evidence"] if item.get("excerpt")), None))
        why = (transfer["transfer_rationale"] if transfer and relevance in ("B", "C") else
               f"Question {reason['questions'][0]} has title/abstract evidence"
               if reason["questions"] and evidence else
               reason["topic_corroboration"]["rationale"] if reason.get("topic_corroboration") else None)
        if evidence is None and reason.get("topic_corroboration"):
            evidence = reason["topic_corroboration"]["evidence"]
        rows.append({"paper_id": paper.paper_id, "title": paper.title,
                     "topic_id": topic_id,
                     "paper_key": card_key(topic_id, paper.paper_id, paper.doi or paper.identifiers.get("doi")),
                     "first_seen": paper.first_seen_at,
                     "abstract": paper.abstract,
                     "abstract_status": "AVAILABLE" if paper.abstract else "MISSING",
                     "year": earliest.year, "date": when, "date_kind": date_kind,
                     "date_source": date_kind,
                     "search_window_start": window[0], "search_window_end": window[1],
                     "date_precision": "day" if len(when) >= 10 else "month" if len(when) >= 7 else "year",
                     "document_type": paper.document_type,
                     "indexed_date": paper.indexed_date, "updated_date": paper.updated_date,
                     "journal": paper.journal or ("bioRxiv" if any(
                         item.provider_id == "biorxiv" for item in paper.source_records) else None),
                     "doi": paper.doi or paper.identifiers.get("doi"),
                     "url": paper.landing_url,
                     "relevance": relevance,
                     "priority": "BOOTSTRAP_SEED",
                     "score": assessment["base_score"],
                     "fulltext_status": "NOT_VERIFIED",
                     "transfer_type": transfer["transfer_type"] if transfer else
                                      ("direct_reference" if relevance == "A" else None),
                     "transfer_explanation": transfer,
                     "research_question": reason["questions"][0] if reason["questions"] else
                                          reason["topic_corroboration"]["question_id"] if reason.get("topic_corroboration") else None,
                     "research_question_label": next((item["label"] for item in bundle["profile"]["questions"]
                         if item["id"] == (reason["questions"][0] if reason["questions"] else
                             reason["topic_corroboration"]["question_id"] if reason.get("topic_corroboration") else None)), None),
                     "research_phase": "重点问题：" + "、".join(item["label"] for item in bundle["profile"]["questions"]
                         if item["id"] in bundle["phase"]["priority_question_ids"]),
                     "method_evidence": next((item["excerpt"] for item in reason["evidence"]
                         if item.get("field") == "abstract" and item.get("phrase") in
                         {alias for method in bundle["profile"]["methods"] for alias in method.get("aliases", [])}), None),
                     "research_alignment": transfer.get("transfer_rationale") if transfer and transfer.get("evidence_excerpt") else
                         reason.get("topic_corroboration", {}).get("rationale") if reason.get("topic_corroboration") else None,
                     "evidence": evidence,
                     "why_recommended": why,
                     "borrowing_value": transfer["transfer_rationale"] if transfer else None,
                     "review_required": assessment["attention_ranking"]["review_required"],
                     "_selection_order": assessment["attention_ranking"]["ordering"],
                     "citations": next((record.normalized_metadata.get("citation_count") for record in paper.source_records
                                        if record.normalized_metadata.get("citation_count") is not None), None)})
    rows.sort(key=lambda row: (row["_selection_order"],
                               row["title"].casefold()))
    for row in rows:
        row.pop("_selection_order")
    for index, row in enumerate(rows[:limit], 1):
        row["rank"] = index
    return rows[:limit], stats


def export_review(output_dir: Path, report: dict) -> dict:
    output_dir = ensure_private_path(output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomic(output_dir / "bootstrap-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(output_dir / "bootstrap-coverage.json", json.dumps(report["coverage"], ensure_ascii=False, indent=2) + "\n")
    lines = [f"# Initial literature review: {report['topic_id']}", "",
             f"Window: {report['window']['start']} to {report['window']['end']}",
             f"Completeness: {report['completeness']}",
             f"Selected: {len(report['papers'])} (A/B/C only)", "",
             "| Rank | Title | Year | Type | Journal | DOI | Relevance | Why recommended |",
             "|---:|---|---:|---|---|---|---|---|"]
    for row in report["papers"]:
        safe = lambda value: str(value if value is not None else "未核实").replace("|", "\\|").replace("\n", " ")
        lines.append("| " + " | ".join(safe(value) for value in
                     (row["rank"], row["title"], row["year"], row["document_type"], row["journal"], row["doi"],
                      row["relevance"],
                      row["why_recommended"])) + " |")
    lines.extend(["", "## Evidence and borrowing details", ""])
    for row in report["papers"]:
        lines.extend([
            f"### {row['rank']}. {row['title']}", "",
            f"- Year / date basis: {row['year']} / {row['date_kind']} ({row['date']})",
            f"- Document type: {row['document_type'] or '未核实'}",
            f"- Journal: {row['journal'] or '未核实'}",
            f"- DOI / original URL: {row['doi'] or '未核实'} / {row['url'] or '未核实'}",
            f"- Abstract: {row['abstract'] or '未提供'}",
            f"- Relevance / transfer: {row['relevance']} / {row['transfer_type'] or '待审核'}",
            f"- Transfer explanation: {json.dumps(row['transfer_explanation'], ensure_ascii=False) if row['transfer_explanation'] else '待审核'}",
            f"- Research question: {row['research_question'] or '待审核'}",
            f"- Evidence: {json.dumps(row['evidence'], ensure_ascii=False) if row['evidence'] else '摘要证据不足'}",
            f"- Why recommended: {row['why_recommended'] or '待审核'}",
            f"- Borrowing value: {row['borrowing_value'] or '待审核'}",
            f"- Review required: {'yes' if row['review_required'] else 'no'}", "",
        ])
    _write_atomic(output_dir / "bootstrap-report.md", "\n".join(lines) + "\n")
    card_paths = []
    for row in report["papers"]:
        card_paths.append(write_card(
            output_dir / "cards", row, topic_id=report["topic_id"],
            topic_name=report.get("topic_name", report["topic_id"]),
            run_id=report.get("run_id", "bootstrap"), seen_on=report["window"]["end"],
            attention="BOOTSTRAP_SEED"))
    card_index = ["# 初始文献卡片：" + report["topic_id"], "",
                  f"目标 30 篇；当前 {len(card_paths)} 篇；状态 {report['completeness']}", ""]
    card_index.extend(f"- [{row['title']}]({path.relative_to(output_dir).as_posix()})"
                      for row, path in zip(report["papers"], card_paths))
    _write_atomic(output_dir / "card-index.md", "\n".join(card_index) + "\n")
    tsv = output_dir / "bootstrap-review.tsv"
    columns = ("paper_id", "decision", "scientific_reason", "important_method",
               "important_question", "transfer_type")
    existing = []
    if tsv.exists():
        with tsv.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if tuple(reader.fieldnames or ()) != columns:
                raise ValueError("Existing review TSV columns differ; refusing to overwrite")
            existing = list(reader)
    present = {row["paper_id"] for row in existing}
    review = io.StringIO(newline="")
    writer = csv.DictWriter(review, fieldnames=columns, delimiter="\t")
    writer.writeheader()
    writer.writerows(existing)
    for row in report["papers"]:
        if row["paper_id"] not in present:
            writer.writerow({"paper_id": row["paper_id"], "decision": "",
                             "scientific_reason": "", "important_method": "",
                             "important_question": row["research_question"] or "",
                             "transfer_type": row["transfer_type"] or ""})
    _write_atomic(tsv, review.getvalue())
    return {"json": str(output_dir / "bootstrap-report.json"),
            "markdown": str(output_dir / "bootstrap-report.md"),
            "review_tsv": str(tsv), "coverage": str(output_dir / "bootstrap-coverage.json"),
            "card_index": str(output_dir / "card-index.md"), "cards": len(card_paths)}


def run_bootstrap(config_dir: Path, database: Path, output_dir: Path, *,
                  registry_path: Path | None = None, provider_names: list[str] | None = None,
                  providers: Mapping[str, object] | None = None,
                  max_pages: int = 200, max_seconds: float = 1800,
                  limit: int = 30, now: datetime | None = None,
                  dry_run: bool = False, resume: bool = False) -> dict:
    config_dir = ensure_private_path(config_dir)
    database = ensure_private_path(database)
    output_dir = ensure_private_path(output_dir)
    # registry_path remains accepted for old CLI callers but is ignored.
    bundle = load_bundle(config_dir)
    topic_id = bundle["topic"]["id"]
    if not 1 <= limit <= 50:
        raise ValueError("Bootstrap limit must be 1-50")
    selected = provider_names or list(PROVIDERS)
    if any(name not in PROVIDERS for name in selected):
        raise ValueError("Unknown bootstrap Provider")
    current = read_state(config_dir)
    if current["topic_id"] != topic_id:
        raise ValueError("Workflow state belongs to another Topic")
    if current["state"] in ("FEEDBACK_IMPORTED", "MEMORY_REVIEW", "WEEKLY_MONITORING"):
        raise ValueError("Bootstrap review has already advanced to human feedback")
    if current["state"] == "BOOTSTRAP_REVIEW_READY" and not dry_run:
        raise ValueError("Bootstrap review is ready; do not overwrite the human review baseline")
    if resume and current["state"] != "BOOTSTRAP_RUNNING":
        raise ValueError("No incomplete Bootstrap run to resume")
    if resume and (current.get("database") != str(database) or
                   current.get("output_dir") != str(output_dir)):
        raise ValueError("Resume must use the original database and output directory")
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start, end = ((current["window"]["start"], current["window"]["end"])
                  if resume and current.get("window") else five_year_window(instant.date()))
    working_database = output_dir / "bootstrap-working.sqlite3"
    if working_database.resolve() == database.resolve():
        raise ValueError("Bootstrap working database must differ from the retained database")
    if resume and current.get("working_database") not in (None, str(working_database)):
        raise ValueError("Resume must use the original Bootstrap working database")
    if not dry_run:
        advance(config_dir, "BOOTSTRAP_RUNNING", window={"start": start, "end": end},
                output_dir=str(output_dir), database=str(database),
                working_database=str(working_database))
    summary = discover_incremental(config_dir, selected, database_path=working_database,
                                   since=start, until=end, max_pages=max_pages,
                                   max_seconds=max_seconds, providers=providers,
                                   cache_abstracts=True, strict_records=True,
                                   channels=[], notifiers={}, current_time=instant,
                                   dry_run=dry_run)
    if dry_run:
        return {"topic_id": topic_id, "window": {"start": start, "end": end},
                "dry_run": True, "discovery": summary}
    papers, counts = _report_rows(config_dir, working_database, (start, end), registry_path, limit)
    complete = summary["status"] == "SUCCESS" and all(
        item["status"] == "SUCCESS" for item in summary["providers"].values())
    if not complete:
        return {"topic_id": topic_id, "completeness": "INCOMPLETE",
                "selected": len(papers), "coverage": {"complete": False,
                "providers": summary["providers"], "candidate_counts": counts,
                "window": {"start": start, "end": end}},
                "working_database": str(working_database),
                "status": "RESUMABLE"}
    ids = AcademicStore(database, cache_abstracts=True).import_selected_papers(
        working_database, topic_id, [row["paper_id"] for row in papers],
        priorities=bundle["topic"].get("extensions", {}).get("field_priority", {}))
    for row in papers:
        row["paper_id"] = ids[row["paper_id"]]
        row["paper_key"] = card_key(topic_id, row["paper_id"], row["doi"])
    coverage = {"complete": complete, "providers": summary["providers"],
                "candidate_counts": counts, "selected": len(papers),
                "window": {"start": start, "end": end}}
    report = {"topic_id": topic_id, "topic_name": bundle["topic"]["display_name"],
              "run_id": instant.strftime("%Y%m%dT%H%M%SZ") + "-" + topic_id,
              "window": coverage["window"],
              "completeness": "INCOMPLETE" if not complete else ("PARTIAL" if len(papers) < 30 else "COMPLETE"),
              "coverage": coverage, "papers": papers}
    paths = export_review(output_dir, report)
    obsidian_delivery = deliver_generated(output_dir / "cards", section="Bootstrap")
    if complete:
        advance(config_dir, "BOOTSTRAP_REVIEW_READY", report=paths["json"])
        working_database.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(working_database) + suffix).unlink(missing_ok=True)
    return {"topic_id": topic_id, "completeness": report["completeness"],
            "selected": len(papers), "coverage": coverage, "outputs": paths,
            "obsidian_delivery": obsidian_delivery}
