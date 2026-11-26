"""Single-value Obsidian review controls and safe frontmatter edits."""
from __future__ import annotations

import json
import re

from literature_radar.delivery.cards import FRONT, frontmatter

DECISIONS = {"must_read": "很重要", "keep": "有用", "later": "一般", "discard": "没用"}
LEGACY = {"important": "must_read", "useful": "keep", "neutral": "later", "irrelevant": "discard"}
BUTTONS_BEGIN = "<!-- LITERATURE-RADAR-BUTTONS:BEGIN -->"
BUTTONS_END = "<!-- LITERATURE-RADAR-BUTTONS:END -->"


def review_buttons() -> str:
    return "\n".join((BUTTONS_BEGIN, "```literature-radar-feedback", "```", BUTTONS_END))


def set_properties(text: str, updates: dict[str, str], *, protect_identity: bool = True) -> str:
    """Replace scalar YAML Properties without touching the note body."""
    match = FRONT.match(text)
    if match is None:
        raise ValueError("review card lacks YAML Properties")
    protected = {"topic", "topic_id", "paper_key", "paper_id", "doi", "source_run"}
    if protect_identity and protected.intersection(updates):
        raise ValueError("identity Properties cannot be changed")
    lines = match.group(1).splitlines()
    seen = set()
    for index, line in enumerate(lines):
        key, sep, _ = line.partition(":")
        if sep and key in updates:
            if key in seen:
                raise ValueError("duplicate YAML Property")
            lines[index] = f"{key}: {json.dumps(str(updates[key]), ensure_ascii=False)}"
            seen.add(key)
    for key, value in updates.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            raise ValueError("invalid YAML Property name")
        if key not in seen:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    return "---\n" + "\n".join(lines) + "\n---\n" + text[match.end():]


def review_state(text: str) -> tuple[str, str]:
    meta = frontmatter(text)
    decision = meta.get("feedback_decision", "").strip()
    state = meta.get("review_status", "").strip()
    if not state:
        return ("reviewed" if decision in DECISIONS else "pending"), decision
    if state not in ("pending", "reviewed", "invalid"):
        return "invalid", decision
    if state == "reviewed" and decision not in DECISIONS:
        return "invalid", decision
    if state == "pending" and decision:
        return "invalid", decision
    return state, decision


def migrate_legacy_card(text: str, *, formal_decision: str | None = None,
                        feedback_updated_at: str = "") -> str:
    """Keep scientific content and notes; replace legacy checkboxes after auditing them."""
    if BUTTONS_BEGIN in text:
        return text
    if "## 我的评价" not in text or "## 我的笔记" not in text:
        raise ValueError("card lacks a distinct feedback and notes region")
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
    checked = re.findall(r"(?m)^- \[[xX]\] .+?<!-- feedback:(important|useful|neutral|irrelevant) -->\s*$", text)
    if len(checked) > 1:
        raise ValueError("multiple legacy feedback choices")
    legacy_decision = LEGACY[checked[0]] if checked else None
    if formal_decision is not None and formal_decision != legacy_decision:
        raise ValueError("formal Feedback conflicts with card choice")
    decision = formal_decision or legacy_decision or ""
    updates = {"paper_title": title, "feedback_decision": decision,
               "review_status": "reviewed" if decision else "pending",
               "feedback_updated_at": feedback_updated_at,
               "feedback_sync_status": "synced" if formal_decision else "not_synced",
               "feedback_synced_decision": formal_decision or ""}
    replaced = set_properties(text, updates)
    start = replaced.index("## 我的评价")
    end = replaced.index("## 我的笔记", start)
    provenance = "<!-- 历史评价已保留于 Stage 4.5.4 备份；原选择：" + (DECISIONS[decision] if decision else "未评价") + "。 -->"
    return replaced[:start] + "## 我的评价\n\n" + review_buttons() + "\n\n" + provenance + "\n\n" + replaced[end:]
