"""Dedicated Vault and real Markdown feedback importer using fictional papers only."""
from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from literature_radar.delivery.cards import frontmatter
from literature_radar.delivery.review_ui import set_properties
from literature_radar.cli import main as cli_main
from literature_radar.delivery.obsidian import (create_dedicated_vault, deliver_cards,
                                                 doctor, migrate_staged, status)
from literature_radar.feedback.cards import import_cards
from literature_radar.feedback.store import load_feedback
from literature_radar.topics.manager import initialize
from literature_radar.workflow.multi_topic import run_bootstraps
from tests.test_stage37_39_workflow import RichProvider, intake, record
from datetime import datetime, timezone


class Stage453DeliveryTests(unittest.TestCase):
    def test_dedicated_layout_metadata_and_four_feedback_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_a = initialize(root / "config", intake("fictional-alpha"))
            config_b = initialize(root / "config", intake("fictional-beta"))
            db = root / "academic.sqlite3"
            records_a = [record(i, doi=f"10.0000/alpha-{i}") for i in range(4)]
            records_b = [record(i + 10, doi=f"10.0000/beta-{i}") for i in range(4)]
            result = run_bootstraps([config_a, config_b], db, root / "reports",
                provider_names=["openalex"], now=datetime(2026, 11, 5, tzinfo=timezone.utc),
                providers_by_topic={"fictional-alpha": {"openalex": RichProvider("openalex", [records_a])},
                                    "fictional-beta": {"openalex": RichProvider("openalex", [records_b])}},
                limit=4)
            self.assertEqual([result["topics"][topic]["selected"] for topic in
                              ("fictional-alpha", "fictional-beta")], [4, 4])
            parent = root / "vaults"
            parent.mkdir()
            private_binding = root / "private" / "obsidian.json"
            setup = create_dedicated_vault(parent, config=private_binding,
                topic_ids=("fictional-alpha", "fictional-beta"), registered=[])
            vault = Path(setup["vault_root"])
            migrated = migrate_staged([root / "reports/fictional-alpha", root / "reports/fictional-beta"],
                config=private_binding, expected_per_topic=4, confirmed=True, database=db)
            self.assertEqual(migrated["status"], "SUCCESS")
            self.assertEqual(migrated["delivery"]["new"], 8)
            self.assertEqual(doctor(config=private_binding)["status"], "PASS")
            self.assertEqual(status(config=private_binding)["counts"]["Bootstrap"],
                             {"fictional-alpha": 4, "fictional-beta": 4})
            cards_a = sorted((vault / "Bootstrap/fictional-alpha").glob("*/*.md"))
            self.assertEqual(len(cards_a), 4)
            self.assertTrue(all(frontmatter(path.read_text())["topic_id"] == "fictional-alpha"
                                and "## 文献元数据" in path.read_text()
                                and "期刊：Journal of Fictional Systems" in path.read_text()
                                for path in cards_a))
            for path, decision in zip(cards_a, ("must_read", "keep", "later", "discard")):
                path.write_text(set_properties(path.read_text(), {"feedback_decision": decision, "review_status": "reviewed"}))
            imported = import_cards(config_a, vault / "Bootstrap/fictional-alpha", db)
            self.assertEqual(imported["imported"], 4)
            self.assertEqual(import_cards(config_a, vault / "Bootstrap/fictional-alpha", db)["imported"], 0)
            self.assertEqual({row["decision"] for row in load_feedback(config_a / "feedback.json")["entries"]},
                             {"must_read", "keep", "later", "discard"})
            self.assertEqual(load_feedback(config_b / "feedback.json")["entries"], [])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main(["feedback", "import-cards", "--config-dir", str(config_a),
                                           "--cards-dir", str(vault / "Bootstrap/fictional-alpha"),
                                           "--database", str(db), "--binding-file", str(private_binding)]), 0)
            self.assertEqual(json.loads(output.getvalue())["dashboard_refresh"], "SUCCESS")
            dashboard = (vault / "dashboard.md").read_text()
            self.assertIn("总计：8 篇", dashboard)
            self.assertIn("已阅读 / 已反馈：4 篇", dashboard)
            self.assertIn("尚未启用（0 篇）", dashboard)
            self.assertEqual(len(list((vault / "Weekly").glob("*/*/*.md"))), 0)
