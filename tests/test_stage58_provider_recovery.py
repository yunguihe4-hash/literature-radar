"""Offline regression checks for Stage 5.8 Provider recovery."""
from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.sources.factory import make_provider
from tests.synthetic_provider import SyntheticProvider, fictional

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "synthetic-topic"


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.db = Path(work.name) / "recovery.sqlite3"
        self.topic = load_bundle(CONFIG)["topic"]["id"]

    def run_scan(self, providers, *, since="2026-01-01", until="2026-01-31", **kw):
        return discover_incremental(
            CONFIG, list(providers), database_path=self.db, since=since, until=until,
            providers=providers, max_pages=10, max_seconds=30, **kw)

    def checkpoint(self, provider, *, since="2026-01-01", until="2026-01-31", topic=None):
        from literature_radar.providers.models import DateWindow
        bundle = load_bundle(CONFIG)
        bundle["topic"]["id"] = topic or self.topic
        plan = provider.plan_queries(bundle["topic"], bundle["focus"], DateWindow(since, until))[0]
        return AcademicStore(self.db).checkpoint(provider.provider_id, topic or self.topic,
                                                 plan.query_signature)

    def test_slow_page_defaults_are_bounded_and_overridable(self):
        for name, read, total in (("europepmc", 45, 60), ("crossref", 30, 45),
                                  ("biorxiv", 30, 45)):
            with self.subTest(provider=name):
                client = make_provider(name).client
                self.assertEqual((client.read_timeout, client.total_timeout, client.max_retries),
                                 (read, total, 3))
                custom = make_provider(name, options={"read_timeout": 12,
                                                       "total_timeout": 20,
                                                       "max_retries": 1}).client
                self.assertEqual((custom.read_timeout, custom.total_timeout, custom.max_retries),
                                 (12, 20, 1))
        self.assertEqual(make_provider("pubmed").client.total_timeout, 25)
        self.assertEqual(make_provider("openalex").client.max_retries, 2)

    def test_page_timeout_commits_prior_page_and_next_run_resumes_without_replay(self):
        rows = [[fictional("one", doi="10.0000/one")],
                [fictional("two", doi="10.0000/two")]]
        first = SyntheticProvider("europepmc", rows, fail_at=1)
        result = self.run_scan({"europepmc": first})
        cp = self.checkpoint(first)
        self.assertEqual(result["providers"]["europepmc"]["status"], "FAILED")
        self.assertEqual((cp["status"], cp["page_number"], cp["records_fetched"],
                          cp["cursor_json"]), ("FAILED", 1, 1, "1"))
        resumed = SyntheticProvider("europepmc", rows)
        self.assertEqual(self.run_scan({"europepmc": resumed})["status"], "SUCCESS")
        self.assertEqual(resumed.calls, [1])
        self.assertEqual(self.checkpoint(resumed)["status"], "SUCCESS")
        self.assertEqual(AcademicStore(self.db).count(), 2)
        completed = SyntheticProvider("europepmc", rows)
        self.assertEqual(self.run_scan({"europepmc": completed})["status"], "SUCCESS")
        self.assertEqual(completed.calls, [])

    def test_date_window_resume_keeps_original_window_and_skips_completed_one(self):
        old = SyntheticProvider("crossref", [[fictional("old", doi="10.0000/old")],
                                             [fictional("later", doi="10.0000/later")]], fail_at=1)
        self.run_scan({"crossref": old})
        newer = SyntheticProvider("crossref", [[fictional("new", doi="10.0000/new")]])
        self.assertEqual(self.run_scan({"crossref": newer}, since="2026-02-01",
                                       until="2026-02-15")["status"], "SUCCESS")
        resumed = SyntheticProvider("crossref", old.pages)
        self.assertEqual(self.run_scan({"crossref": resumed})["status"], "SUCCESS")
        self.assertEqual(resumed.calls, [1])
        self.assertEqual((resumed.fetched_windows[0].start,
                          resumed.fetched_windows[0].end), ("2026-01-01", "2026-01-31"))
        self.assertEqual(self.checkpoint(newer, since="2026-02-01",
                                         until="2026-02-15")["status"], "SUCCESS")

    def test_one_provider_timeout_leaves_other_provider_running_and_partial_persists(self):
        slow = SyntheticProvider("europepmc", [[fictional("first", doi="10.0000/first")],
                                               [fictional("second", doi="10.0000/second")]], fail_at=1)
        fast = SyntheticProvider("pubmed", [[fictional("third", doi="10.0000/third")]])
        result = self.run_scan({"europepmc": slow, "pubmed": fast})
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["providers"]["europepmc"]["status"], "FAILED")
        self.assertEqual(result["providers"]["pubmed"]["status"], "SUCCESS")
        self.assertEqual(fast.calls, [0])
        self.assertEqual(self.checkpoint(slow)["page_number"], 1)
        self.assertEqual(AcademicStore(self.db).count(), 2)
        recovered = SyntheticProvider("europepmc", slow.pages)
        no_replay = SyntheticProvider("pubmed", fast.pages)
        self.assertEqual(self.run_scan({"europepmc": recovered, "pubmed": no_replay})["status"], "SUCCESS")
        self.assertEqual((recovered.calls, no_replay.calls), ([1], []))

    def test_first_provider_exhausts_own_budget_without_starving_next(self):
        class SlowFirstPage(SyntheticProvider):
            def fetch_page(self, query, cursor=None):
                page = super().fetch_page(query, cursor)
                time.sleep(0.03)
                return page

        slow = SlowFirstPage("europepmc", [[fictional("slow", doi="10.0000/slow")],
                                            [fictional("unseen", doi="10.0000/unseen")]])
        fast = SyntheticProvider("pubmed", [[fictional("fast", doi="10.0000/fast")]])
        result = discover_incremental(
            CONFIG, ["europepmc", "pubmed"], database_path=self.db,
            since="2026-01-01", until="2026-01-31", max_pages=10,
            max_seconds=0.02, providers={"europepmc": slow, "pubmed": fast})
        self.assertEqual(result["providers"]["europepmc"]["status"], "INCOMPLETE")
        self.assertEqual(result["providers"]["pubmed"]["status"], "SUCCESS")
        self.assertEqual((slow.calls, fast.calls), ([0], [0]))
        self.assertEqual(self.checkpoint(slow)["page_number"], 1)

    def test_topic_checkpoint_isolation(self):
        first = SyntheticProvider("openalex", [[fictional("a", doi="10.0000/a")],
                                                 [fictional("b", doi="10.0000/b")]], fail_at=1)
        self.run_scan({"openalex": first})
        second_bundle = copy.deepcopy(load_bundle(CONFIG))
        second_bundle["topic"]["id"] = "second-synthetic-topic"
        second = SyntheticProvider("openalex", [[fictional("c", doi="10.0000/c")]])
        with patch("literature_radar.discovery.incremental.load_bundle", return_value=second_bundle):
            result = self.run_scan({"openalex": second})
        self.assertEqual(result["topic_id"], "second-synthetic-topic")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(second.calls, [0])
        self.assertEqual(self.checkpoint(first)["page_number"], 1)
        self.assertEqual(self.checkpoint(second, topic="second-synthetic-topic")["status"], "SUCCESS")
        resumed = SyntheticProvider("openalex", first.pages)
        self.assertEqual(self.run_scan({"openalex": resumed})["status"], "SUCCESS")
        self.assertEqual(resumed.calls, [1])


if __name__ == "__main__":
    unittest.main()
