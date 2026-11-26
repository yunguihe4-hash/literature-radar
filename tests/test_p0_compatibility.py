"""P0 compatibility contracts; fictional data and isolated temporary paths."""
from __future__ import annotations

import fcntl
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.attention.calibration import (
    calibrated_attention, record_evidence, rollback_calibration,
    train_due, training_history, training_status,
)
from literature_radar.attention.legacy_rank import attention_sort_key, classified_attention
from literature_radar.attention.legacy_trainer_math import FEATURE_NAMES, gate
from literature_radar.attention.compatibility import (
    import_ai_os_profile, import_ai_os_evidence, original_keyword_evidence,
    assess_original,
)
from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_bundle
from literature_radar.models import Paper
from literature_radar.delivery.obsidian import create_dedicated_vault
from literature_radar.database.academic import AcademicStore
from literature_radar.topics.manager import initialize
from literature_radar.workflow.state import read_state
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from literature_radar.workflow.weekly_scheduler import run_scheduled
from literature_radar.sources.http import RequestFailure
from tests.test_stage37_39_workflow import RichProvider, intake, record


class Recorder:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, payload, key):
        if self.fail:
            raise RuntimeError("fictional desktop unavailable")
        self.sent.append(key)


class TimeoutOnceProvider(RichProvider):
    def __init__(self, provider_id, pages):
        super().__init__(provider_id, pages)
        self.timeout_once = True

    def fetch_page(self, query, cursor=None):
        if self.timeout_once:
            self.timeout_once = False
            raise RequestFailure(self.provider_id, "timeout")
        return super().fetch_page(query, cursor)


class P0CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "academic.sqlite3"
        self.config_a = initialize(self.root / "config", intake("fictional-alpha"))
        self.config_b = initialize(self.root / "config", intake("fictional-beta"))
        for path in (self.config_a, self.config_b):
            activate_weekly(path, skip_bootstrap=True, reason="offline compatibility test")

    def test_two_missed_cycles_and_failed_topic_isolated(self):
        state_file = self.root / "reports/weekly_schedule.json"
        state_file.parent.mkdir(parents=True)
        initial = "2026-10-26T09:00:00-04:00"
        state_file.write_text(json.dumps({"schema_version": 2, "topics": {
            topic: {"next_scheduled_time": initial, "lookback_days": 7}
            for topic in ("fictional-alpha", "fictional-beta")}}))
        calls = []

        def runner(config, database, output, **kwargs):
            self.assertEqual(database, self.database)
            self.assertEqual(kwargs["cards_dir"], self.root / "shared-cards")
            calls.append((config.name, kwargs["scheduled_at"].isoformat(),
                          kwargs["now"].isoformat()))
            if config.name == "fictional-alpha":
                raise RuntimeError("fictional whole-Topic failure")
            return {"status": "CATCHUP_REQUIRED", "coverage_complete": True,
                    "overlap_window": {"end": kwargs["scheduled_at"].date().isoformat()},
                    "run_id": kwargs["scheduled_at"].strftime("%Y%m%d") + "-beta",
                    "candidate_count": 0, "recommended_count": 0,
                    "pending_provider_windows": []}

        result = run_scheduled(self.root / "config", self.database, self.root / "reports",
                               now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc),
                               cards_dir=self.root / "shared-cards",
                               runner=runner)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["topics"][0]["runs"]), 1)
        self.assertEqual(len(result["topics"][1]["runs"]), 3)
        self.assertEqual([call[1][:10] for call in calls if call[0] == "fictional-beta"],
                         ["2026-10-26", "2026-11-02", "2026-11-09"])
        self.assertEqual([call[1][-6:] for call in calls if call[0] == "fictional-beta"],
                         ["-04:00", "-05:00", "-05:00"])
        self.assertEqual(result["topics"][0]["next_scheduled_time"], initial)
        self.assertEqual(len((self.root / "reports/weekly_history.md").read_text().splitlines()), 8)
        again = run_scheduled(self.root / "config", self.database, self.root / "reports",
                              now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc),
                              cards_dir=self.root / "shared-cards",
                              runner=runner)
        self.assertEqual(again["topics"][1]["status"], "NOT_DUE")
        def recovered(config, _database, _output, **kwargs):
            return {"status": "CATCHUP_REQUIRED", "coverage_complete": True,
                    "overlap_window": {"end": kwargs["scheduled_at"].date().isoformat()},
                    "run_id": kwargs["scheduled_at"].strftime("%Y%m%d") + "-" + config.name,
                    "candidate_count": 0, "recommended_count": 0,
                    "pending_provider_windows": []}
        fixed = run_scheduled(self.root / "config", self.database, self.root / "reports",
                              now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc),
                              cards_dir=self.root / "shared-cards",
                              runner=recovered)
        self.assertEqual(len(fixed["topics"][0]["runs"]), 3)
        history = (self.root / "reports/weekly_history.md").read_text()
        self.assertIn("|fictional-alpha|-|FAILED|0|", history)
        self.assertIn("|20261026-fictional-alpha|CATCHUP_REQUIRED|0|", history)

    def test_disabled_topic_and_flock(self):
        topic_file = self.config_b / "topic.json"
        topic = json.loads(topic_file.read_text())
        topic["enabled"] = False
        topic_file.write_text(json.dumps(topic))
        lock_path = self.root / "reports/weekly.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_scheduled(self.root / "config", self.database, self.root / "reports",
                                   lock_file=lock_path, now=datetime(2026, 11, 9, 15,
                                                                    tzinfo=timezone.utc))
        self.assertEqual(result["status"], "LOCKED")
        self.assertFalse((self.root / "reports/weekly_schedule.json").exists())
        dry = run_scheduled(self.root / "config", self.database, self.root / "reports",
                            dry_run=True, now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(dry["topics"], ["fictional-alpha"])

    def test_legacy_schedule_and_history_migrate_without_losing_fields(self):
        topic_file = self.config_b / "topic.json"
        topic = json.loads(topic_file.read_text())
        topic["enabled"] = False
        topic_file.write_text(json.dumps(topic))
        state_file = self.root / "reports/weekly_schedule.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"topic": "fictional-alpha",
            "next_scheduled_time": "2026-11-16T09:00:00",
            "lookback_days": 7, "legacy_note": "preserve"}))
        history_file = self.root / "reports/weekly_history.md"
        history_file.write_text("# Weekly Literature Radar History\n\n"
            "|scheduled_time|topic|run_id|status|candidate_count|\n"
            "|---|---|---|---|---|\n"
            "|2026-11-02 09:00|fictional-alpha|old-run|SUCCESS|2|\n")
        result = run_scheduled(self.root / "config", self.database, self.root / "reports",
            now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(result["topics"][0]["status"], "NOT_DUE")
        migrated = json.loads(state_file.read_text())
        self.assertEqual(migrated["topics"]["fictional-alpha"]["legacy_note"], "preserve")
        self.assertIn("|2026-11-02 09:00|-|fictional-alpha|old-run|SUCCESS|2|",
                      history_file.read_text())

    def test_real_weekly_uses_planned_window_and_actual_delivery_day(self):
        state_file = self.root / "reports/weekly_schedule.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"schema_version": 2, "topics": {
            "fictional-alpha": {"next_scheduled_time": "2026-11-02T09:00:00-05:00",
                                "lookback_days": 7}}}))
        topic_file = self.config_b / "topic.json"
        topic = json.loads(topic_file.read_text())
        topic["enabled"] = False
        topic_file.write_text(json.dumps(topic))
        provider = RichProvider("openalex", [[record(1)]])
        result = run_scheduled(
            self.root / "config", self.database, self.root / "reports",
            now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc),
            provider_names=["openalex"],
            providers_by_topic={"fictional-alpha": {"openalex": provider}})
        runs = result["topics"][0]["runs"]
        self.assertEqual([run["scheduled_time"][:10] for run in runs],
                         ["2026-11-02", "2026-11-09"])
        latest = json.loads((self.root / "reports/fictional-alpha/weekly-report.json").read_text())
        self.assertEqual(latest["primary_window"]["end"], "2026-11-09")
        self.assertEqual(latest["actual_run_at"][:10], "2026-11-09")
        self.assertEqual(read_state(self.config_a)["last_weekly_window"]["end"], "2026-11-09")

    def test_crash_after_topic_commit_reuses_completed_cycle(self):
        topic_file = self.config_b / "topic.json"
        topic = json.loads(topic_file.read_text())
        topic["enabled"] = False
        topic_file.write_text(json.dumps(topic))
        planned = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
        actual = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
        output = self.root / "reports/fictional-alpha"
        first = run_weekly(self.config_a, self.database, output,
                           provider_names=["openalex"],
                           providers={"openalex": RichProvider("openalex", [[record(1)]])},
                           now=actual, scheduled_at=planned)
        self.assertEqual(first["status"], "SUCCESS")
        state_file = self.root / "reports/weekly_schedule.json"
        state_file.write_text(json.dumps({"schema_version": 2, "topics": {
            "fictional-alpha": {"next_scheduled_time": "2026-11-02T09:00:00-05:00",
                                "lookback_days": 7}}}))
        def must_not_run(*_args, **_kwargs):
            raise AssertionError("committed Weekly must not rerun")
        recovered = run_scheduled(self.root / "config", self.database,
                                  self.root / "reports", now=actual,
                                  runner=must_not_run)
        self.assertEqual(recovered["status"], "SUCCESS")
        self.assertEqual(recovered["topics"][0]["runs"][0]["recommended_count"], 1)
        self.assertEqual(len(list((output / "cards").rglob("*.md"))), 1)

    def test_provider_timeout_recovers_without_repeating_healthy_source(self):
        healthy = RichProvider("openalex", [[record(1)]])
        timeout = TimeoutOnceProvider("crossref", [[record(2)]])
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex", "crossref"],
                           providers={"openalex": healthy, "crossref": timeout},
                           now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(first["status"], "PARTIAL_SUCCESS")
        self.assertEqual(first["recommended_count"], 1)
        self.assertEqual(len(first["pending_provider_windows"]), 1)
        self.assertEqual(first["providers"]["crossref"]["error_category"], "timeout")
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex", "crossref"],
                            providers={"openalex": healthy, "crossref": timeout},
                            now=datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(len(healthy.calls), 1)
        self.assertEqual(len(timeout.calls), 1)
        self.assertEqual(second["pending_provider_windows"], [])

    def test_zero_candidate_weekly_has_no_card_or_inbox(self):
        result = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[]])},
                            now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["recommended_count"], 0)
        self.assertEqual(result["cards"], [])
        self.assertEqual(result["notification"]["sent"], 0)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["topic_id"], "fictional-alpha")
        self.assertEqual(manifest["recommendations"], [])
        self.assertEqual(manifest["search_window"], result["overlap_window"])

    def test_weekly_processes_thirty_feedback_memory_batch_separately(self):
        question = load_bundle(self.config_a)["profile"]["questions"][0]["id"]
        entries = [{"topic_id": "fictional-alpha",
                    "paper_key": f"fictional-alpha|id:{number:03d}",
                    "decision": "keep", "reason": "fictional human review",
                    "recorded_at": f"2026-11-05T00:{number - 1:02d}:00+00:00",
                    "extensions": {"important_question": question}}
                   for number in range(1, 31)]
        (self.config_a / "feedback.json").write_text(json.dumps(
            {"schema_version": 1, "entries": entries}))
        result = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[]])},
                            now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(result["memory_training"]["status"], "TRAINED")
        self.assertEqual(result["memory_training"]["completed_batches"], 1)
        self.assertEqual(result["memory_training"]["processed_feedback"], 30)
        self.assertEqual(result["attention_training"]["status"], "INSUFFICIENT_LABEL_DIVERSITY")

    def test_failed_feedback_training_is_not_reported_as_weekly_success(self):
        with patch("literature_radar.workflow.weekly.train_due_batches",
                   side_effect=RuntimeError("fictional training failure")):
            result = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                                provider_names=["openalex"],
                                providers={"openalex": RichProvider("openalex", [[]])},
                                now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["memory_training"]["status"], "FAILED")

    def test_seven_dimension_order_and_classes(self):
        rank = {name: False for name in FEATURE_NAMES if name != "keyword_evidence"}
        rank["keyword_evidence"] = 100
        first = {"attention_rank": rank, "paper": {"title": "A"}, "eligible": False}
        second = {"attention_rank": {**rank, "research_question_match": True,
                                     "keyword_evidence": 0},
                  "paper": {"title": "B"}, "eligible": False}
        self.assertLess(attention_sort_key(second), attention_sort_key(first))
        self.assertEqual(classified_attention(first), "LOW")
        self.assertEqual(classified_attention(second), "MEDIUM")
        second["eligible"] = True
        self.assertEqual(classified_attention(second), "HIGH")

    def test_private_original_profile_import_is_exact_and_non_overwriting(self):
        source = self.root / "old/skills/literature-intelligence"
        for child in ("profile", "ontology", "memory"):
            (source / child).mkdir(parents=True)
        old_profile = {"schema_version": 1, "topics": {"fictional-alpha": {
            "important_questions": [{"name": "signal mapping", "aliases": ["signal mapping"]}],
            "important_applications": [], "accepted_cross_species_sources": ["Fictivus beta"],
            "excluded_directions": ["unrelated astronomy"]}}}
        (source / "profile/research-profile.yaml").write_text(json.dumps(old_profile))
        (source / "profile/research-phase.yaml").write_text(
            "schema_version: 1\ntopics:\n  fictional-alpha:\n"
            "    current_phase: signal mapping\n    active_signals:\n"
            "      - phase lattice assay\n")
        for name, value in (("ontology/cross-species-transfer.yaml", {
                "target_species": [{"name": "Imaginaryus alpha", "aliases": ["Imaginaryus alpha"]}],
                "model_species": [{"name": "Fictivus beta", "aliases": ["Fictivus beta"]}],
                "technology_source": []}),
                ("ontology/methods.yaml", {"methods": [{"name": "phase lattice assay",
                    "aliases": ["phase lattice assay"], "transferable_to": ["signal mapping"]}]}),
                ("memory/attention-memory.yaml", {"entries": []})):
            (source / name).write_text(json.dumps(value))
        imported = import_ai_os_profile(self.root / "old", self.config_a)
        self.assertEqual(imported["status"], "IMPORTED")
        sidecar = json.loads((self.config_a / "legacy-intelligence.json").read_text())
        self.assertEqual(sidecar["profile"], old_profile["topics"]["fictional-alpha"])
        self.assertEqual(sidecar["phase"]["active_signals"], ["phase lattice assay"])
        with self.assertRaises(FileExistsError):
            import_ai_os_profile(self.root / "old", self.config_a)
        bundle = load_bundle(self.config_a)
        preferred = bundle["profile"]["questions"][0]["id"]
        memory_file = self.config_a / "attention-memory.json"
        memory = json.loads(memory_file.read_text())
        memory["preferred_concept_ids"] = [preferred]
        memory_file.write_text(json.dumps(memory))
        bundle = load_bundle(self.config_a)
        paper = Paper("fictional-identity",
                      "Imaginaryus alpha signal mapping with phase lattice assay",
                      "Imaginaryus alpha signal mapping uses a phase lattice assay.",
                      "fixture")
        old_rank = assess_original(paper, bundle, 20, config_dir=self.config_a)
        self.assertTrue(old_rank["attention_rank"]["attention_memory"])
        with self.assertRaisesRegex(ValueError, "requires its evidence rules"):
            original_keyword_evidence(paper, self.config_a, 20)

    def test_original_keyword_evidence_uses_original_topic_weights_and_ontology(self):
        source = self.root / "old/skills/literature-intelligence"
        (source / "rules").mkdir(parents=True)
        (source / "ontology").mkdir(parents=True)
        rules = {"schema_version": 2, "topic": "fictional-alpha",
                 "dimensions": {"Direct project relevance": [
                     {"keyword": "signal mapping", "score": 31,
                      "aliases": ["signal mapping"]}]},
                 "ontologies": ["lattice.yaml"]}
        ontology = {"schema_version": 1, "name": "lattice",
                    "classification": "Technology transfer", "concepts": [
                        {"keyword": "phase lattice assay", "score": 47,
                         "aliases": ["phase lattice assay"], "method": "assay",
                         "possible_application": "mapping"}]}
        (source / "rules/fictional-alpha.yaml").write_text(json.dumps(rules))
        (source / "ontology/lattice.yaml").write_text(json.dumps(ontology))
        self.assertEqual(import_ai_os_evidence(self.root / "old", self.config_a)["ontology_count"], 1)
        with self.assertRaises(FileExistsError):
            import_ai_os_evidence(self.root / "old", self.config_a)
        paper = Paper("fictional-identity", "Signal mapping with phase lattice assay",
                      "Signal mapping was performed with a phase lattice assay.", "fixture")
        score, evidence, source_name = original_keyword_evidence(paper, self.config_a, 3)
        self.assertEqual((score, source_name), (78, "original_ai_os"))
        self.assertEqual({entry["score"] for entry in evidence}, {31, 47})
        integrated = recommend(paper, load_bundle(self.config_a), config_dir=self.config_a)
        self.assertEqual(integrated["original_attention"]["attention_rank"]["keyword_evidence"], 78)
        self.assertEqual(integrated["original_attention"]["keyword_evidence_source"], "original_ai_os")
        fallback, _, fallback_source = original_keyword_evidence(paper, self.config_b, 3)
        self.assertEqual((fallback, fallback_source), (3, "fallback"))

    def test_original_trainer_requires_twenty_and_rejects_regression(self):
        topic = "fictional-alpha"
        rank = {name: False for name in FEATURE_NAMES if name != "keyword_evidence"}
        rank["keyword_evidence"] = 50
        for number in range(20):
            key = f"{topic}|doi:10.0000/training-{number}"
            row = {"topic_id": topic, "paper_key": key, "attention_rank": rank,
                   "intrinsic_attention": "LOW"}
            record_evidence(self.config_a, "fictional-run", [row])
        entries = [{"topic_id": topic, "paper_key": f"{topic}|doi:10.0000/training-{number}",
                    "decision": "must_read" if number < 10 else "discard",
                    "reason": "fictional", "recorded_at": "2026-11-08T00:00:00Z"}
                   for number in range(20)]
        feedback_path = self.config_a / "feedback.json"
        feedback_path.write_text(json.dumps({"schema_version": 1, "entries": entries[:19]}))
        self.assertEqual(train_due(self.config_a)["status"], "NOT_ELIGIBLE")
        feedback_path.write_text(json.dumps({"schema_version": 1, "entries": entries}))
        result = train_due(self.config_a)
        self.assertEqual(result["trainable_feedback"], 20)
        self.assertEqual(result["evaluation"]["evaluation_method"], "leave_one_out")
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse((self.config_a / "attention-calibration/active.json").exists())
        self.assertEqual(train_due(self.config_a)["status"], "NOT_ELIGIBLE")
        self.assertFalse(gate({"ranking_utility": 0, **{
            key: 0 for key in result["evaluation"]["candidate"] if key != "ranking_utility"}},
            result["evaluation"]["active_or_builtin"])[0])

    def test_original_trainer_activates_only_after_quality_gate(self):
        topic = "fictional-alpha"
        memory_before = (self.config_a / "attention-memory.json").read_bytes()
        entries = []
        for number in range(20):
            positive = number % 2 == 0
            key = f"{topic}|doi:10.0000/activation-{number}"
            rank = {name: False for name in FEATURE_NAMES if name != "keyword_evidence"}
            rank.update(research_question_match=not positive,
                        transfer_value=positive, method_match=positive,
                        keyword_evidence=10 + number)
            record_evidence(self.config_a, "activation-run", [{
                "topic_id": topic, "paper_key": key, "attention_rank": rank,
                "intrinsic_attention": "HIGH" if positive else "LOW"}])
            entries.append({"topic_id": topic, "paper_key": key,
                            "decision": "must_read" if positive else "discard",
                            "reason": "fictional", "recorded_at": "2026-11-08T00:00:00Z"})
        (self.config_a / "feedback.json").write_text(json.dumps(
            {"schema_version": 1, "entries": entries}))
        result = train_due(self.config_a, minimum_new_feedback=20)
        self.assertEqual(result["status"], "ACTIVATED")
        self.assertEqual((self.config_a / "attention-memory.json").read_bytes(), memory_before)
        self.assertTrue(result["evaluation"]["gate_passed"])
        self.assertEqual(result["active_version"], "v001")
        self.assertEqual(training_history(self.config_a)["versions"][0]["version"], "v001")
        self.assertEqual(training_status(self.config_a)["last_training_status"], "ACTIVATED")
        original = {"attention_rank": rank, "final_attention": "LOW"}
        calibrated = calibrated_attention(self.config_a, original)
        self.assertEqual(calibrated["topic_calibration_version"], "v001")
        self.assertEqual(original["final_attention"], "LOW")
        self.assertTrue(rollback_calibration(self.config_a, "v001")["rollback"])
        self.assertEqual(training_status(self.config_a)["last_training_status"], "ROLLED_BACK")

    def test_high_inbox_dedup_and_failed_desktop_retry(self):
        parent = self.root / "vaults"
        parent.mkdir()
        setup = create_dedicated_vault(parent, config=self.root / "config/obsidian.json",
                                       topic_ids=("fictional-alpha",), registered=[])
        vault = Path(setup["vault_root"])
        delivery_file = self.config_a / "delivery.json"
        delivery = json.loads(delivery_file.read_text())
        delivery["notification"] = {"enabled": True, "provider": "desktop"}
        delivery_file.write_text(json.dumps(delivery))
        failed = Recorder(fail=True)
        first = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                           provider_names=["openalex"],
                           providers={"openalex": RichProvider("openalex", [[record(1)]])},
                           notifiers={"desktop": failed},
                           now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(first["status"], "NOTIFICATION_FAILED")
        self.assertEqual(len(list((vault / "Inbox/Literature").glob("*.md"))), 1)
        self.assertEqual(first["notification"]["failed"], 1)
        self.assertIsNotNone(read_state(self.config_a)["active_weekly_window"])
        for hour in (13, 14, 15):
            pending = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                                 provider_names=["openalex"],
                                 providers={"openalex": RichProvider("openalex", [[]])},
                                 notifiers={"desktop": failed},
                                 now=datetime(2026, 11, 6, hour, tzinfo=timezone.utc))
            self.assertEqual(pending["status"], "NOTIFICATION_FAILED")
            self.assertEqual(pending["notification"]["unresolved"], 1)
        self.assertEqual(AcademicStore(self.database).retry_failed_notifications(channel="desktop"), 1)
        good = Recorder()
        second = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                            provider_names=["openalex"],
                            providers={"openalex": RichProvider("openalex", [[]])},
                            notifiers={"desktop": good},
                            now=datetime(2026, 11, 6, 16, tzinfo=timezone.utc))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(len(good.sent), 1)
        self.assertEqual(len(list((vault / "Inbox/Literature").glob("*.md"))), 1)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE channel='desktop' AND status='SENT'").fetchone()[0], 1)
        repeated = run_weekly(self.config_a, self.database, self.root / "weekly-a",
                              provider_names=["openalex"],
                              providers={"openalex": RichProvider("openalex", [[]])},
                              notifiers={"desktop": good},
                              now=datetime(2026, 11, 6, 17, tzinfo=timezone.utc))
        self.assertEqual(repeated["status"], "NOT_DUE")
        self.assertEqual(len(good.sent), 1)


if __name__ == "__main__":
    unittest.main()
