"""Obsidian Bases views scoped to one Topic and the Bootstrap directory."""
from __future__ import annotations

import json


def bootstrap_base(topic_id: str, *, delivery_prefix: str = "") -> str:
    folder = "/".join(part for part in (delivery_prefix, "Bootstrap", topic_id) if part)
    quoted_folder = json.dumps(folder, ensure_ascii=False)
    quoted_topic = json.dumps(topic_id, ensure_ascii=False)
    lines = ["filters:", "  and:", f"    - 'file.inFolder({quoted_folder})'",
             f"    - 'note.topic_id == {quoted_topic}'",
             '    - \'note.type == "literature-review-card"\'',
             "properties:", "  file.name:", "    displayName: 论文标题",
             "  note.attention:", "    displayName: Attention",
             "  note.review_status:", "    displayName: 阅读状态",
             "  note.feedback_decision:", "    displayName: 我的评价",
             "  note.feedback_updated_at:", "    displayName: 评价时间",
             "  note.doi:", "    displayName: DOI", "views:"]
    for name, condition in (("全部文献", None), ("待评价", "pending"),
                            ("已评价", "reviewed"), ("反馈异常", "invalid")):
        lines.extend(["  - type: table", f"    name: {name}", "    groupBy:",
                      "      property: note.review_status", "      direction: ASC"])
        if condition is not None:
            lines.extend(["    filters:", f"      and: ['note.review_status == {json.dumps(condition)}']"])
        lines.extend(["    order:", "      - file.name", "      - note.attention",
                      "      - note.review_status", "      - note.feedback_decision",
                      "      - note.feedback_updated_at", "      - note.doi", "      - note.paper_id",
                      "    summaries:", "      note.paper_id: Filled"])
    return "\n".join(lines) + "\n"
