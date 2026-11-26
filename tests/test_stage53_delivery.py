"""Isolated regressions for dated Weekly archives and scoped Vault delivery."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from literature_radar.cli import main as cli_main
from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_bundle
from literature_radar.delivery.cards import frontmatter, write_card
from literature_radar.delivery.obsidian import bind_vault, deliver_selected
from literature_radar.delivery.review_ui import set_properties
from literature_radar.topics.manager import initialize
from literature_radar.models import Paper
from literature_radar.workflow.multi_topic import run_weeklies
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from tests.test_stage37_39_workflow import RichProvider, intake, record


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


class Stage53DeliveryTests(unittest.TestCase):
    def test_old_unchanged_paper_does_not_refresh_archive_or_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            topic = "fictional-alpha"
            config = initialize(root / "private", intake(topic))
            activate_weekly(config, skip_bootstrap=True, reason="Fictional isolated test")
            vault = root / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            binding = root / "private/obsidian.json"
            bind_vault(vault, config=binding)
            archive = root / "weekly/cards"
            historical = write_card(archive, {
                "paper_id": "historical-identity", "doi": "10.0000/synthetic-1",
                "title": "Imaginaryus alpha signal mapping 1 with phase lattice assay"},
                topic_id=topic, topic_name=topic, run_id="historical-run",
                seen_on="2026-10-01", attention="MEDIUM")
            self.assertEqual(deliver_selected([historical], section="Weekly", config=binding)["new"], 1)
            vault_card = next(path for path in (vault / "Literature-Radar/Weekly").rglob("*.md")
                              if path.name != "index.md")
            archive_before, vault_before = historical.read_bytes(), vault_card.read_bytes()
            old = record(1, date="2024-08-15")
            result = run_weekly(config, root / "db.sqlite3", root / "run",
                                cards_dir=archive, provider_names=["openalex"],
                                providers={"openalex": RichProvider("openalex", [[old]])},
                                now=NOW, notification_preview=True)
            self.assertEqual(result["recommended_count"], 0)
            self.assertEqual(result["obsidian_delivery"]["new"], 0)
            self.assertEqual(result["obsidian_delivery"]["updated"], 0)
            self.assertEqual(historical.read_bytes(), archive_before)
            self.assertEqual(vault_card.read_bytes(), vault_before)

    def test_cross_species_method_requires_explicit_article_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = initialize(Path(temporary), intake("fictional-transfer"))
            paper = Paper("fictional-1", "Fictivus beta signal mapping",
                          "Fictivus beta signal mapping uses a phase lattice assay "
                          "to validate the transfer of state boundaries in a fictional sensor.",
                          "fixture")
            without_proof = recommend(paper, load_bundle(config))
            self.assertEqual(without_proof["attention_ranking"]["category"], "D")
            ontology = config / "ontology.json"
            content = json.loads(ontology.read_text())
            transfer = next(item for item in content["relations"] if item["type"] == "method_transfer")
            transfer["extensions"] = {"transfer_evidence_phrase": "validate the transfer of state boundaries"}
            ontology.write_text(json.dumps(content))
            with_proof = recommend(paper, load_bundle(config))
            self.assertEqual(with_proof["attention_ranking"]["category"], "B")
            generic = Paper("fictional-2", "Fictivus beta phase lattice assay",
                            "A phase lattice assay was used in an unrelated experiment.", "fixture")
            self.assertEqual(recommend(generic, load_bundle(config))["attention_ranking"]["category"], "D")

    def test_partial_cli_exit_is_three(self):
        with patch("literature_radar.cli.run_weeklies", return_value={"status": "PARTIAL", "topics": {}}):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli_main(["weekly", "run-many", "--config-dir", str(root),
                                               "--database", str(root / "db.sqlite3"),
                                               "--output-root", str(root / "reports")]), 3)

    def test_delivery_date_changes_only_on_real_push_and_keeps_original_archive_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = {"paper_id": "fictional-1", "doi": "10.0000/fictional-1",
                   "title": "Fictional long read study", "event": "NEW_PAPER"}
            kwargs = {"topic_id": "fictional-topic", "topic_name": "Fictional Topic"}
            first = write_card(root, row, run_id="run-1", seen_on="2026-11-03",
                               attention="MEDIUM", **kwargs)
            first.write_text(set_properties(first.read_text(),
                                            {"feedback_decision": "keep", "review_status": "reviewed"})
                             + "\nPrivate reading note.\n")
            unchanged = write_card(root, {**row, "event": "UNCHANGED"}, run_id="run-2",
                                   seen_on="2026-11-10", attention="HIGH", create=False, **kwargs)
            self.assertEqual(unchanged, first)
            meta = frontmatter(first.read_text())
            self.assertEqual(meta["delivery_date"], "2026-11-03")
            self.assertEqual(meta["feedback_decision"], "keep")
            self.assertIn("Private reading note.", first.read_text())
            pushed = write_card(root, {**row, "event": "MATERIAL_UPDATE"}, run_id="run-3",
                                seen_on="2026-11-10", attention="HIGH", **kwargs)
            self.assertEqual(pushed, first)
            self.assertEqual(first.relative_to(root).parts[:3],
                             ("2026-11-03", "fictional-topic", "MEDIUM"))
            self.assertEqual(frontmatter(first.read_text())["delivery_date"], "2026-11-10")
            self.assertIn("Private reading note.", first.read_text())
            self.assertEqual(len(list(root.rglob("*.md"))), 1)

    def test_shared_dated_archive_delivers_only_touched_cards_and_preserves_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            topic = "fictional-alpha"
            config = initialize(root / "private", intake(topic))
            other_topic = "fictional-beta"
            other_config = initialize(root / "private", intake(other_topic))
            activate_weekly(config, skip_bootstrap=True, reason="Fictional isolated test")
            activate_weekly(other_config, skip_bootstrap=True, reason="Fictional isolated test")
            vault = root / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            bind_vault(vault, config=root / "private" / "obsidian.json")
            archive = root / "weekly" / "cards"
            historical = write_card(archive, {"paper_id": "historical", "doi": "10.0000/historical",
                                              "title": "Unrelated historical card"},
                                    topic_id=topic, topic_name=topic, run_id="old",
                                    seen_on="2026-10-27", attention="LOW")
            result = run_weeklies([config, other_config], root / "db.sqlite3", root / "runs" / "first",
                                  cards_root=archive, provider_names=["openalex"],
                                  providers_by_topic={topic: {"openalex": RichProvider("openalex", [[record(1)]])},
                                                      other_topic: {"openalex": RichProvider("openalex", [[record(2)]])}},
                                  now=NOW, notification_preview=True)
            report = result["topics"][topic]
            self.assertEqual((result["status"], report["recommended_count"]), ("SUCCESS", 1))
            self.assertEqual(result["topics"][other_topic]["recommended_count"], 1)
            self.assertEqual((report["obsidian_delivery"]["new"], report["obsidian_delivery"]["failed"]), (1, 0))
            self.assertTrue(historical.is_file())
            self.assertEqual(len(list(archive.rglob("*.md"))), 3)
            card = Path(report["cards"][0])
            self.assertEqual(card.relative_to(archive).parts[:3], ("2026-11-03", topic, "HIGH"))
            self.assertEqual(frontmatter(card.read_text())["delivery_date"], "2026-11-03")
            delivered = next(path for path in (vault / "Literature-Radar" / "Weekly" / "2026-11-03" / topic).rglob("*.md")
                             if path.name != "index.md")
            self.assertNotIn("Unrelated historical card", delivered.read_text())
            delivered.write_text(set_properties(delivered.read_text(),
                                                {"feedback_decision": "keep", "review_status": "reviewed"})
                                 + "\nMy private note.\n")
            unchanged_vault_bytes = delivered.read_bytes()
            unchanged_archive_bytes = card.read_bytes()

            # The unprocessed event ledger may replay an eligible card after a
            # crash; its delivery is byte-identical and never sweeps other cards.
            replay = run_weeklies([config, other_config], root / "db.sqlite3", root / "runs" / "replay",
                                  cards_root=archive, provider_names=["openalex"],
                                  providers_by_topic={topic: {"openalex": RichProvider("openalex", [[]])},
                                                      other_topic: {"openalex": RichProvider("openalex", [[]])}},
                                  now=NOW, force=True, notification_preview=True)
            delivery = replay["topics"][topic]["obsidian_delivery"]
            self.assertEqual((delivery["new"], delivery["updated"], delivery["unchanged"],
                              delivery["failed"]), (0, 0, 1, 0))
            self.assertEqual(len(list(archive.rglob("*.md"))), 3)
            self.assertEqual(delivered.read_bytes(), unchanged_vault_bytes)
            self.assertEqual(card.read_bytes(), unchanged_archive_bytes)
            self.assertEqual(frontmatter(delivered.read_text())["feedback_decision"], "keep")
            self.assertIn("My private note.", delivered.read_text())


if __name__ == "__main__":
    unittest.main()
