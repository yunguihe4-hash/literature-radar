"""Topic-scoped Obsidian review cards with a preserved human evaluation area."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

MARKERS = {"important": "很重要", "useful": "有用", "neutral": "一般", "irrelevant": "没用"}
START = "<!-- AUTO-GENERATED:BEGIN -->"
END = "<!-- AUTO-GENERATED:END -->"
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def card_key(topic_id: str, paper_id: str, doi: str | None) -> str:
    return f"{topic_id}|doi:{doi.casefold().removeprefix('https://doi.org/')}" if doi else f"{topic_id}|id:{paper_id}"


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try: return str(json.loads(value))
        except (ValueError, TypeError): pass
    return value


def frontmatter(text: str) -> dict[str, str]:
    match = FRONT.match(text)
    if not match:
        raise ValueError("review card lacks YAML Properties")
    result = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            result[key] = _scalar(value)
    return result


def _plain(value) -> str:
    return " ".join(str(value or "").split()).replace("<", "&lt;").replace(">", "&gt;")


def _human_section(previous: str) -> str:
    marker = "## 我的评价"
    if marker in previous:
        return previous[previous.index(marker):].rstrip() + "\n"
    from literature_radar.delivery.review_ui import review_buttons
    return "## 我的评价\n\n" + review_buttons() + "\n\n## 我的笔记\n"


def _push_history(old: dict[str, str]) -> list[dict[str, str]]:
    encoded = old.get("push_history")
    if encoded:
        try:
            value = json.loads(encoded)
            if isinstance(value, list) and all(isinstance(item, dict) and
                                               isinstance(item.get("run_id"), str) and
                                               isinstance(item.get("date"), str) for item in value):
                return value
        except (ValueError, TypeError):
            pass
        raise ValueError("existing card push_history is invalid")
    if old and old.get("first_recommended"):
        return [{"run_id": old.get("source_run") or "legacy",
                 "date": old["first_recommended"], "event": "PRE_STAGE46"}]
    return []


def render_card(row: dict, *, topic_id: str, topic_name: str, run_id: str,
                seen_on: str, attention: str, previous: str = "",
                pushed: bool = True) -> str:
    if attention not in {"HIGH", "MEDIUM", "LOW", "BOOTSTRAP_SEED"}:
        raise ValueError("invalid Attention level")
    seed = attention == "BOOTSTRAP_SEED"
    old = frontmatter(previous) if previous else {}
    paper_id = str(row["paper_id"])
    doi = str(row.get("doi") or "").strip()
    key = card_key(topic_id, paper_id, doi)
    if old and (old.get("topic_id") != topic_id or old.get("paper_key") != key or old.get("paper_id") != paper_id):
        raise ValueError("existing card identity conflicts with current paper")
    first_seen = old.get("first_seen") or str(row.get("first_seen") or seen_on)[:10]
    first_recommended = old.get("first_recommended") or old.get("first_recommended_date") or seen_on
    first_attention = old.get("first_attention") or attention
    history = _push_history(old)
    if pushed and not any(item["run_id"] == run_id for item in history):
        history.append({"run_id": run_id, "date": seen_on,
                        "event": str(row.get("event") or ("BOOTSTRAP_SEED" if seed else "RECOMMENDED"))})
    last_pushed = history[-1]["date"] if history else ""
    props = {
        "type": "literature-review-card", "card_kind": "bootstrap_seed" if seed else "weekly_recommendation",
        "topic": topic_id, "topic_id": topic_id,
        "topic_display_name": topic_name, "paper_key": key, "paper_id": paper_id,
        "doi": doi, "source_run": run_id, "first_seen": first_seen,
        "publication_date": str(row.get("publication_date") or row.get("date") or ""),
        "online_date": str(row.get("online_date") or ""),
        "date_source": str(row.get("date_source") or "unverified"),
        "search_window_start": str(row.get("search_window_start") or ""),
        "search_window_end": str(row.get("search_window_end") or ""),
        "delivery_date": seen_on if pushed else old.get("delivery_date") or first_recommended,
        "first_recommended": first_recommended,
        "first_recommended_date": first_recommended,
        "first_attention": first_attention, "last_seen": seen_on,
        "last_pushed": last_pushed, "push_count": len(history),
        "push_history": json.dumps(history, ensure_ascii=False, separators=(",", ":")),
        "current_attention": attention, "attention": attention,
        "relevance_category": str(row.get("relevance") or old.get("relevance_category") or ""),
        "recommendation_status": str(row.get("recommendation_status") or
                                     ("bootstrap_seed" if seed else "active" if pushed else old.get("recommendation_status") or "active")),
        "paper_title": str(row.get("title") or ""),
        "feedback_decision": old.get("feedback_decision", ""),
        "review_status": old.get("review_status", "pending"),
        "feedback_updated_at": old.get("feedback_updated_at", ""),
        "feedback_sync_status": old.get("feedback_sync_status", "not_synced"),
        "feedback_synced_decision": old.get("feedback_synced_decision", ""),
    }
    header = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in props.items()) + "\n---\n\n"
    evidence = row.get("evidence") or row.get("transfer_explanation") or ""
    if isinstance(evidence, (dict, list)):
        evidence = json.dumps(evidence, ensure_ascii=False)
    lines = [START, f"# {_plain(row.get('title'))}", "",
             f"- 研究主题：{_plain(topic_name)}",
             "- 类型：初次调研种子文献" if seed else f"- Attention：{attention}",
             f"- 发表时间：{_plain(row.get('date') or row.get('publication_date'))}",
             f"- 发表时间精度：{_plain(row.get('date_precision') or '未核实')}",
             f"- DOI：{_plain(doi)}", f"- 原文链接：{_plain(row.get('url') or row.get('landing_url') or ('https://doi.org/'+doi if doi else ''))}",
             "", "## 摘要", "", _plain(row.get("abstract")) or "未提供摘要。",
             "", "## 为什么推荐", "", _plain(row.get("why_recommended")) or "待核对推荐理由。",
             "", "## 研究问题", "", _plain(row.get("research_question")) or "待核对。",
             "", "## 方法学", "", _plain(row.get("methodology") or row.get("transfer_type")) or "待核对。",
             "", "## 相关性证据", "", _plain(evidence) or "待核对。",
             "", "## 初次调研种子" if seed else "## Attention 判定", "",
             ("用于建立初始科研阅读基础；人工评价另行记录。" if seed else
              _plain(row.get("attention_reason")) or
              ("LOW：达到相关性门槛，但证据或评分尚不足以提高提醒等级；可在下方反馈。" if attention == "LOW"
               else f"{attention}：按当前 Topic 的 Attention 规则判定；可在下方反馈。")),
             "", "## 全文状态", "", _plain(row.get("fulltext_status")) or "未核验全文。",
             "", END, "", _human_section(previous)]
    return header + "\n".join(lines).rstrip() + "\n"


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".review-card-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _readable_name(title: str, key: str, directory: Path) -> str:
    """Keep the title visible; use a short identity suffix only for a collision."""
    value = unicodedata.normalize("NFC", str(title or ""))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = " ".join(value.split()).strip(" .") or "Untitled paper"
    while len(value.encode("utf-8")) > 180:
        value = value[:-1].rstrip(" .")
    candidate = value + ".md"
    if not (directory / candidate).exists():
        return candidate
    digest = hashlib.sha256(key.encode()).hexdigest()
    for length in (8, 12, 16):
        candidate = f"{value} ({digest[:length]}).md"
        if not (directory / candidate).exists():
            return candidate
    raise ValueError("too many card title collisions")


def write_card(root: Path, row: dict, *, topic_id: str, topic_name: str,
               run_id: str, seen_on: str, attention: str,
               create: bool = True) -> Path | None:
    """Update one stable card in place; create date/Topic/level folders only on first write."""
    date.fromisoformat(seen_on[:10])
    root = Path(root)
    if root.is_symlink():
        raise ValueError("card root cannot be a symlink")
    key = card_key(topic_id, str(row["paper_id"]), row.get("doi"))
    existing = None
    if root.exists():
        for path in root.rglob("*.md"):
            if path.is_symlink(): continue
            try: meta = frontmatter(path.read_text(encoding="utf-8"))
            except ValueError: continue
            if meta.get("paper_key") == key:
                if meta.get("topic_id") != topic_id or path.parent.parent.name != topic_id:
                    raise ValueError("card Topic property and directory disagree")
                if existing is not None: raise ValueError("duplicate card identity in vault")
                existing = path
    if existing is None:
        if not create:
            return None
        directory = root / seen_on[:10] / topic_id / attention
        name = _readable_name(str(row.get("title") or ""), key, directory)
        path = directory / name
        previous = ""
    else:
        path = existing
        previous = path.read_text(encoding="utf-8")
    if path.is_symlink() or (root.exists() and not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("card path escapes root")
    content = render_card(row, topic_id=topic_id, topic_name=topic_name,
                          run_id=run_id, seen_on=seen_on[:10], attention=attention,
                          previous=previous, pushed=create)
    if content != previous:
        _atomic(path, content)
    return path
