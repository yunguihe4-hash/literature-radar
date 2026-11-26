"""Stage 4.5.4 isolated review, Bridge, title and batch regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.delivery.bases import bootstrap_base
from literature_radar.delivery.cards import frontmatter, render_card
from literature_radar.delivery.obsidian import create_dedicated_vault, deliver_cards, rename_delivered_cards
from literature_radar.delivery.review_ui import migrate_legacy_card, review_buttons, set_properties
from literature_radar.feedback.batches import batch_progress, prepare_due_batches
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.review import _atomic_json
from literature_radar.feedback.store import load_feedback
from literature_radar.topics.manager import initialize
from literature_radar.workflow.multi_topic import run_bootstraps
from tests.test_stage37_39_workflow import RichProvider, intake, record


class Stage454ReviewTests(unittest.TestCase):
    def test_buttons_migrate_legacy_and_base_scope(self):
        raw = render_card({"paper_id": "a", "title": "A / title", "doi": "10.0000/a"},
                          topic_id="fictional-alpha", topic_name="Alpha", run_id="test",
                          seen_on="2026-11-05", attention="HIGH")
        self.assertEqual(review_buttons().count("```literature-radar-feedback"), 1)
        legacy = raw.replace("review_status: \"pending\"", "review_status: \"pending\"")
        legacy = legacy.replace("## 我的评价\n\n" + review_buttons(),
                                "## 我的评价\n\n- [x] 很重要 <!-- feedback:important -->")
        legacy += "\nMy private note\n"
        migrated = migrate_legacy_card(legacy, formal_decision="must_read")
        self.assertEqual(frontmatter(migrated)["feedback_decision"], "must_read")
        self.assertIn("My private note", migrated)
        self.assertIn("```literature-radar-feedback", migrated)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            migrate_legacy_card(legacy, formal_decision="discard")
        base = bootstrap_base("fictional-alpha")
        self.assertIn('file.inFolder("Bootstrap/fictional-alpha")', base)
        self.assertEqual(base.count("    name:"), 4)
        self.assertEqual(base.count("note.paper_id: Filled"), 4)

    def test_topic_batches_30_60_90_without_auto_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = initialize(Path(temporary) / "config", intake("fictional-alpha"))
            self.assertEqual(batch_progress(config)["next_threshold"], 30)
            for count, expected in ((29, 0), (30, 1), (60, 2), (90, 3)):
                entries = [{"topic_id": "fictional-alpha", "paper_key": f"fictional-alpha|id:{i}",
                            "decision": "keep" if i % 2 else "must_read", "reason": "human",
                            "recorded_at": f"2026-11-05T00:{i//60:02d}:{i%60:02d}+00:00"}
                           for i in range(count)]
                _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries})
                outcome = prepare_due_batches(config)
                self.assertEqual(outcome["prepared_batches"], expected)
                self.assertEqual(len(outcome["created_proposals"]), expected - (0 if count == 29 else
                    {30: 0, 60: 1, 90: 2}[count]))
            self.assertEqual(prepare_due_batches(config)["created_proposals"], [])
            for number in (1, 2, 3):
                proposal = json.loads((config / f"attention-batch-{number:03d}.json").read_text())
                self.assertEqual(proposal["threshold"], number * 30)
                self.assertFalse(proposal["automatic_memory_update"])

    def test_bridge_history_topic_isolation_and_title_filenames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha = initialize(root / "config", intake("fictional-alpha"))
            beta = initialize(root / "config", intake("fictional-beta"))
            db = root / "academic.sqlite3"
            results = run_bootstraps([alpha, beta], db, root / "reports",
                provider_names=["openalex"], now=datetime(2026, 11, 5, tzinfo=timezone.utc),
                providers_by_topic={"fictional-alpha": {"openalex": RichProvider("openalex", [[record(1, doi="10.0000/alpha")]])},
                                    "fictional-beta": {"openalex": RichProvider("openalex", [[record(2, doi="10.0000/beta")]])}},
                limit=1)
            self.assertEqual([results["topics"][t]["selected"] for t in ("fictional-alpha", "fictional-beta")], [1, 1])
            parent = root / "vaults"; parent.mkdir()
            binding = root / "config/obsidian.json"
            setup = create_dedicated_vault(parent, config=binding,
                topic_ids=("fictional-alpha", "fictional-beta"), registered=[])
            vault = Path(setup["vault_root"])
            sources = sorted((root / "reports/fictional-alpha/cards").rglob("*.md"))
            delivery = deliver_cards(sources, section="Bootstrap", config=binding)
            self.assertEqual(delivery["new"], 1)
            card = next((vault / "Bootstrap/fictional-alpha").glob("*/*.md"))
            self.assertFalse(card.stem.isalnum() and len(card.stem) == 24)
            self.assertEqual(rename_delivered_cards(config=binding)["renamed"], 0)
            meta = frontmatter(card.read_text())
            outbox = vault / ".obsidian/plugins/literature-radar-bridge/outbox.json"
            outbox.parent.mkdir(parents=True)
            card.write_text(set_properties(card.read_text(), {"feedback_decision": "must_read", "review_status": "reviewed"}))
            first = {"id": "event-1", "topic_id": "fictional-alpha", "paper_key": meta["paper_key"],
                     "paper_id": meta["paper_id"], "doi": meta["doi"], "decision": "must_read",
                     "recorded_at": "2026-11-06T00:00:00+00:00", "path": str(card.relative_to(vault))}
            outbox.write_text(json.dumps({"version": 1, "events": [first]}))
            self.assertEqual(sync_outbox(binding, [alpha, beta], db)["imported"], 1)
            self.assertEqual(sync_outbox(binding, [alpha, beta], db)["imported"], 0)
            self.assertEqual(load_feedback(beta / "feedback.json")["entries"], [])
            self.assertEqual(frontmatter(card.read_text())["feedback_sync_status"], "synced")
            card.write_text(set_properties(card.read_text(), {"feedback_decision": "keep", "review_status": "reviewed"}))
            second = {**first, "id": "event-2", "decision": "keep", "recorded_at": "2026-11-06T00:01:00+00:00"}
            outbox.write_text(json.dumps({"version": 1, "events": [first, second]}))
            self.assertEqual(sync_outbox(binding, [alpha, beta], db)["imported"], 1)
            self.assertEqual([item["decision"] for item in load_feedback(alpha / "feedback.json")["entries"]],
                             ["must_read", "keep"])
