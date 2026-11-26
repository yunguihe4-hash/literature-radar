"""Deterministic Chinese Markdown report for completed human research reviews."""
from __future__ import annotations

import json
from pathlib import Path

from literature_radar.private_paths import ensure_private_path


def _text(value) -> str:
    return str(value) if value not in (None, "") else "—"


def _list(values: list[str]) -> str:
    return "；".join(values) if values else "—"


def _mode_sections(snapshot: dict) -> list[str]:
    """Render only search facts recorded with this run, never infer coverage."""
    mode = snapshot["mode"]
    context = snapshot.get("mode_context") or {}
    lines: list[str] = []
    if mode == "baseline":
        topic, focus = snapshot["topic"], snapshot["focus"]
        lines += ["## 当前调研范围", "", _text(topic.get("description")), "",
                  "## 当前 Focus", "",
                  f"{_text(focus.get('title'))}（{_text(focus.get('version'))}）："
                  f"{_text(focus.get('description'))}", ""]
    if mode in {"weekly", "targeted", "deep-baseline"} and context:
        window = context.get("window") or {}
        lines += ["## 检索范围与覆盖", "",
                  f"- 检索窗口：{_text(window.get('start'))} 至 {_text(window.get('end'))}",
                  f"- 来源：{_list(context.get('sources') or [])}",
                  f"- 检索状态：{_text(context.get('discovery_status'))}",
                  f"- 入选人工评价候选：{_text(context.get('candidate_count'))}", ""]
        variants = context.get("query_variants") or []
        if variants:
            lines += ["### 实际查询组", ""]
            lines += [f"- {item['name']}（{item['group']}）：{_list(item['terms'])}"
                      for item in variants]
            lines += [""]
    if mode == "targeted":
        lines += ["## 补漏解释", "",
                  "本报告中的候选数只表示本次检索后进入人工评价的记录；"
                  "不能据此推断新发表论文数或首次入库数。", ""]
    if mode == "deep-baseline":
        lines += ["## Recall audit", ""]
        audit = context.get("recall_audit")
        if audit:
            lines += [f"- Known seeds：{_text(audit.get('seed_count'))}",
                      f"- Recovered：{_text(audit.get('naturally_recovered'))}",
                      f"- Missing：{len(audit.get('missing_seeds') or [])}",
                      f"- Recall rate：{_text(audit.get('recall_rate'))}", ""]
        else:
            lines += ["本次运行未提供已知种子集，未计算召回率。", ""]
    return lines


def render_review_report(run_dir: Path, output: Path) -> dict:
    run_dir, output = ensure_private_path(run_dir), ensure_private_path(output)
    snapshot = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    result = json.loads((run_dir / "evaluations.json").read_text(encoding="utf-8"))
    candidate_ids = [item["paper_id"] for item in snapshot.get("candidates", [])]
    evaluated_ids = [item["paper_id"] for item in result.get("evaluations", [])]
    selected_ids = result.get("selected_paper_ids", [])
    if (result.get("run_id") != snapshot.get("run_id") or
            result.get("source_snapshot_sha256") != snapshot.get("sha256") or
            len(candidate_ids) != len(set(candidate_ids)) or
            len(evaluated_ids) != len(set(evaluated_ids)) or
            set(evaluated_ids) != set(candidate_ids) or
            len(selected_ids) != len(set(selected_ids)) or
            not set(selected_ids).issubset(candidate_ids)):
        raise ValueError("Research evaluation is incomplete or mismatched")
    candidates = {item["paper_id"]: item for item in snapshot["candidates"]}
    selected = set(selected_ids)
    mode = snapshot["mode"]
    lines = [f"# {_text(snapshot['topic'].get('display_name'))}：{mode} Research Report", "",
             f"- Run ID：{snapshot['run_id']}",
             f"- Topic：{snapshot['topic_id']}",
             f"- Focus：{snapshot['focus_version']}",
             f"- 候选：{len(candidates)}",
             f"- 人工评价：{len(result['evaluations'])}",
             f"- 推荐：{len(selected)}",
             f"- 预筛除：{len(snapshot.get('filtered', []))}", "",
             *_mode_sections(snapshot), "## 核心推荐", ""]
    for row in result["evaluations"]:
        if row["paper_id"] not in selected:
            continue
        paper = candidates[row["paper_id"]]
        lines += [f"### {paper['title']}", "",
                  f"- 作者：{_list(paper['authors'])}",
                  f"- 发表/上线：{_text(paper['publication_date'] or paper['online_date'])}",
                  f"- 期刊：{_text(paper['journal'])}",
                  f"- DOI：{_text(paper['doi'])}",
                  f"- 总评分：{row['overall_score']:.2f}",
                  f"- 推荐等级：{row['recommendation']}",
                  f"- 阅读优先级：{row['reading_priority']}", "",
                  f"**中文概括：** {_text(row['chinese_summary'])}", "",
                  f"**关键方法：** {_list(row['key_methods'])}", "",
                  f"**主要发现：** {_list(row['key_findings'])}", "",
                  f"**为什么相关：** {_text(row['recommendation_reason'])}", "",
                  f"**可借鉴：** {_text(row['direct_relevance'])}", "",
                  f"**局限：** {_text(row['limitations'])}", ""]
    if not selected:
        lines += ["没有论文达到推荐阈值。", ""]
    lines += ["## Monitor 与排除", ""]
    other = [row for row in result["evaluations"] if row["paper_id"] not in selected]
    lines += [f"- {candidates[row['paper_id']]['title']}（{row['overall_score']:.2f}；{row['recommendation']}）"
              for row in other] or ["无。"]
    if mode == "citation-expand" and snapshot.get("mode_context"):
        context = snapshot["mode_context"]
        lines += ["", "## Seed papers", ""]
        lines += [f"- {seed['title']}（DOI {_text(seed['doi'])}；paper_id {seed['paper_id']}）"
                  for seed in context.get("seed_papers", [])] or ["无。"]
        lines += ["", "## 多Seed共同连接论文", ""]
        lines += [f"- {candidates[item['paper_id']]['title']}：seed_count={item['seed_count']}；"
                  f"关系={_list(item['relations'])}；来源={_list(item['providers'])}"
                  for item in context.get("multi_seed", []) if item["paper_id"] in candidates] or ["无。"]
    if snapshot.get("lineage"):
        lines += ["", "## 元数据重评差异", "",
                  f"- 原运行：{snapshot['lineage']['source_run_id']}"]
        changes = json.loads((run_dir / "score-changes.json").read_text(encoding="utf-8"))
        lines += [f"- {item['paper_id']}：{item['old_score']:.2f} → {item['new_score']:.2f}；"
                  f"{item['old_recommendation']} → {item['new_recommendation']}"
                  for item in changes["changes"]]
    lines += ["", "_本报告基于人工评价与存储的元数据；不代表已阅读全文。_", ""]
    if output.exists():
        if output.read_text(encoding="utf-8") != "\n".join(lines):
            raise ValueError("Existing report differs; refusing to overwrite human edits")
    else:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.write_text("\n".join(lines), encoding="utf-8")
    return {"run_id": snapshot["run_id"], "path": str(output),
            "selected_count": len(selected), "status": "REPORT_READY"}
