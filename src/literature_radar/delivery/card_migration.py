"""Offline, bounded migration of managed review-card content."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from literature_radar.delivery.cards import START, END, _atomic, frontmatter, render_card
from literature_radar.delivery.review_ui import set_properties


def _section(text: str, heading: str) -> str:
    match = re.search(r"(?m)^## " + re.escape(heading) + r"\s*\n\s*([^\n]+)", text)
    return match.group(1).strip() if match else ""


def _bullet(text: str, label: str) -> str:
    match = re.search(r"(?m)^- " + re.escape(label) + r"：([^\n]+)", text)
    return match.group(1).strip() if match else ""


def _alignment(value: str) -> str | None:
    if not value or value == "待核对。":
        return None
    try:
        evidence = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(evidence, dict) and evidence.get("excerpt"):
        field = "摘要" if evidence.get("field") == "abstract" else "论文"
        return f"{field}证据：{evidence['excerpt']}；具体迁移需人工评估。"
    return value


def _managed(text: str) -> bool:
    return (text.count(START) == text.count(END) == 1 and
            text.index(START) < text.index(END))


def _render_update(text: str, paper: dict, config: dict) -> str:
    meta = frontmatter(text)
    if not _managed(text):
        raise ValueError("missing or ambiguous AUTO-GENERATED boundary")
    if meta.get("paper_id") != paper.get("paper_id") or meta.get("doi", "").casefold() != str(paper.get("doi") or "").casefold():
        raise ValueError("database/card paper identity differs")
    topic_id = meta["topic_id"]
    if config["topic"]["id"] != topic_id:
        raise ValueError("Topic config/card identity differs")
    question = _section(text, "研究问题") or _bullet(text, "研究问题")
    labels = {item["id"]: item["label"] for item in config["profile"]["questions"]}
    label = labels.get(question, question)
    if not label or label == "待核对。":
        label = next((item["label"] for item in config["profile"]["questions"]
                      if any(alias.casefold() in (paper.get("abstract") or "").casefold()
                             for alias in item.get("aliases", []))), "")
    phase = config["phase"].get("current_phase") or "重点问题：" + "、".join(
        labels[item] for item in config["phase"].get("priority_question_ids", []) if item in labels)
    old_evidence = _section(text, "相关性证据") or _bullet(text, "迁移价值")
    if old_evidence.startswith("LOW 保底推荐；"):
        old_evidence = old_evidence.removeprefix("LOW 保底推荐；")
    row = {"paper_id": paper["paper_id"], "doi": paper.get("doi"),
           "title": paper.get("title"), "abstract": paper.get("abstract"),
           "journal": paper.get("journal") or ("bioRxiv" if any(
               item.get("provider_id") == "biorxiv" for item in paper.get("source_records", [])) else None),
           "publication_date": meta.get("publication_date") or paper.get("publication_date"),
           "date": meta.get("publication_date") or paper.get("publication_date"),
           "url": paper.get("landing_url"), "research_question_label": label,
           "research_phase": phase, "relevance": meta.get("relevance_category"),
           "research_alignment": _alignment(old_evidence)}
    attention = meta.get("current_attention") or meta.get("attention")
    generated = render_card(row, topic_id=topic_id, topic_name=config["topic"]["display_name"],
                            run_id=meta.get("source_run") or "historical", seen_on=meta.get("delivery_date") or "1970-01-01",
                            attention=attention, previous="", pushed=False)
    block = generated[generated.index(START):generated.index(END) + len(END)]
    updated = text[:text.index(START)] + block + text[text.index(END) + len(END):]
    return set_properties(updated, {"topic": config["topic"]["display_name"],
                                    "journal": row["journal"] or "未收录期刊信息"},
                          protect_identity=False)


def migrate(vault: Path, database: Path, config_root: Path, archive: Path | None,
            backup_dir: Path | None, *, apply: bool = False) -> dict:
    vault, config_root = Path(vault), Path(config_root)
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    topics = {}
    changed, skipped, archives = [], [], []
    archive_index: dict[str, list[Path]] = {}
    if archive:
        for candidate in sorted(archive.rglob("*.md")):
            if candidate.is_symlink():
                continue
            try:
                key = frontmatter(candidate.read_text(encoding="utf-8")).get("paper_key")
                if key:
                    archive_index.setdefault(key, []).append(candidate)
            except (OSError, ValueError):
                continue
    for section in ("Bootstrap", "Weekly"):
        for path in sorted((vault / section).rglob("*.md")):
            if path.is_symlink():
                skipped.append((str(path), "symlink")); continue
            try:
                old = path.read_text(encoding="utf-8")
                meta = frontmatter(old)
                if meta.get("type") != "literature-review-card":
                    continue
                if not _managed(old):
                    raise ValueError("missing or ambiguous AUTO-GENERATED boundary")
                topic = meta["topic_id"]
                if topic not in topics:
                    base = config_root / topic
                    topics[topic] = {name: json.loads((base / (name + ".json")).read_text(encoding="utf-8"))
                                     for name in ("topic", "profile", "phase")}
                record = connection.execute("select payload from papers_v2 where paper_id=?", (meta["paper_id"],)).fetchone()
                if record is None:
                    raise ValueError("paper absent from SQLite")
                paper = json.loads(record[0])
                new = _render_update(old, paper, topics[topic])
                if new != old:
                    changed.append((path, old, new))
                if archive and section == "Weekly":
                    counterparts = archive_index.get(meta.get("paper_key", ""), [])
                    if len(counterparts) > 1:
                        raise ValueError("duplicate archive paper_key")
                    if counterparts:
                        counterpart = counterparts[0]
                        archived = counterpart.read_text(encoding="utf-8")
                        archived_meta = frontmatter(archived)
                        if archived_meta.get("paper_key") != meta.get("paper_key"):
                            raise ValueError("archive/Vault identity differs")
                        revised = _render_update(archived, paper, topics[topic])
                        if revised != archived:
                            archives.append((counterpart, archived, revised))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                skipped.append((str(path), str(exc)))
    connection.close()
    if apply:
        if backup_dir is None or backup_dir.exists():
            raise ValueError("apply requires a new backup directory")
        backup_dir.mkdir(parents=True, mode=0o700)
        for path, old, _ in changed:
            target = backup_dir / "vault" / path.relative_to(vault)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(old, encoding="utf-8")
        for path, old, _ in archives:
            target = backup_dir / "archive" / path.relative_to(archive)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(old, encoding="utf-8")
        for path, _, new in [*changed, *archives]:
            _atomic(path, new)
    return {"vault_changed": len(changed), "archive_changed": len(archives),
            "skipped": len(skipped), "skip_reasons": skipped, "applied": apply,
            "backup_dir": str(backup_dir) if apply else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.vault, args.database, args.config_root, args.archive,
                             args.backup_dir, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
