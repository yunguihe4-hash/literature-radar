"""Explicit-path offline CLI and read-only configuration validation."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from literature_radar import __version__

from literature_radar.config import ConfigurationError, load_bundle, validate_config_dir
from literature_radar.sources.fixture import FixtureProvider
from literature_radar.pipeline import run
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.discovery.query_planner import import_ai_os_search
from literature_radar.feedback.store import add_feedback
from literature_radar.feedback.batches import prepare_due_batches, batch_progress
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.legacy_cards import collect_legacy_cards
from literature_radar.research.specialty import run_specialty, recall_audit
from literature_radar.research.recall_benchmark import evaluate_benchmark
from literature_radar.research.citation import expand_citations
from literature_radar.research.metadata import enrich_metadata
from literature_radar.research.review import (prepare_review, prepare_reevaluation,
                                              import_review as import_research_review)
from literature_radar.research.knowledge import export_knowledge
from literature_radar.research.report import render_review_report
from literature_radar.delivery.notifiers import configured_notifiers
from literature_radar.delivery.obsidian import (binding_file, create_dedicated_vault,
    defer_setup, deliver_cards, deliver_generated, discover_vaults, doctor as obsidian_doctor, migrate_staged, open_location,
    preview_new_vault, rename_delivered_cards, select_existing_vault, status as obsidian_status)
from literature_radar.database.backup import backup_database, restore_database
from literature_radar.database.academic import AcademicStore
from literature_radar.private_paths import ensure_private_path
from literature_radar.scheduler.runner import run_due
from literature_radar.scheduler.systemd import generate_weekly_units
from literature_radar.topics.manager import initialize as initialize_topic, describe as describe_topic, list_topics
from literature_radar.topics.focus import set_focus, focus_history
from literature_radar.journal.registry import import_registry, load_registry, match_quality
from literature_radar.workflow.bootstrap import run_bootstrap, export_review
from literature_radar.workflow.weekly import run_weekly, activate_weekly
from literature_radar.workflow.multi_topic import run_bootstraps, run_weeklies
from literature_radar.workflow.weekly_scheduler import run_scheduled
from literature_radar.attention.calibration import training_status, training_history, rollback_calibration
from literature_radar.attention.compatibility import import_ai_os_profile, import_ai_os_evidence
from literature_radar.workflow.state import read_state
from literature_radar.feedback.review import import_review, feedback_summary, propose_memory, apply_memory
from literature_radar.feedback.cards import import_cards


def _validation_payload(report) -> dict:
    return {
        "status": "PASS",
        "schemas_validated": report.schemas_validated,
        "configuration_files_validated": report.files_validated,
        "cross_references_checked": report.cross_references_checked,
        "errors": 0,
        "topic_id": report.bundle["topic"]["id"],
    }


def _weekly_exit_code(result: dict, command: str) -> int:
    if command == "run-scheduled":
        # A bounded Provider checkpoint is a normal partial cycle. A failed
        # Topic, configuration error, or concurrent lock remains observable.
        topics = result.get("topics", [])
        if result.get("status") == "DRY_RUN":
            return 0 if topics and not result.get("configuration_errors") else 3
        accepted = {"SUCCESS", "SUCCESS_WITH_REVIEW_CONFLICTS", "NOT_DUE",
                    "CATCHUP_REQUIRED", "PARTIAL_SUCCESS",
                    "PARTIAL_SUCCESS_CATCHUP_REQUIRED"}
        if (result.get("status") not in {"SUCCESS", "PARTIAL"} or not topics or
                any(topic.get("status") not in accepted or
                    any(run.get("status") not in accepted for run in topic.get("runs", []))
                    for topic in topics)):
            return 3
        return 0
    return 0 if result["status"] in ("SUCCESS", "SUCCESS_WITH_REVIEW_CONFLICTS", "NOT_DUE", "DRY_RUN") else 3


def _interactive_obsidian_setup(config_path: Path, topic_ids: tuple[str, ...] = ()) -> dict:
    """Every mode is an explicit user choice; discovery only supplies candidates."""
    vaults = discover_vaults()
    print("Obsidian setup: 1. Create a new dedicated vault (recommended)\n"
          "                2. Select an existing vault\n"
          "                3. Configure later")
    answer = input("Choose 1, 2, or 3 [1]: ").strip() or "1"
    if answer == "3":
        return defer_setup(config=config_path)
    if answer == "1":
        parent = Path(input("Vault parent directory (e.g. ~/Documents): ").strip()).expanduser()
        name = input("Vault name [Literature-Radar]: ").strip() or "Literature-Radar"
        target = preview_new_vault(parent, name, registered=vaults)
        print(f"New Vault path: {target}")
        if input("Create and bind this dedicated Vault? [y/N] ").strip().lower() not in ("y", "yes"):
            return {**defer_setup(config=config_path), "preview_path": str(target)}
        return create_dedicated_vault(parent, name, config=config_path,
                                      topic_ids=topic_ids, registered=vaults, rebind=True)
    if answer == "2":
        if vaults:
            print("Registered Vault candidates:")
            for index, vault in enumerate(vaults, 1):
                print(f"{index}. {vault.name}: {vault}")
        response = input("Vault number or absolute path: ").strip()
        if response.isdigit() and 1 <= int(response) <= len(vaults):
            chosen = vaults[int(response) - 1]
        elif response:
            chosen = Path(response).expanduser()
        else:
            raise ValueError("Select an existing Vault path explicitly")
        print(f"Existing Vault: {chosen.resolve()}\nDelivery namespace: {chosen.resolve() / 'Literature-Radar'}")
        if input("Bind this existing Vault and use only the Literature-Radar namespace? [y/N] ").strip().lower() not in ("y", "yes"):
            return defer_setup(config=config_path)
        return select_existing_vault(chosen, config=config_path, topic_ids=topic_ids, rebind=True)
    raise ValueError("Choose 1, 2, or 3")


def _first_run_obsidian_setup() -> None:
    """Offer the full setup menu; never infer binding from discovered Vaults."""
    config_path = binding_file()
    if config_path.exists():
        return
    if not sys.stdin.isatty():
        print("OBSIDIAN_NOT_CONNECTED: run literature-radar obsidian setup to create, select, or defer a Vault", file=sys.stderr)
        return
    print(json.dumps(_interactive_obsidian_setup(config_path), ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="literature-radar")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    legacy = sub.add_parser("validate", help="Stage 1 validation alias")
    legacy.add_argument("--config", type=Path, required=True)

    config = sub.add_parser("config", help="Configuration operations")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", help="Validate configuration without running discovery")
    validate.add_argument("--config-dir", type=Path, required=True)
    validate.add_argument("--format", choices=("text", "json"), default="text")

    database_cmd = sub.add_parser("database", help="Initialize a private SQLite store")
    database_sub = database_cmd.add_subparsers(dest="database_command", required=True)
    database_init = database_sub.add_parser("init")
    database_init.add_argument("--database", type=Path, required=True)

    topic = sub.add_parser("topic", help="Create and inspect private research Topics")
    topic_sub = topic.add_subparsers(dest="topic_command", required=True)
    topic_init = topic_sub.add_parser("init")
    topic_init.add_argument("--config-root", type=Path, required=True)
    topic_init.add_argument("--input", type=Path, help="User-owned JSON intake form")
    topic_init.add_argument("--id")
    topic_init.add_argument("--name")
    topic_init.add_argument("--goal")
    for option in ("question", "target-species", "model-species", "method", "application",
                   "transfer-direction", "exclusion", "journal-category", "search-term", "source"):
        topic_init.add_argument(f"--{option}", action="append")
    topic_init.add_argument("--weekly-alert-limit", type=int, help=argparse.SUPPRESS)
    topic_init.add_argument("--notification", choices=("none", "obsidian", "desktop"))
    topic_init.add_argument("--disable-weekly", action="store_true",
                            help="Keep this Topic out of the scheduled Weekly scan after activation")
    topic_validate = topic_sub.add_parser("validate")
    topic_validate.add_argument("--config-dir", type=Path, required=True)
    topic_validate.add_argument("--format", choices=("text", "json"), default="text")
    topic_show = topic_sub.add_parser("show")
    topic_show.add_argument("--config-dir", type=Path, required=True)
    topic_list = topic_sub.add_parser("list")
    topic_list.add_argument("--config-root", type=Path, required=True)
    topic_focus = topic_sub.add_parser("set-focus")
    topic_focus.add_argument("--config-dir", type=Path, required=True)
    topic_focus.add_argument("--search-term", action="append", required=True)
    topic_focus.add_argument("--reason", required=True)
    topic_history = topic_sub.add_parser("focus-history")
    topic_history.add_argument("--config-dir", type=Path, required=True)
    topic_search = topic_sub.add_parser("import-ai-os-search")
    topic_search.add_argument("--config-dir", type=Path, required=True)
    topic_search.add_argument("--ai-os-root", type=Path, required=True)
    topic_search.add_argument("--dry-run", action="store_true")

    execute = sub.add_parser("run-offline")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument("--fixture", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--database", type=Path)
    execute.add_argument("--memory", type=Path)
    execute.add_argument("--run-dir", type=Path)

    discover = sub.add_parser("discover", help="Explicit multi-provider academic discovery")
    discover.add_argument("--config-dir", type=Path, required=True)
    discover.add_argument("--topic-id")
    discover.add_argument("--database", type=Path)
    discover.add_argument("--providers", required=True)
    discover.add_argument("--since")
    discover.add_argument("--until")
    discover.add_argument("--max-pages", type=int, default=200)
    discover.add_argument("--max-seconds", type=float, default=300)
    discover.add_argument("--page-size", type=int, default=25)
    discover.add_argument("--overlap-days", type=int, default=7)
    discover.add_argument("--lookback-days", type=int, default=30)
    discover.add_argument("--reconcile-days", type=int)
    discover.add_argument("--cache-abstracts", action="store_true")
    discover.add_argument("--memory", type=Path)
    discover.add_argument("--feedback-file", type=Path)
    discover.add_argument("--obsidian-vault", type=Path)
    discover.add_argument("--notify-desktop", action="store_true")
    discover.add_argument("--dry-run", action="store_true")
    discover.add_argument("--no-persist", action="store_true")
    discover.add_argument("--format", choices=("text", "json"), default="text")

    feedback = sub.add_parser("feedback")
    feedback.add_argument("--file", type=Path)
    feedback.add_argument("--topic")
    feedback.add_argument("--paper-key")
    feedback.add_argument("--decision")
    feedback.add_argument("--reason")
    feedback_sub = feedback.add_subparsers(dest="feedback_command")
    feedback_import = feedback_sub.add_parser("import")
    feedback_import.add_argument("--config-dir", type=Path, required=True)
    feedback_import.add_argument("--review-tsv", type=Path, required=True)
    feedback_import.add_argument("--feedback-file", type=Path)
    feedback_cards = feedback_sub.add_parser("import-cards")
    feedback_cards.add_argument("--config-dir", type=Path, required=True)
    feedback_cards.add_argument("--cards-dir", type=Path, required=True)
    feedback_cards.add_argument("--database", type=Path, required=True)
    feedback_cards.add_argument("--feedback-file", type=Path)
    feedback_cards.add_argument("--binding-file", type=Path,
                                help="Refresh the bound Obsidian Dashboard after Markdown feedback import")
    feedback_sync = feedback_sub.add_parser("sync-outbox")
    feedback_sync.add_argument("--binding-file", type=Path, required=True)
    feedback_sync.add_argument("--config-dir", type=Path, action="append", required=True)
    feedback_sync.add_argument("--database", type=Path, required=True)
    feedback_legacy = feedback_sub.add_parser("collect-legacy")
    feedback_legacy.add_argument("--config-dir", type=Path, required=True)
    feedback_legacy.add_argument("--database", type=Path, required=True)
    feedback_legacy.add_argument("--vault", type=Path, required=True)
    feedback_legacy.add_argument("--dry-run", action="store_true")
    feedback_batches = feedback_sub.add_parser("batch-status")
    feedback_batches.add_argument("--config-dir", type=Path, required=True)
    feedback_stats = feedback_sub.add_parser("summary")
    feedback_stats.add_argument("--config-dir", type=Path, required=True)
    feedback_stats.add_argument("--feedback-file", type=Path)

    memory = sub.add_parser("memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_propose = memory_sub.add_parser("propose")
    memory_propose.add_argument("--config-dir", type=Path, required=True)
    memory_propose.add_argument("--feedback-file", type=Path)
    memory_propose.add_argument("--output", type=Path, required=True)
    memory_apply = memory_sub.add_parser("apply")
    memory_apply.add_argument("--config-dir", type=Path, required=True)
    memory_apply.add_argument("--proposal", type=Path, required=True)
    memory_apply.add_argument("--confirm", action="store_true")

    journal = sub.add_parser("journal", help="User-supplied CAS/JCR registry")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)
    journal_import = journal_sub.add_parser("import")
    journal_import.add_argument("--input", type=Path, required=True)
    journal_import.add_argument("--registry", type=Path, required=True)
    journal_show = journal_sub.add_parser("show")
    journal_show.add_argument("--registry", type=Path, required=True)
    journal_show.add_argument("--issn", action="append")
    journal_show.add_argument("--issn-l")
    journal_show.add_argument("--journal-title")
    journal_show.add_argument("--edition-year", type=int)

    bootstrap = sub.add_parser("bootstrap")
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_command", required=True)
    for command in ("run", "resume"):
        item = bootstrap_sub.add_parser(command)
        item.add_argument("--config-dir", type=Path, required=True)
        item.add_argument("--database", type=Path, required=True)
        item.add_argument("--output-dir", type=Path, required=True)
        item.add_argument("--journal-registry", type=Path,
                          help="Deprecated compatibility option; Bootstrap ignores journal ratings")
        item.add_argument("--providers", default="openalex,crossref,europepmc,pubmed,biorxiv")
        item.add_argument("--max-pages", type=int, default=200)
        item.add_argument("--max-seconds", type=float, default=1800)
        item.add_argument("--limit", type=int, default=30)
        item.add_argument("--dry-run", action="store_true")
    bootstrap_many = bootstrap_sub.add_parser("run-many", help="Run independent Bootstrap jobs for multiple Topics")
    bootstrap_many.add_argument("--config-dir", type=Path, action="append", required=True)
    bootstrap_many.add_argument("--database", type=Path, required=True)
    bootstrap_many.add_argument("--output-root", type=Path, required=True)
    bootstrap_many.add_argument("--providers", default="openalex,crossref,europepmc,pubmed,biorxiv")
    bootstrap_many.add_argument("--max-workers", type=int, default=4)
    bootstrap_many.add_argument("--max-pages", type=int, default=200)
    bootstrap_many.add_argument("--max-seconds", type=float, default=1800)
    bootstrap_many.add_argument("--limit", type=int, default=30)
    bootstrap_many.add_argument("--dry-run", action="store_true")
    bootstrap_status = bootstrap_sub.add_parser("status")
    bootstrap_status.add_argument("--config-dir", type=Path, required=True)
    bootstrap_export = bootstrap_sub.add_parser("export-review")
    bootstrap_export.add_argument("--output-dir", type=Path, required=True)

    weekly = sub.add_parser("weekly")
    weekly_sub = weekly.add_subparsers(dest="weekly_command", required=True)
    weekly_activate = weekly_sub.add_parser("activate")
    weekly_activate.add_argument("--config-dir", type=Path, required=True)
    weekly_activate.add_argument("--skip-bootstrap", action="store_true")
    weekly_activate.add_argument("--reason")
    weekly_status = weekly_sub.add_parser("status")
    weekly_status.add_argument("--config-dir", type=Path, required=True)
    weekly_run = weekly_sub.add_parser("run")
    weekly_run.add_argument("--config-dir", type=Path, required=True)
    weekly_run.add_argument("--database", type=Path, required=True)
    weekly_run.add_argument("--output-dir", type=Path, required=True)
    weekly_run.add_argument("--cards-dir", type=Path,
                            help="Obsidian card root; defaults to output-dir/cards")
    weekly_run.add_argument("--journal-registry", type=Path,
                            help="Deprecated compatibility option; Weekly ignores journal ratings")
    weekly_run.add_argument("--providers", default="openalex,crossref,europepmc,pubmed,biorxiv")
    weekly_run.add_argument("--max-pages", type=int, default=200)
    weekly_run.add_argument("--max-seconds", type=float, default=900)
    weekly_run.add_argument("--force", action="store_true")
    weekly_run.add_argument("--dry-run", action="store_true")
    weekly_run.add_argument("--preview-notifications", action="store_true",
                            help="Deliver real cards without queueing or sending desktop alerts")
    weekly_many = weekly_sub.add_parser("run-many", help="Run independent Weekly jobs for multiple Topics")
    weekly_many.add_argument("--config-dir", type=Path, action="append", required=True)
    weekly_many.add_argument("--database", type=Path, required=True)
    weekly_many.add_argument("--output-root", type=Path, required=True)
    weekly_many.add_argument("--cards-root", type=Path)
    weekly_many.add_argument("--providers", default="openalex,crossref,europepmc,pubmed,biorxiv")
    weekly_many.add_argument("--max-workers", type=int, default=4)
    weekly_many.add_argument("--max-pages", type=int, default=200)
    weekly_many.add_argument("--max-seconds", type=float, default=900)
    weekly_many.add_argument("--force", action="store_true")
    weekly_many.add_argument("--dry-run", action="store_true")
    weekly_many.add_argument("--preview-notifications", action="store_true",
                             help="Deliver real cards without queueing or sending desktop alerts")
    weekly_scheduled = weekly_sub.add_parser("run-scheduled", help="Run all enabled due Topics at their planned Weekly times")
    weekly_scheduled.add_argument("--config-root", type=Path, required=True)
    weekly_scheduled.add_argument("--database", type=Path, required=True)
    weekly_scheduled.add_argument("--output-root", type=Path, required=True)
    weekly_scheduled.add_argument("--cards-dir", type=Path,
                                  help="Shared date/Topic/level Weekly card archive")
    weekly_scheduled.add_argument("--state-file", type=Path)
    weekly_scheduled.add_argument("--history-file", type=Path)
    weekly_scheduled.add_argument("--summary-file", type=Path)
    weekly_scheduled.add_argument("--lock-file", type=Path)
    weekly_scheduled.add_argument("--providers", default="openalex,crossref,europepmc,pubmed,biorxiv")
    weekly_scheduled.add_argument("--max-pages", type=int, default=200)
    weekly_scheduled.add_argument("--max-seconds", type=float, default=900)
    weekly_scheduled.add_argument("--dry-run", action="store_true")
    weekly_scheduled.add_argument("--preview-notifications", action="store_true")
    weekly_scheduled.add_argument("--timezone", default="America/New_York",
                                  help="IANA timezone for the planned weekly slot")
    weekly_scheduled.add_argument("--weekday", type=int, choices=range(7), default=0,
                                  help="0=Monday through 6=Sunday")
    weekly_scheduled.add_argument("--local-time", default="09:00", help="24-hour HH:MM")
    weekly_units = weekly_sub.add_parser("systemd-generate", help="Create non-overwriting user service and timer units")
    weekly_units.add_argument("--unit-dir", type=Path, required=True)
    weekly_units.add_argument("--cli", type=Path, required=True,
                              help="Installed literature-radar executable in the target environment")
    weekly_units.add_argument("--config-root", type=Path, required=True)
    weekly_units.add_argument("--database", type=Path, required=True)
    weekly_units.add_argument("--output-root", type=Path, required=True)
    weekly_units.add_argument("--cards-dir", type=Path, required=True)
    weekly_units.add_argument("--unit-name", default="literature-radar-open-weekly")
    weekly_units.add_argument("--timezone", default="America/New_York")
    weekly_units.add_argument("--weekday", type=int, choices=range(7), default=0)
    weekly_units.add_argument("--local-time", default="09:00")
    weekly_units.add_argument("--preview-notifications", action="store_true")
    attention = sub.add_parser("attention", help="Inspect and roll back Topic calibration")
    attention_sub = attention.add_subparsers(dest="attention_command", required=True)
    for name in ("training-status", "training-history", "rollback", "import-ai-os-profile", "import-ai-os-evidence"):
        item = attention_sub.add_parser(name)
        item.add_argument("--config-dir", type=Path, required=True)
        if name == "rollback":
            item.add_argument("--version", required=True)
        if name in {"import-ai-os-profile", "import-ai-os-evidence"}:
            item.add_argument("--ai-os-root", type=Path, required=True)
            item.add_argument("--dry-run", action="store_true")

    schedule = sub.add_parser("schedule", help="Run due Topic/Provider pairs once")
    research = sub.add_parser("research", help="Optional research discovery outside Weekly")
    research_sub = research.add_subparsers(dest="research_command", required=True)
    for name in ("targeted", "deep-baseline", "recall-audit", "recall-benchmark", "citation-expand", "enrich-metadata"):
        item = research_sub.add_parser(name)
        item.add_argument("--config-dir", type=Path, required=True)
        item.add_argument("--database", type=Path, required=True)
        if name == "enrich-metadata":
            item.add_argument("--paper-id", action="append", required=True)
            item.add_argument("--providers", default="crossref,pubmed,openalex,europepmc")
        elif name == "citation-expand":
            item.add_argument("--paper-id", action="append")
            item.add_argument("--direction", choices=("references", "citations", "both"), default="both")
            item.add_argument("--max-per-seed", type=int, default=100)
            item.add_argument("--providers", default="openalex,opencitations")
            item.add_argument("--run-dir", type=Path, help="Create a strict human evaluation packet for related papers")
        elif name == "recall-audit":
            item.add_argument("--seeds", type=Path, required=True)
        elif name == "recall-benchmark":
            item.add_argument("--benchmark-dir", type=Path, required=True)
            item.add_argument("--output", type=Path, required=True)
        else:
            item.add_argument("--since", required=True)
            item.add_argument("--until", required=True)
            item.add_argument("--providers", default="openalex,pubmed,europepmc,biorxiv")
            item.add_argument("--max-pages", type=int, default=8)
            item.add_argument("--max-seconds", type=float, default=120)
            item.add_argument("--dry-run", action="store_true")
            item.add_argument("--run-dir", type=Path, help="Create a strict human evaluation packet for found papers")
            if name == "targeted":
                item.add_argument("--term", required=True)
            else:
                item.add_argument("--variants-per-group", type=int, default=4)
    research_prepare = research_sub.add_parser("prepare-evaluation", help="Create a strict human research evaluation packet")
    research_prepare.add_argument("--config-dir", type=Path, required=True)
    research_prepare.add_argument("--database", type=Path, required=True)
    research_prepare.add_argument("--run-dir", type=Path, required=True)
    research_prepare.add_argument("--paper-id", action="append", required=True)
    research_prepare.add_argument("--mode", choices=("targeted", "deep-baseline", "citation-expand",
                                                    "citation-reevaluation"), default="targeted")
    research_import = research_sub.add_parser("import-evaluation", help="Validate a complete human evaluation response")
    research_import.add_argument("--run-dir", type=Path, required=True)
    research_import.add_argument("--input", type=Path, required=True)
    research_import.add_argument("--database", type=Path,
                                 help="Override the original private database path after relocation")
    research_reeval = research_sub.add_parser("prepare-reevaluation", help="Reassess metadata-only citation papers after recovery")
    research_reeval.add_argument("--source-run-dir", type=Path, required=True)
    research_reeval.add_argument("--config-dir", type=Path, required=True)
    research_reeval.add_argument("--database", type=Path, required=True)
    research_reeval.add_argument("--run-dir", type=Path, required=True)
    research_export = research_sub.add_parser("export-knowledge", help="Export selected notes after final human review")
    research_export.add_argument("--run-dir", type=Path, required=True)
    research_export.add_argument("--output-root", type=Path, required=True)
    research_export.add_argument("--confirm", action="store_true", required=True)
    research_export.add_argument("--build-graph", action="store_true")
    research_report = research_sub.add_parser("render-report", help="Render a completed human research review")
    research_report.add_argument("--run-dir", type=Path, required=True)
    research_report.add_argument("--output", type=Path, required=True)
    schedule.add_argument("--schedule-file", type=Path, required=True)
    schedule.add_argument("--database", type=Path, required=True)
    schedule.add_argument("--dry-run", action="store_true")

    backup = sub.add_parser("backup", help="Create a verified, non-overwriting SQLite backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore", help="Restore into a new database path")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    retry = sub.add_parser("retry-notifications", help="Reset failed notification attempts after fixing a channel")
    retry.add_argument("--database", type=Path, required=True)
    retry.add_argument("--channel", choices=("obsidian", "desktop"))
    obsidian = sub.add_parser("obsidian", help="Discover, bind, inspect and open an Obsidian Vault")
    obsidian_sub = obsidian.add_subparsers(dest="obsidian_command", required=True)
    for name in ("setup", "status", "doctor", "open", "deliver", "migrate", "rename-cards"):
        item = obsidian_sub.add_parser(name)
        item.add_argument("--binding-file", type=Path, help="Private binding configuration file")
        if name == "setup":
            item.add_argument("--vault", type=Path, help="Explicit Vault root path")
            item.add_argument("--mode", choices=("create", "existing", "later"),
                              help="Create a dedicated Vault, select an existing Vault, or defer")
            item.add_argument("--parent-dir", type=Path, help="Parent directory for a new dedicated Vault")
            item.add_argument("--name", default="Literature-Radar", help="Name of a new dedicated Vault")
            item.add_argument("--topic-id", action="append", default=[], help="Initialize this Topic directory")
            item.add_argument("--config-dir", type=Path, action="append", default=[], help="Read a Topic ID from a private configuration")
            item.add_argument("--yes", action="store_true", help="Confirm the selected Vault noninteractively")
        if name == "deliver":
            item.add_argument("--cards-dir", type=Path, required=True)
            item.add_argument("--section", choices=("Bootstrap", "Weekly"), required=True)
        if name == "migrate":
            item.add_argument("--source-dir", type=Path, action="append", required=True,
                              help="Complete staged Topic report directory containing cards/")
            item.add_argument("--expected-per-topic", type=int, default=30)
            item.add_argument("--database", type=Path, help="Read-only metadata lookup for journal names")
            item.add_argument("--yes", action="store_true", help="Confirm backup and copy into the bound Vault")
    args = parser.parse_args(argv)
    try:
        running_bootstrap = args.command == "bootstrap" and args.bootstrap_command in ("run", "resume", "run-many")
        running_weekly = args.command == "weekly" and args.weekly_command in ("run", "run-many")
        if running_bootstrap or running_weekly:
            if not getattr(args, "dry_run", False):
                directories = getattr(args, "config_dir", [])
                if isinstance(directories, Path):
                    directories = [directories]
                if not directories or not all((Path(directory).parent / "obsidian.json").is_file()
                                              for directory in directories):
                    _first_run_obsidian_setup()
        if args.command == "obsidian":
            config_path = args.binding_file or binding_file()
            if args.obsidian_command == "setup":
                topic_ids = tuple(dict.fromkeys([*args.topic_id,
                    *(load_bundle(directory)["topic"]["id"] for directory in args.config_dir)]))
                mode = args.mode or ("existing" if args.vault else None)
                if mode == "later":
                    result = defer_setup(config=config_path)
                elif mode in ("create", "existing"):
                    if not args.yes:
                        if not sys.stdin.isatty():
                            raise ValueError("Explicit --yes confirmation is required for noninteractive Vault creation or binding")
                        print(f"Mode: {mode}; Vault: {args.vault if mode == 'existing' else args.parent_dir / args.name if args.parent_dir else 'choose parent'}")
                        if input("Confirm Vault setup? [y/N] ").strip().lower() not in ("y", "yes"):
                            result = defer_setup(config=config_path)
                        else:
                            args.yes = True
                    if args.yes:
                        if mode == "create":
                            if args.parent_dir is None:
                                raise ValueError("--parent-dir is required to create a dedicated Vault")
                            target = preview_new_vault(args.parent_dir, args.name)
                            print(f"New Vault path: {target}", file=sys.stderr)
                            result = create_dedicated_vault(args.parent_dir, args.name,
                                config=config_path, topic_ids=topic_ids, rebind=True)
                        else:
                            if args.vault is None:
                                raise ValueError("--vault is required to select an existing Vault")
                            result = select_existing_vault(args.vault, config=config_path,
                                                           topic_ids=topic_ids, rebind=True)
                elif sys.stdin.isatty():
                    result = _interactive_obsidian_setup(config_path, topic_ids)
                else:
                    raise ValueError("Choose --mode create --parent-dir PATH, --mode existing --vault PATH, or --mode later")
                print(json.dumps(result, ensure_ascii=False))
            elif args.obsidian_command == "status":
                print(json.dumps(obsidian_status(config=config_path), ensure_ascii=False, indent=2))
            elif args.obsidian_command == "doctor":
                print(json.dumps(obsidian_doctor(config=config_path), ensure_ascii=False, indent=2))
            elif args.obsidian_command == "open":
                print(json.dumps(open_location(config=config_path), ensure_ascii=False))
            elif args.obsidian_command == "migrate":
                confirmed = args.yes or (sys.stdin.isatty() and
                    input("Back up staged Topic reports and copy cards into the bound Vault? [y/N] ").strip().lower() in ("y", "yes"))
                result = migrate_staged(args.source_dir, config=config_path,
                    expected_per_topic=args.expected_per_topic, confirmed=confirmed,
                    database=args.database)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result["status"] != "SUCCESS":
                    return 2
            elif args.obsidian_command == "rename-cards":
                print(json.dumps(rename_delivered_cards(config=config_path), ensure_ascii=False, indent=2))
            else:
                if not args.cards_dir.is_dir():
                    raise ValueError("Cards directory does not exist")
                result = deliver_generated(args.cards_dir, section=args.section, config=config_path)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result["status"] != "SUCCESS":
                    return 2
        elif args.command == "topic":
            if args.topic_command == "init":
                specification = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
                if not isinstance(specification, dict):
                    raise ValueError("Topic intake must be a JSON object")
                mappings = {"question": "questions", "target_species": "target_species",
                            "model_species": "model_species", "method": "methods",
                            "application": "applications", "transfer_direction": "transfer_directions",
                            "exclusion": "exclusions", "journal_category": "journal_categories",
                            "search_term": "search_terms", "source": "sources"}
                for argument, field in mappings.items():
                    if getattr(args, argument) is not None:
                        specification[field] = getattr(args, argument)
                for argument in ("id", "name", "goal", "weekly_alert_limit", "notification"):
                    if getattr(args, argument) is not None:
                        specification[argument] = getattr(args, argument)
                if args.disable_weekly:
                    specification["weekly_enabled"] = False
                prompts = {"id": "Topic ID (lowercase letters and hyphens)",
                           "name": "Topic name", "goal": "Research goal",
                           "questions": "Core scientific questions (semicolon separated)",
                           "target_species": "Target organisms (semicolon separated)",
                           "model_species": "Acceptable model organisms (semicolon separated)",
                           "methods": "Important methods (semicolon separated)",
                           "applications": "Applications (semicolon separated)",
                           "transfer_directions": "Cross-domain transfer directions (semicolon separated)",
                           "exclusions": "Unwanted research directions (semicolon separated)",
                           "journal_categories": "Journal subject categories (semicolon separated)",
                           "notification": "Notification none/obsidian/desktop [none]"}
                if not args.input and sys.stdin.isatty():
                    for field, prompt in prompts.items():
                        if field in specification:
                            continue
                        answer = input(prompt + ": ").strip()
                        if field == "notification":
                            specification[field] = answer or "none"
                        elif field in ("questions", "target_species", "model_species", "methods", "applications", "transfer_directions", "exclusions", "journal_categories"):
                            specification[field] = [item.strip() for item in answer.split(";") if item.strip()]
                        elif answer:
                            specification[field] = answer
                destination = initialize_topic(args.config_root, specification)
                print(json.dumps({"status": "PASS", "config_dir": str(destination),
                                  "topic_id": destination.name}, ensure_ascii=False))
            elif args.topic_command == "validate":
                report = validate_config_dir(args.config_dir)
                payload = _validation_payload(report)
                if args.format == "json":
                    print(json.dumps(payload, ensure_ascii=False))
                else:
                    print(f"Topic validation: PASS ({payload['topic_id']}); {payload['configuration_files_validated']} files; {payload['cross_references_checked']} references")
            elif args.topic_command == "show":
                print(json.dumps(describe_topic(args.config_dir), ensure_ascii=False, indent=2))
            elif args.topic_command == "set-focus":
                print(json.dumps(set_focus(args.config_dir, args.search_term,
                                           reason=args.reason), ensure_ascii=False))
            elif args.topic_command == "focus-history":
                print(json.dumps(focus_history(args.config_dir), ensure_ascii=False))
            elif args.topic_command == "import-ai-os-search":
                print(json.dumps(import_ai_os_search(args.config_dir, args.ai_os_root,
                                                     dry_run=args.dry_run), ensure_ascii=False))
            else:
                print(json.dumps(list_topics(args.config_root), ensure_ascii=False, indent=2))
        elif args.command == "journal":
            if args.journal_command == "import":
                print(json.dumps({"imported": import_registry(args.input, args.registry)}))
            else:
                print(json.dumps(match_quality(load_registry(args.registry), issn=args.issn,
                                               issn_l=args.issn_l,
                                               journal_title=args.journal_title,
                                               edition_year=args.edition_year), ensure_ascii=False))
        elif args.command == "bootstrap":
            if args.bootstrap_command == "status":
                print(json.dumps(read_state(args.config_dir), ensure_ascii=False))
            elif args.bootstrap_command == "export-review":
                report = json.loads((args.output_dir / "bootstrap-report.json").read_text(encoding="utf-8"))
                print(json.dumps(export_review(args.output_dir, report), ensure_ascii=False))
            elif args.bootstrap_command == "run-many":
                print(json.dumps(run_bootstraps(
                    args.config_dir, args.database, args.output_root,
                    provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                    max_workers=args.max_workers, max_pages=args.max_pages,
                    max_seconds=args.max_seconds, limit=args.limit,
                    dry_run=args.dry_run), ensure_ascii=False))
            else:
                print(json.dumps(run_bootstrap(
                    args.config_dir, args.database, args.output_dir,
                    registry_path=args.journal_registry,
                    provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                    max_pages=args.max_pages, max_seconds=args.max_seconds,
                    limit=args.limit, dry_run=args.dry_run,
                    resume=args.bootstrap_command == "resume"), ensure_ascii=False))
        elif args.command == "memory":
            if args.memory_command == "propose":
                result = propose_memory(args.config_dir,
                                        args.feedback_file or args.config_dir / "feedback.json",
                                        args.output)
            else:
                result = apply_memory(args.config_dir, args.proposal, confirmed=args.confirm)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "attention":
            if args.attention_command == "training-status":
                result = training_status(args.config_dir)
            elif args.attention_command == "training-history":
                result = training_history(args.config_dir)
            elif args.attention_command == "rollback":
                result = rollback_calibration(args.config_dir, args.version)
            elif args.attention_command == "import-ai-os-evidence":
                result = import_ai_os_evidence(args.ai_os_root, args.config_dir,
                                               dry_run=args.dry_run)
            else:
                result = import_ai_os_profile(args.ai_os_root, args.config_dir,
                                              dry_run=args.dry_run)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "weekly":
            if args.weekly_command == "activate":
                result = activate_weekly(args.config_dir, skip_bootstrap=args.skip_bootstrap,
                                         reason=args.reason)
            elif args.weekly_command == "status":
                result = read_state(args.config_dir)
            elif args.weekly_command == "run-scheduled":
                result = run_scheduled(args.config_root, args.database, args.output_root,
                                       state_file=args.state_file, history_file=args.history_file,
                                       summary_file=args.summary_file, lock_file=args.lock_file,
                                       cards_dir=args.cards_dir,
                                       provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                                       max_pages=args.max_pages, max_seconds=args.max_seconds,
                                       dry_run=args.dry_run,
                                       notification_preview=args.preview_notifications,
                                       timezone=args.timezone, weekday=args.weekday,
                                       local_time=args.local_time)
            elif args.weekly_command == "systemd-generate":
                result = generate_weekly_units(
                    args.unit_dir, cli=args.cli, config_root=args.config_root,
                    database=args.database, output_root=args.output_root,
                    cards_dir=args.cards_dir, unit_name=args.unit_name,
                    timezone=args.timezone, weekday=args.weekday,
                    local_time=args.local_time,
                    preview_notifications=args.preview_notifications)
            elif args.weekly_command == "run-many":
                result = run_weeklies(args.config_dir, args.database, args.output_root,
                                      provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                                      max_workers=args.max_workers, max_pages=args.max_pages,
                                      max_seconds=args.max_seconds, force=args.force,
                                      dry_run=args.dry_run, cards_root=args.cards_root,
                                      notification_preview=args.preview_notifications)
            else:
                result = run_weekly(args.config_dir, args.database, args.output_dir,
                                    registry_path=args.journal_registry,
                                    provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                                    max_pages=args.max_pages, max_seconds=args.max_seconds,
                                    force=args.force, dry_run=args.dry_run,
                                    cards_dir=args.cards_dir,
                                    notification_preview=args.preview_notifications)
            print(json.dumps(result, ensure_ascii=False))
            return (0 if args.weekly_command in {"activate", "status", "systemd-generate"}
                    else _weekly_exit_code(result, args.weekly_command))
        elif args.command == "config":
            report = validate_config_dir(args.config_dir)
            payload = _validation_payload(report)
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print("Configuration validation: PASS")
                print(f"Schemas validated: {payload['schemas_validated']}")
                print(f"Configuration files validated: {payload['configuration_files_validated']}")
                print(f"Cross-references checked: {payload['cross_references_checked']}")
                print("Errors: 0")
        elif args.command == "database":
            path = ensure_private_path(args.database)
            AcademicStore(path)
            print(json.dumps({"status": "READY", "database": str(path)}))
        elif args.command == "validate":
            print(json.dumps({"topic_id": load_bundle(args.config)["topic"]["id"], "valid": True}))
        elif args.command == "discover":
            bundle = load_bundle(args.config_dir)
            if args.topic_id and bundle["topic"]["id"] != args.topic_id:
                raise ValueError("--topic-id does not match configuration")
            channels, notifiers = ([], {}) if args.dry_run or args.no_persist else configured_notifiers(
                bundle, vault_override=args.obsidian_vault, desktop=args.notify_desktop)
            result = discover_incremental(
                args.config_dir, [name.strip() for name in args.providers.split(",") if name.strip()],
                database_path=args.database, since=args.since, until=args.until,
                max_pages=args.max_pages, page_size=args.page_size,
                overlap_days=args.overlap_days, lookback_days=args.lookback_days,
                reconcile_days=args.reconcile_days, dry_run=args.dry_run,
                no_persist=args.no_persist, cache_abstracts=args.cache_abstracts,
                max_seconds=args.max_seconds, strict_records=True,
                memory_path=args.memory, feedback_path=args.feedback_file,
                channels=channels, notifiers=notifiers)
            if args.format == "json":
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"Discovery: {result['status']}")
                for name, state in result["providers"].items():
                    print(f"{name}: {state['status']} pages={state['pages']} records={state['records']}")
            return 0 if result["status"] == "SUCCESS" else 3
        elif args.command == "run-offline":
            result = run(
                args.config, FixtureProvider(args.fixture), args.output,
                database_path=args.database, memory_path=args.memory, run_directory=args.run_dir,
            )
            print(json.dumps({"topic_id": result["topic_id"], "count": result["count"]}))
        elif args.command == "research":
            if args.research_command == "prepare-evaluation":
                result = prepare_review(args.config_dir, args.database, args.run_dir,
                                        paper_ids=args.paper_id, mode=args.mode)
            elif args.research_command == "import-evaluation":
                result = import_research_review(args.run_dir, args.input,
                                                database=args.database)
            elif args.research_command == "prepare-reevaluation":
                result = prepare_reevaluation(args.source_run_dir, args.config_dir,
                                              args.database, args.run_dir)
            elif args.research_command == "export-knowledge":
                result = export_knowledge(args.run_dir, args.output_root,
                                          confirmed=args.confirm,
                                          build_graph=args.build_graph)
            elif args.research_command == "render-report":
                result = render_review_report(args.run_dir, args.output)
            elif args.research_command == "recall-audit":
                seeds = json.loads(args.seeds.read_text(encoding="utf-8"))
                if not isinstance(seeds, list):
                    raise ValueError("recall seeds must be a JSON array")
                result = recall_audit(args.config_dir, args.database, seeds)
            elif args.research_command == "recall-benchmark":
                result = evaluate_benchmark(args.config_dir, args.database,
                                            args.benchmark_dir, args.output)
            elif args.research_command == "citation-expand":
                result = expand_citations(args.config_dir, args.database,
                                          paper_ids=args.paper_id,
                                          direction=args.direction,
                                          max_per_seed=args.max_per_seed,
                                          provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                                          evaluation_run_dir=args.run_dir)
            elif args.research_command == "enrich-metadata":
                result = enrich_metadata(args.config_dir, args.database,
                                         paper_ids=args.paper_id,
                                         provider_names=[name.strip() for name in args.providers.split(",") if name.strip()])
            else:
                result = run_specialty(
                    args.config_dir, args.database, mode=args.research_command,
                    since=args.since, until=args.until,
                    term=args.term if args.research_command == "targeted" else None,
                    provider_names=[name.strip() for name in args.providers.split(",") if name.strip()],
                    variants_per_group=(args.variants_per_group
                                        if args.research_command == "deep-baseline" else 4),
                    max_pages=args.max_pages, max_seconds=args.max_seconds,
                    dry_run=args.dry_run, evaluation_run_dir=args.run_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("status", "SUCCESS") in {
                "SUCCESS", "DRY_RUN", "AWAITING_HUMAN_EVALUATION",
                "EVALUATED_AWAITING_FINAL_HUMAN_REVIEW",
                "EXPORTED_AFTER_HUMAN_CONFIRMATION",
                "NO_REEVALUATION_CANDIDATES", "REPORT_READY"} else 3
        elif args.command == "schedule":
            result = run_due(args.schedule_file, args.database, dry_run=args.dry_run)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "SUCCESS" else 3
        elif args.command == "backup":
            print(json.dumps({"backup": str(backup_database(args.database, args.output))}))
        elif args.command == "restore":
            print(json.dumps({"restored": str(restore_database(args.backup, args.output))}))
        elif args.command == "retry-notifications":
            if not args.database.is_file():
                raise ValueError("notification database does not exist")
            print(json.dumps({"reset": AcademicStore(args.database).retry_failed_notifications(
                channel=args.channel)}))
        elif args.command == "feedback":
            if args.feedback_command == "import":
                result = import_review(args.config_dir, args.review_tsv,
                                       args.feedback_file or args.config_dir / "feedback.json")
            elif args.feedback_command == "import-cards":
                refresh_section = None
                if args.binding_file is not None:
                    delivery = obsidian_status(config=args.binding_file)
                    if delivery.get("status") != "PASS":
                        raise ValueError("Bound Obsidian Vault is not healthy")
                    root = Path(delivery["delivery_root"])
                    refresh_section = next((section for section in ("Bootstrap", "Weekly")
                                            if args.cards_dir.resolve().is_relative_to(root / section)), None)
                    if refresh_section is None:
                        raise ValueError("Feedback cards must be inside the bound Bootstrap or Weekly directory")
                result = import_cards(args.config_dir, args.cards_dir, args.database,
                                      args.feedback_file, allow_revisions=True)
                result["attention_batch"] = prepare_due_batches(args.config_dir,
                    cards_root=args.cards_dir if args.cards_dir.name == load_bundle(args.config_dir)["topic"]["id"] else None)
                if args.binding_file is not None:
                    result["dashboard_refresh"] = deliver_cards([], section=refresh_section,
                                                                 config=args.binding_file)["status"]
            elif args.feedback_command == "collect-legacy":
                result = collect_legacy_cards(args.config_dir, args.database,
                                              args.vault, dry_run=args.dry_run)
            elif args.feedback_command == "sync-outbox":
                result = sync_outbox(args.binding_file, args.config_dir, args.database)
            elif args.feedback_command == "batch-status":
                result = batch_progress(args.config_dir)
            elif args.feedback_command == "summary":
                result = feedback_summary(args.config_dir,
                                          args.feedback_file or args.config_dir / "feedback.json")
            else:
                if not all((args.file, args.topic, args.paper_key, args.decision, args.reason)):
                    raise ValueError("Legacy feedback needs --file, --topic, --paper-key, --decision and --reason")
                result = add_feedback(args.file, args.topic, args.paper_key,
                                      args.decision, args.reason)
            print(json.dumps(result, ensure_ascii=False))
    except ValueError as exc:
        if not isinstance(exc, ConfigurationError):
            print(f"Discovery configuration: FAIL [{type(exc).__name__}]", file=sys.stderr)
            return 2
        payload = {"status": "FAIL", "category": exc.category, "file": exc.file,
                   "field": exc.field, "error": str(exc), "suggestion": exc.correction}
        if args.command in ("config", "topic") and getattr(args, "format", None) == "json":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"Configuration validation: FAIL [{exc.category}] {exc}", file=sys.stderr)
            if exc.correction:
                print(f"Suggested correction: {exc.correction}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.DatabaseError) as exc:
        print(f"Operation: FAIL [{type(exc).__name__}]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
