"""Synthetic Topic feedback proves batch boundaries and recoverable Memory versions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_radar.attention.batch_training import train_due_batches, rollback_last_batch
from literature_radar.config import load_bundle
from literature_radar.feedback.batches import batch_progress
from literature_radar.feedback.review import _atomic_json
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake


class Stage46AttentionTraining(unittest.TestCase):
    def _setup(self, root, name):
        config = initialize(root / "config", intake(name))
        question = load_bundle(config)["profile"]["questions"][0]["id"]
        return config, question

    def _write_feedback(self, config, count, question):
        topic = config.name
        entries = [{"topic_id": topic, "paper_key": f"{topic}|id:{n:03d}",
                    "decision": ("must_read", "keep", "later", "discard")[(n - 1) % 4],
                    "reason": "explicit human decision", "recorded_at": f"2026-11-05T00:{(n-1)//60:02d}:{(n-1)%60:02d}+00:00",
                    "extensions": {"important_question": question}}
                   for n in range(1, count + 1)]
        _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries})
        return entries

    def test_29_30_59_60_90_and_topic_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alpha, question = self._setup(root, "fictional-alpha")
            beta, other = self._setup(root, "fictional-beta")
            for count, expected in ((29, 0), (30, 1), (59, 1), (60, 2), (90, 3)):
                self._write_feedback(alpha, count, question)
                result = train_due_batches(alpha)
                self.assertEqual(result["completed_batches"], expected)
                self.assertEqual(result["processed_feedback"], expected * 30)
                self.assertEqual(result["unprocessed_feedback"], count - expected * 30)
                self.assertEqual(result["next_threshold"], (expected + 1) * 30)
                self.assertEqual(batch_progress(beta)["completed_batches"], 0)
                if count == 29:
                    self.assertFalse((alpha / "attention-batch-state.json").exists())
            state = json.loads((alpha / "attention-batch-state.json").read_text())
            self.assertEqual([b["batch_id"] for b in state["batches"]],
                             [f"fictional-alpha:{n:03d}" for n in (1, 2, 3)])
            self.assertEqual(len(list((alpha / "attention-versions").glob("*-record.json"))), 3)
            self.assertEqual(json.loads((beta / "attention-memory.json").read_text())["preferred_concept_ids"], [])
            self.assertEqual(json.loads((alpha / "attention-memory.json").read_text())["preferred_concept_ids"], [question])
            entries = self._write_feedback(alpha, 90, question)
            entries.append({**entries[0], "decision": "discard", "recorded_at": "2026-11-06T00:00:00+00:00"})
            _atomic_json(alpha / "feedback.json", {"schema_version": 1, "entries": entries})
            self.assertEqual(train_due_batches(alpha)["trained"], [])
            self.assertEqual(batch_progress(alpha)["valid_feedback"], 90)
            self._write_feedback(beta, 30, other)
            self.assertEqual(train_due_batches(beta)["completed_batches"], 1)
            self.assertEqual(json.loads((beta / "attention-memory.json").read_text())["preferred_concept_ids"], [other])

    def test_retry_after_memory_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, question = self._setup(Path(temporary), "fictional-alpha")
            self._write_feedback(config, 30, question)
            original = _atomic_json
            state_path = config / "attention-batch-state.json"
            triggered = False
            def fail_once(path, value):
                nonlocal triggered
                if Path(path) == state_path and not triggered and value["batches"][0].get("status") == "COMPLETED":
                    triggered = True
                    raise OSError("simulated state commit failure")
                return original(path, value)
            with patch("literature_radar.attention.batch_training._atomic_json", side_effect=fail_once):
                with self.assertRaises(OSError):
                    train_due_batches(config)
            self.assertTrue(triggered)
            result = train_due_batches(config)
            self.assertEqual(result["completed_batches"], 1)
            self.assertEqual(result["trained"], ["fictional-alpha:001"])
            self.assertEqual(train_due_batches(config)["trained"], [])
            record = json.loads((config / "attention-versions/batch-001-record.json").read_text())
            self.assertEqual(record["feedback_count"], 30)
            self.assertEqual((record["feedback_start"], record["feedback_end"]), (1, 30))
            self.assertEqual(len(record["feedback"]), 30)
            rolled = rollback_last_batch(config)
            self.assertEqual(rolled["batch_id"], "fictional-alpha:001")
            self.assertEqual(json.loads((config / "attention-memory.json").read_text())["preferred_concept_ids"], [])
            self.assertEqual(train_due_batches(config)["trained"], [])
            self.assertEqual(batch_progress(config)["completed_batches"], 0)

    def test_two_batches_at_once_and_automatic_entry_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, question = self._setup(Path(temporary), "fictional-alpha")
            entries = self._write_feedback(config, 60, question)
            entries.append({"topic_id": "fictional-alpha", "paper_key": "fictional-alpha|id:automatic",
                            "decision": "keep", "reason": "discovery bookkeeping",
                            "recorded_at": "2026-11-05T23:00:00+00:00",
                            "extensions": {"source": "bootstrap"}})
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries})
            result = train_due_batches(config)
            self.assertEqual(result["trained"], ["fictional-alpha:001", "fictional-alpha:002"])
            self.assertEqual(result["valid_feedback"], 60)
            self.assertEqual(result["unprocessed_feedback"], 0)

    def test_revision_after_first_batch_does_not_reenter_second(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, question = self._setup(Path(temporary), "fictional-alpha")
            entries = self._write_feedback(config, 30, question)
            self.assertEqual(train_due_batches(config)["completed_batches"], 1)
            first_proposal = (config / "attention-batch-001.json").read_bytes()
            entries.append({**entries[0], "decision": "discard",
                            "recorded_at": "2026-11-07T00:00:00+00:00"})
            for n in range(31, 61):
                entries.append({"topic_id": "fictional-alpha", "paper_key": f"fictional-alpha|id:{n:03d}",
                                "decision": "keep", "reason": "explicit human decision",
                                "recorded_at": f"2026-11-06T00:00:{n-31:02d}+00:00"})
            _atomic_json(config / "feedback.json", {"schema_version": 1, "entries": entries})
            result = train_due_batches(config)
            self.assertEqual(result["completed_batches"], 2)
            self.assertEqual(result["valid_feedback"], 60)
            self.assertEqual((config / "attention-batch-001.json").read_bytes(), first_proposal)
            second = json.loads((config / "attention-batch-002.json").read_text())
            self.assertEqual(second["paper_keys"], [f"fictional-alpha|id:{n:03d}" for n in range(31, 61)])


if __name__ == "__main__":
    unittest.main()
