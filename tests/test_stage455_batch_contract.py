"""Isolated 30-paper batch identity and calibration status regression."""
import json
import tempfile
import unittest
from pathlib import Path

from literature_radar.feedback.batches import batch_progress, prepare_due_batches
from literature_radar.feedback.review import _atomic_json
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake


class Stage455BatchContract(unittest.TestCase):
    def test_distinct_papers_30_60_90_revisions_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = initialize(base / "config", intake("fictional-alpha"))
            beta = initialize(base / "config", intake("fictional-beta"))
            entries = []
            for count in range(1, 91):
                entries.append({"topic_id": "fictional-alpha", "paper_key": f"fictional-alpha|id:{count}",
                                "decision": "keep", "reason": "human",
                                "recorded_at": f"2026-11-05T00:{(count-1)//60:02d}:{(count-1)%60:02d}+00:00"})
                if count in (1, 29, 30, 60, 90):
                    _atomic_json(alpha / "feedback.json", {"schema_version": 1, "entries": entries})
                    progress = prepare_due_batches(alpha)
                    expected = count // 30
                    self.assertEqual(progress["prepared_batches"], expected)
                    self.assertEqual([b["batch_id"] for b in progress["batches"]],
                                     [f"fictional-alpha:{n:03d}" for n in range(1, expected + 1)])
                    self.assertEqual(batch_progress(beta)["prepared_batches"], 0)
            self.assertEqual(prepare_due_batches(alpha)["created_proposals"], [])
            revised = [*entries, {**entries[0], "decision": "discard", "recorded_at": "2026-11-06T00:00:00+00:00"}]
            _atomic_json(alpha / "feedback.json", {"schema_version": 1, "entries": revised})
            self.assertEqual(prepare_due_batches(alpha)["created_proposals"], [])
            self.assertEqual(batch_progress(alpha)["valid_feedback"], 90)
            state_path = alpha / "attention-batch-state.json"
            state = json.loads(state_path.read_text())
            state["batches"][0].update(status="COMPLETED", training_version="v1",
                                       completed_at="2026-11-06T00:01:00+00:00")
            _atomic_json(state_path, state)
            progress = batch_progress(alpha)
            self.assertEqual((progress["completed_batches"], progress["pending_batches"]), (1, 2))
            self.assertEqual(progress["last_calibration_time"], "2026-11-06T00:01:00+00:00")
            self.assertEqual(prepare_due_batches(alpha)["created_proposals"], [])


if __name__ == "__main__":
    unittest.main()
