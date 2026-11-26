"""Dashboard uses card state immediately and formal feedback per Topic."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literature_radar.delivery.cards import card_key, render_card
from literature_radar.delivery.obsidian import _dashboard
from literature_radar.delivery.review_ui import set_properties
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.review import _atomic_json
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake


class Stage46DashboardTests(unittest.TestCase):
    def test_outbox_refuses_mixed_topic_formal_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            topic = "fictional-alpha"
            config = initialize(base / "config", intake(topic))
            paper_id, doi = "paper-alpha", "10.0000/alpha"
            key = card_key(topic, paper_id, doi)
            card = base / "vault" / "Bootstrap" / topic / "HIGH" / "Alpha paper.md"
            card.parent.mkdir(parents=True)
            card.write_text(render_card({"paper_id": paper_id, "title": "Alpha paper", "doi": doi},
                                        topic_id=topic, topic_name="Alpha", run_id="test",
                                        seen_on="2026-11-06", attention="HIGH"), encoding="utf-8")
            event = {"id": "event-1", "topic_id": topic, "paper_key": key,
                     "paper_id": paper_id, "doi": doi, "decision": "keep",
                     "recorded_at": "2026-11-06T00:00:00+00:00",
                     "path": str(card.relative_to(base / "vault"))}
            outbox = base / "vault/.obsidian/plugins/literature-radar-bridge/outbox.json"
            outbox.parent.mkdir(parents=True)
            outbox.write_text(json.dumps({"version": 1, "events": [event]}), encoding="utf-8")
            foreign = {"topic_id": "fictional-beta", "paper_key": "fictional-beta|id:1",
                       "decision": "keep", "reason": "human", "recorded_at": "2026-11-05T00:00:00+00:00"}
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": [foreign]})
            paper = SimpleNamespace(paper_id=paper_id, doi=doi, identifiers={})
            store = SimpleNamespace(topic_papers=lambda _: [paper])
            binding = {"delivery_root": str(base / "vault"), "vault_root": str(base / "vault")}
            with patch("literature_radar.feedback.bridge.AcademicStore", return_value=store), \
                    patch("literature_radar.feedback.bridge._read_binding", return_value=binding):
                result = sync_outbox(base / "config/obsidian.json", [config], base / "db.sqlite3")
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["imported"], 0)
            self.assertIn("another Topic", result["failures"][0]["error"])
            self.assertEqual(json.loads((config / "feedback.json").read_text())["entries"], [foreign])
            collision = {"topic_id": topic, "paper_key": f"{topic}|id:other",
                         "decision": "keep", "reason": "human", "recorded_at": "2026-11-05T00:00:00+00:00",
                         "extensions": {"bridge_event_id": "event-1"}}
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": [collision]})
            with patch("literature_radar.feedback.bridge.AcademicStore", return_value=store), \
                    patch("literature_radar.feedback.bridge._read_binding", return_value=binding):
                collision_result = sync_outbox(base / "config/obsidian.json", [config], base / "db.sqlite3")
            self.assertEqual(collision_result["status"], "PARTIAL")
            self.assertEqual(collision_result["imported"], 0)
            self.assertIn("event ID conflicts", collision_result["failures"][0]["error"])

    def test_one_card_change_updates_only_its_topic_without_batch_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "vault"
            config_root = base / "config"
            alpha = "fictional-alpha"
            beta = "fictional-beta"
            initialize(config_root, intake(alpha))
            initialize(config_root, intake(beta))
            manifest = {"cards": {}}
            cards = {}
            for topic in (alpha, beta):
                paper_id = f"paper-{topic}"
                doi = f"10.0000/{topic}"
                key = card_key(topic, paper_id, doi)
                relative = f"Bootstrap/{topic}/HIGH/{topic} paper.md"
                path = vault / relative
                path.parent.mkdir(parents=True)
                path.write_text(render_card({"paper_id": paper_id, "title": f"{topic} paper", "doi": doi},
                                            topic_id=topic, topic_name=topic, run_id="test",
                                            seen_on="2026-11-06", attention="HIGH"), encoding="utf-8")
                manifest["cards"][f"Bootstrap|{key}"] = {
                    "relative_path": relative, "paper_key": key, "title": f"{topic} paper"}
                cards[topic] = path

            def dashboard() -> str:
                return _dashboard(vault, manifest, prefix="", config=config_root / "obsidian.json")

            before = dashboard()
            self.assertIn("待评价 1；已评价 0", before)
            self.assertIn("总计：2 篇", before)
            cards[alpha].write_text(set_properties(cards[alpha].read_text(encoding="utf-8"), {
                "feedback_decision": "must_read", "review_status": "reviewed",
                "feedback_sync_status": "pending"}), encoding="utf-8")
            after = dashboard()
            alpha_block = after.split(f"Bootstrap/{alpha}/index", 1)[1].split(f"Bootstrap/{beta}/index", 1)[0]
            beta_block = after.split(f"Bootstrap/{beta}/index", 1)[1].split("总计：", 1)[0]
            self.assertIn("待评价 0；已评价 1", alpha_block)
            self.assertIn("很重要 1", alpha_block)
            self.assertIn("待评价 1；已评价 0", beta_block)
            self.assertIn("很重要 0", beta_block)
            self.assertEqual(dashboard(), after)  # reopening leaves the same persisted state


if __name__ == "__main__":
    unittest.main()
