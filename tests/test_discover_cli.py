import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
from literature_radar.cli import main

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"

class DiscoverCLITests(unittest.TestCase):
    def test_dry_run_all_sources_is_offline_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "should-not-exist.sqlite3"
            output = io.StringIO()
            with patch("literature_radar.sources.http.HTTPClient._network", side_effect=AssertionError("network used")):
                with redirect_stdout(output):
                    code = main(["discover", "--config-dir", str(CONFIG), "--database", str(database),
                                 "--providers", "openalex,crossref,europepmc,pubmed,biorxiv",
                                 "--dry-run", "--format", "json"])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(len(result["providers"]), 5)
            self.assertFalse(database.exists())

    def test_topic_mismatch_and_missing_database(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["discover", "--config-dir", str(CONFIG),
                                   "--providers", "openalex", "--topic-id", "other",
                                   "--dry-run"]), 2)
            self.assertEqual(main(["discover", "--config-dir", str(CONFIG),
                                   "--providers", "openalex"]), 2)
