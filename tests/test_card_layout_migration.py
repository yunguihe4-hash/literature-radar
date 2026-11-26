"""Focused card layout and offline migration checks."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature_radar.delivery.cards import END, START, frontmatter, render_card
from literature_radar.delivery.card_migration import migrate


class CardLayoutMigrationTests(unittest.TestCase):
    def test_evidenced_card_layout_and_date_precision(self):
        row = {"paper_id": "p1", "title": "Plant Y study", "doi": "10.1000/plant",
               "journal": "Horticulture Research", "publication_date": "2026-11-20",
               "date_precision": "day", "abstract": "We generated chromosome-level assemblies.",
               "research_question_label": "Resolve Y structure", "research_phase": "Y assembly",
               "relevance": "C", "fulltext_status": "NOT_VERIFIED"}
        card = render_card(row, topic_id="plant-y", topic_name="Plant Y", run_id="run-1",
                           seen_on="2026-11-22", attention="MEDIUM")
        self.assertIn("- Journal：Horticulture Research", card)
        self.assertEqual(card.count("## 为什么推给我"), 1)
        self.assertIn("- 可迁移方法：摘要明确报告：We generated chromosome-level assemblies.", card)
        for old in ("发表时间精度：", "## 方法学", "## 相关性证据", "## Attention 判定", "## 全文状态", "NOT_VERIFIED"):
            self.assertNotIn(old, card)
        self.assertEqual(row["date_precision"], "day")
        self.assertEqual(frontmatter(card)["topic"], "Plant Y")
        self.assertIn("- 迁移价值：LOW 保底推荐；", render_card(
            row, topic_id="plant-y", topic_name="Plant Y", run_id="run-1",
            seen_on="2026-11-22", attention="LOW"))

    def test_temp_vault_preserves_notes_feedback_identity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            topic = "plant-y"
            path = vault / "Weekly/2026-11-22/plant-y/LOW/Plant Y study.md"
            path.parent.mkdir(parents=True)
            old = render_card({"paper_id": "p1", "title": "Plant Y study", "doi": "10.1000/plant",
                               "abstract": "We generated chromosome-level assemblies."},
                              topic_id=topic, topic_name="Plant Y", run_id="run-1",
                              seen_on="2026-11-22", attention="LOW")
            old = old.replace(END, '## 研究问题\n\ny-structure\n\n## 相关性证据\n\n{"field":"abstract","excerpt":"Y chromosome evidence"}\n\n' + END)
            old = old.replace("## 我的笔记\n", "## 我的笔记\n人工原始笔记。\n")
            old = old.replace('"feedback_decision": ""', '"feedback_decision": "keep"')
            old = old.replace('"review_status": "pending"', '"review_status": "reviewed"')
            path.write_text(old, encoding="utf-8")
            archive = root / "archive"
            archive_card = archive / "other-name.md"
            archive.mkdir()
            archive_card.write_text(old, encoding="utf-8")
            db = root / "paper.sqlite3"
            connection = sqlite3.connect(db)
            connection.execute("create table papers_v2 (paper_id text, payload text)")
            connection.execute("insert into papers_v2 values (?, ?)", ("p1", json.dumps({
                "paper_id": "p1", "title": "Plant Y study", "doi": "10.1000/plant",
                "abstract": "We generated chromosome-level assemblies.",
                "publication_date": "2026-11-20", "journal": "Horticulture Research"})))
            connection.commit(); connection.close()
            config = root / "config" / topic
            config.mkdir(parents=True)
            for name, value in {"topic": {"id": topic, "display_name": "Plant Y"},
                                "profile": {"questions": [{"id": "y-structure", "label": "Resolve Y structure",
                                                           "aliases": ["Y chromosome"]}]},
                                "phase": {"priority_question_ids": ["y-structure"]}}.items():
                (config / f"{name}.json").write_text(json.dumps(value))
            args = (vault, db, root / "config", archive)
            self.assertEqual(migrate(*args, None)["vault_changed"], 1)
            result = migrate(*args, root / "backup", apply=True)
            self.assertEqual((result["vault_changed"], result["archive_changed"]), (1, 1))
            new = path.read_text()
            self.assertEqual(new[new.index(END) + len(END):], old[old.index(END) + len(END):])
            for field in ("doi", "paper_key", "paper_id", "push_history", "delivery_date", "feedback_decision"):
                self.assertEqual(frontmatter(new)[field], frontmatter(old)[field])
            self.assertEqual(frontmatter(new)["current_attention"], "LOW")
            self.assertEqual(migrate(*args, None)["vault_changed"], 0)
            self.assertIn("Horticulture Research", archive_card.read_text())
            self.assertEqual(new.count(START), 1)


if __name__ == "__main__":
    unittest.main()
