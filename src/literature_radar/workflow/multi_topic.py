"""Independent Topic jobs; a failed Topic never cancels the others."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Mapping

from literature_radar.config import load_bundle
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.workflow.bootstrap import run_bootstrap
from literature_radar.workflow.state import read_state
from literature_radar.workflow.weekly import run_weekly


def _topics(config_dirs):
    items = {}
    for directory in config_dirs:
        directory = Path(directory)
        topic_id = load_bundle(directory)["topic"]["id"]
        if topic_id in items:
            raise ValueError("duplicate Topic ID in multi-Topic job")
        items[topic_id] = directory
    return items


def run_bootstraps(config_dirs, database: Path, output_root: Path, *,
                   providers_by_topic: Mapping[str, Mapping[str, object]] | None = None,
                   provider_names: list[str] | None = None, now: datetime | None = None,
                   max_workers: int = 4, limit: int = 30,
                   max_pages: int = 200, max_seconds: float = 1800,
                   dry_run: bool = False) -> dict:
    topics = _topics(config_dirs)
    if not topics:
        return {"status": "EMPTY", "topics": {}}
    if max_workers < 1:
        raise ValueError("max_workers must be positive")

    def one(topic_id, directory):
        state = read_state(directory)
        if state["state"] in {"BOOTSTRAP_REVIEW_READY", "FEEDBACK_IMPORTED", "MEMORY_REVIEW", "WEEKLY_MONITORING"}:
            return {"topic_id": topic_id, "status": "ALREADY_READY", "report": state.get("report")}
        result = run_bootstrap(directory, database, Path(output_root) / topic_id,
                               providers=(providers_by_topic or {}).get(topic_id),
                               provider_names=provider_names, now=now,
                               max_pages=max_pages, max_seconds=max_seconds, limit=limit,
                               dry_run=dry_run,
                               resume=state["state"] == "BOOTSTRAP_RUNNING")
        return {**result, "status": "DRY_RUN" if dry_run else result["completeness"]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(topics))) as pool:
        futures = {pool.submit(one, topic_id, path): topic_id for topic_id, path in topics.items()}
        for future in as_completed(futures):
            topic_id = futures[future]
            try: results[topic_id] = future.result()
            except Exception as exc:
                results[topic_id] = {"topic_id": topic_id, "status": "FAILED",
                                     "error_category": type(exc).__name__, "error": str(exc)[:300]}
    return {"status": "SUCCESS" if all(r["status"] in {"COMPLETE", "ALREADY_READY", "DRY_RUN"} for r in results.values())
            else "PARTIAL", "topics": results}


def run_weeklies(config_dirs, database: Path, output_root: Path, *,
                 providers_by_topic: Mapping[str, Mapping[str, object]] | None = None,
                 provider_names: list[str] | None = None,
                 notifiers_by_topic: Mapping[str, Mapping[str, object]] | None = None,
                 now: datetime | None = None, max_workers: int = 4,
                 scheduled_at: datetime | None = None,
                 force: bool = False, dry_run: bool = False,
                 max_pages: int = 200, max_seconds: float = 900,
                 cards_root: Path | None = None,
                 notification_preview: bool = False) -> dict:
    topics = _topics(config_dirs)
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    # One import covers every Topic. Importing the shared Bridge outbox from
    # concurrent Topic workers can race on the same JSON file and Dashboard.
    feedback_sync = {"status": "NOT_RUN" if dry_run else "NO_BINDING"}
    binding_path = next(iter(topics.values())).parent / "obsidian.json" if topics else None
    if not dry_run and binding_path is not None and binding_path.is_file():
        try:
            feedback_sync = sync_outbox(binding_path, list(topics.values()), database)
        except (OSError, ValueError) as exc:
            feedback_sync = {"status": "FAILED", "error": str(exc)}
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1,len(topics)))) as pool:
        futures = {pool.submit(run_weekly, path, database, Path(output_root) / topic_id,
                               providers=(providers_by_topic or {}).get(topic_id),
                               provider_names=provider_names,
                               notifiers=(notifiers_by_topic or {}).get(topic_id),
                               now=now, scheduled_at=scheduled_at, force=force, dry_run=dry_run,
                               max_pages=max_pages, max_seconds=max_seconds,
                               cards_dir=(Path(cards_root) if cards_root else Path(output_root)/topic_id/"cards"),
                               sync_feedback=False,
                               notification_preview=notification_preview): topic_id
                   for topic_id, path in topics.items()}
        for future in as_completed(futures):
            topic_id = futures[future]
            try:
                results[topic_id] = future.result()
                if not dry_run:
                    results[topic_id]["feedback_sync"] = feedback_sync
            except Exception as exc:
                results[topic_id] = {"topic_id": topic_id, "status": "FAILED",
                                     "error_category": type(exc).__name__, "error": str(exc)[:300]}
    return {"status": "SUCCESS" if all(r["status"] in {"SUCCESS", "SUCCESS_WITH_REVIEW_CONFLICTS", "NOT_DUE", "DRY_RUN"} for r in results.values())
            else "PARTIAL", "feedback_sync": feedback_sync, "topics": results}
