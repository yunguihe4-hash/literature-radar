"""Bootstrap seed and real human review stay separate in a temporary Vault."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.attention.calibration import train_due
from literature_radar.delivery.cards import frontmatter
from literature_radar.delivery.obsidian import create_dedicated_vault, deliver_cards
from literature_radar.delivery.review_ui import set_properties
from literature_radar.feedback.batches import batch_progress
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.topics.manager import initialize
from literature_radar.workflow.bootstrap import run_bootstrap
from tests.test_stage37_39_workflow import RichProvider, intake, record


class BootstrapSeedIsolation(unittest.TestCase):
    def test_trainer_uses_twenty_human_reviews_while_memory_batch_uses_thirty(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = initialize(Path(temporary) / "config", intake("fictional-training"))
            entries = [{"topic_id": "fictional-training", "paper_key": f"fictional-training|id:{i}",
                        "decision": "keep" if i % 2 else "discard", "reason": "human review",
                        "recorded_at": f"2026-11-05T00:{i:02d}:00+00:00",
                        "extensions": {"source": "obsidian_bridge"}}
                       for i in range(30)]
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries[:19]})
            self.assertEqual(train_due(config)["status"], "NOT_ELIGIBLE")
            self.assertEqual(batch_progress(config)["valid_feedback"], 19)
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries[:20]})
            self.assertEqual(train_due(config)["status"], "DATA_NOT_REPLAYABLE")
            self.assertEqual(batch_progress(config)["valid_feedback"], 20)
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries})
            self.assertEqual(batch_progress(config)["valid_feedback"], 30)
            result = train_due(config)
            self.assertEqual(result["status"], "DATA_NOT_REPLAYABLE")
            self.assertIsNone(result["active_version"])

    def test_seed_is_neutral_until_explicit_human_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-seed"))
            database = root / "academic.sqlite3"
            output = root / "bootstrap-output"
            result = run_bootstrap(config, database, output, provider_names=["openalex"],
                                   providers={"openalex": RichProvider("openalex", [[record(1)]])},
                                   now=datetime(2026, 11, 5, tzinfo=timezone.utc), limit=1)
            self.assertEqual(result["selected"], 1)
            self.assertEqual(batch_progress(config)["valid_feedback"], 0)
            self.assertEqual(train_due(config)["status"], "NOT_ELIGIBLE")
            source = next((output / "cards").rglob("*.md"))
            self.assertEqual(source.parent.name, "BOOTSTRAP_SEED")
            self.assertEqual(frontmatter(source.read_text())["card_kind"], "bootstrap_seed")

            parent = root / "vaults"
            parent.mkdir()
            binding = root / "config/obsidian.json"
            setup = create_dedicated_vault(parent, config=binding,
                                           topic_ids=("fictional-seed",), registered=[])
            vault = Path(setup["vault_root"])
            self.assertEqual(deliver_cards([source], section="Bootstrap", config=binding)["new"], 1)
            card = next((vault / "Bootstrap/fictional-seed/BOOTSTRAP_SEED").glob("*.md"))
            dashboard = (vault / "dashboard.md").read_text()
            self.assertIn("初次调研种子 1", dashboard)
            self.assertNotIn("HIGH", frontmatter(card.read_text())["current_attention"])

            card.write_text(set_properties(card.read_text(), {
                "feedback_decision": "must_read", "review_status": "reviewed"}))
            meta = frontmatter(card.read_text())
            event = {"id": "human-seed-review", "topic_id": "fictional-seed",
                     "paper_key": meta["paper_key"], "paper_id": meta["paper_id"],
                     "doi": meta["doi"], "decision": "must_read",
                     "recorded_at": "2026-11-06T00:00:00+00:00",
                     "path": str(card.relative_to(vault))}
            outbox = vault / ".obsidian/plugins/literature-radar-bridge/outbox.json"
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(json.dumps({"version": 1, "events": [event]}))
            self.assertEqual(sync_outbox(binding, [config], database)["imported"], 1)
            self.assertEqual(sync_outbox(binding, [config], database)["imported"], 0)
            self.assertEqual(len(load_feedback(config / "feedback.json")["entries"]), 1)
            self.assertEqual(batch_progress(config)["valid_feedback"], 1)
            self.assertEqual(train_due(config)["status"], "NOT_ELIGIBLE")


if __name__ == "__main__":
    unittest.main()
