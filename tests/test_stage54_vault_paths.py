"""Temporary-Vault compatibility for old and dated Weekly paths."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literature_radar.delivery.cards import card_key, frontmatter, render_card, write_card
from literature_radar.delivery.obsidian import (bind_vault, deliver_cards, rename_delivered_cards,
                                                 status, _managed_card_location)
from literature_radar.delivery.review_ui import set_properties
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.store import load_feedback
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake


class Stage54VaultPathsTests(unittest.TestCase):
    def test_legacy_manifest_and_dated_weekly_share_dashboard_without_moves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            binding = root / "private" / "obsidian.json"
            bind_vault(vault, config=binding)
            archive = root / "archive"
            old = write_card(archive, {"paper_id": "old", "doi": "10.0000/old",
                                       "title": "Legacy fictional title"}, topic_id="fictional-alpha",
                             topic_name="Alpha", run_id="old-run", seen_on="2026-11-03", attention="HIGH")
            first = deliver_cards([old], section="Weekly", config=binding)
            self.assertEqual(first["new"], 1)
            manifest_path = binding.with_name("obsidian-delivery-manifest.json")
            manifest = json.loads(manifest_path.read_text())
            item_id = next(iter(manifest["cards"]))
            dated = vault / "Literature-Radar" / manifest["cards"][item_id]["relative_path"]
            self.assertEqual(dated.relative_to(vault / "Literature-Radar").parts[:2],
                             ("Weekly", "2026-11-03"))
            legacy_relative = "Weekly/fictional-alpha/HIGH/Legacy fictional title.md"
            legacy = vault / "Literature-Radar" / legacy_relative
            legacy.parent.mkdir(parents=True, exist_ok=True)
            dated.rename(legacy)  # Temporary Vault fixture; formal Vault is never moved.
            manifest["cards"][item_id]["relative_path"] = legacy_relative
            manifest_path.write_text(json.dumps(manifest))
            old.write_text(set_properties(old.read_text(), {"first_attention": "MEDIUM"}))
            legacy.write_text(set_properties(legacy.read_text(),
                                             {"feedback_decision": "keep", "review_status": "reviewed",
                                              "first_attention": "MEDIUM"})
                              + "\nHuman note survives.\n")
            # Existing formal cards may have been placed under their current
            # Attention level while keeping an earlier first_attention value.
            self.assertEqual(status(config=binding)["counts"]["Weekly"]["fictional-alpha"], 1)
            new = write_card(archive, {"paper_id": "new", "doi": "10.0000/new",
                                       "title": "New fictional title"}, topic_id="fictional-alpha",
                             topic_name="Alpha", run_id="new-run", seen_on="2026-11-10", attention="MEDIUM")
            result = deliver_cards([old, new], section="Weekly", config=binding)
            self.assertEqual((result["new"], result["failed"]), (1, 0))
            self.assertEqual(len(list((vault / "Literature-Radar/Weekly").rglob("*.md"))), 3)  # two cards + index
            self.assertTrue(legacy.is_file())
            self.assertIn("Human note survives.", legacy.read_text())
            self.assertEqual(frontmatter(legacy.read_text())["feedback_decision"], "keep")
            self.assertEqual(status(config=binding)["counts"]["Weekly"]["fictional-alpha"], 2)
            dashboard = (vault / "Literature-Radar/dashboard.md").read_text()
            self.assertIn("待阅读：1 篇", dashboard)
            self.assertIn("已阅读 / 已反馈：1 篇", dashboard)
            index = (vault / "Literature-Radar/Weekly/fictional-alpha/index.md").read_text()
            self.assertIn(legacy_relative.removesuffix(".md"), index)
            self.assertIn("Weekly/2026-11-10/fictional-alpha/MEDIUM", index)
            updated_source = write_card(archive, {"paper_id": "new", "doi": "10.0000/new",
                                                  "title": "New fictional title", "event": "MATERIAL_UPDATE"},
                                        topic_id="fictional-alpha", topic_name="Alpha", run_id="update-run",
                                        seen_on="2026-11-17", attention="HIGH")
            self.assertEqual(updated_source, new)
            changed = deliver_cards([new], section="Weekly", config=binding)
            self.assertEqual((changed["updated"], changed["failed"]), (1, 0))
            manifest = json.loads(manifest_path.read_text())
            new_item = next(item for item in manifest["cards"].values()
                            if item["paper_key"] == card_key("fictional-alpha", "new", "10.0000/new"))
            self.assertTrue(new_item["relative_path"].startswith("Weekly/2026-11-10/fictional-alpha/MEDIUM/"))
            self.assertEqual(frontmatter((vault / "Literature-Radar" / new_item["relative_path"]).read_text())
                             ["current_attention"], "HIGH")
            self.assertEqual(rename_delivered_cards(config=binding)["renamed"], 0)
            replay = deliver_cards([old, new], section="Weekly", config=binding)
            self.assertEqual((replay["new"], replay["failed"]), (0, 0))
            self.assertTrue(legacy.is_file())

    def test_dated_outbox_import_and_invalid_date_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            topic = "fictional-alpha"
            config = initialize(base / "private", intake(topic))
            paper_id, doi = "paper-1", "10.0000/paper-1"
            key = card_key(topic, paper_id, doi)
            relative = f"Weekly/2026-11-10/{topic}/HIGH/Fictional paper.md"
            card = base / "vault" / relative
            card.parent.mkdir(parents=True)
            card.write_text(set_properties(render_card({"paper_id": paper_id,
                                                        "title": "Fictional paper", "doi": doi},
                                                       topic_id=topic, topic_name="Alpha", run_id="weekly",
                                                       seen_on="2026-11-10", attention="HIGH"),
                                           {"feedback_decision": "keep", "review_status": "reviewed",
                                            "first_attention": "MEDIUM"}))
            outbox = base / "vault/.obsidian/plugins/literature-radar-bridge/outbox.json"
            outbox.parent.mkdir(parents=True)
            event = {"id": "dated-1", "topic_id": topic, "paper_key": key,
                     "paper_id": paper_id, "doi": doi, "decision": "keep",
                     "recorded_at": "2026-11-10T12:00:00+00:00", "path": relative}
            outbox.write_text(json.dumps({"version": 1, "events": [event]}))
            binding = {"delivery_root": str(base / "vault"), "vault_root": str(base / "vault")}
            paper = SimpleNamespace(paper_id=paper_id, doi=doi, identifiers={})
            store = SimpleNamespace(topic_papers=lambda _: [paper])
            with patch("literature_radar.feedback.bridge.AcademicStore", return_value=store), \
                    patch("literature_radar.feedback.bridge._read_binding", return_value=binding):
                first = sync_outbox(base / "private/obsidian.json", [config], base / "db.sqlite3")
                second = sync_outbox(base / "private/obsidian.json", [config], base / "db.sqlite3")
            self.assertEqual((first["status"], first["imported"], second["imported"]),
                             ("SUCCESS", 1, 0))
            self.assertEqual(len(load_feedback(config / "feedback.json")["entries"]), 1)
            self.assertEqual(frontmatter(card.read_text())["feedback_sync_status"], "synced")
            bad = {**event, "id": "bad-date", "path": f"Weekly/2026-02-30/{topic}/HIGH/Fictional paper.md"}
            outbox.write_text(json.dumps({"version": 1, "events": [event, bad]}))
            with patch("literature_radar.feedback.bridge.AcademicStore", return_value=store), \
                    patch("literature_radar.feedback.bridge._read_binding", return_value=binding):
                rejected = sync_outbox(base / "private/obsidian.json", [config], base / "db.sqlite3")
            self.assertEqual((rejected["status"], rejected["imported"]), ("PARTIAL", 0))
            self.assertEqual(len(load_feedback(config / "feedback.json")["entries"]), 1)
            self.assertIsNone(_managed_card_location(bad["path"]))


if __name__ == "__main__":
    unittest.main()
