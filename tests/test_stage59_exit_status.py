"""Scheduled Weekly exit status must distinguish bounded recovery from failure."""
import unittest

from literature_radar.cli import _weekly_exit_code


class ScheduledExitStatusTests(unittest.TestCase):
    def test_partial_checkpoint_is_success(self):
        result = {"status": "PARTIAL", "topics": [
            {"status": "PARTIAL_SUCCESS", "runs": [{"status": "PARTIAL_SUCCESS"}]},
            {"status": "NOT_DUE", "runs": []},
        ]}
        self.assertEqual(_weekly_exit_code(result, "run-scheduled"), 0)
        self.assertEqual(_weekly_exit_code({"status": "DRY_RUN", "topics": ["one"],
                                            "configuration_errors": []}, "run-scheduled"), 0)

    def test_real_failure_and_lock_remain_nonzero(self):
        for result in (
            {"status": "PARTIAL", "topics": [{"status": "FAILED", "runs": [{"status": "FAILED"}]}]},
            {"status": "PARTIAL", "topics": [{"status": "NOT_DUE", "runs": [{"status": "FAILED"}]}]},
            {"status": "LOCKED", "topics": []},
        ):
            self.assertEqual(_weekly_exit_code(result, "run-scheduled"), 3)


if __name__ == "__main__":
    unittest.main()
