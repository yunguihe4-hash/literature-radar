"""Scheduled Weekly cycles, separate from Provider page recovery.

The schedule cursor names a planned Monday. A Provider checkpoint names an
unfinished query within that cycle. Neither cursor can stand in for the other.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from literature_radar.config import load_bundle
from literature_radar.workflow.state import read_state
from literature_radar.workflow.weekly import run_weekly

ZONE = ZoneInfo("America/New_York")
HEADER = "# Weekly Literature Radar History\n\n|scheduled_time|actual_time|topic|run_id|status|candidate_count|\n|---|---|---|---|---|---|\n"
OLD_HEADER = "|scheduled_time|topic|run_id|status|candidate_count|"


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json(path: Path, value: dict) -> None:
    _atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _history(path: Path) -> str:
    if not path.exists():
        return HEADER
    value = path.read_text(encoding="utf-8")
    if not value.startswith("# Weekly Literature Radar History"):
        raise ValueError("Unsupported Weekly history format")
    if "|scheduled_time|actual_time|topic|run_id|status|candidate_count|" in value:
        return value
    if OLD_HEADER not in value:
        raise ValueError("Unsupported Weekly history columns")
    rows = []
    for line in value.splitlines():
        if line.startswith("|") and line not in (OLD_HEADER, "|---|---|---|---|---|"):
            cells = line.strip("|").split("|")
            if len(cells) != 5:
                raise ValueError("Invalid legacy Weekly history row")
            rows.append("|" + "|".join((cells[0], "-", *cells[1:])) + "|")
    return HEADER + ("\n".join(rows) + "\n" if rows else "")


def _local(value: str | datetime, zone: ZoneInfo = ZONE) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=zone)
    return instant.astimezone(zone).replace(microsecond=0)


def _last_monday(actual: datetime, zone: ZoneInfo = ZONE,
                 weekday: int = 0, local_time: time = time(9)) -> datetime:
    """Most recent configured weekly slot; original Monday 09:00 is the default."""
    local = actual.astimezone(zone)
    candidate = datetime.combine(local.date() - timedelta(days=(local.weekday() - weekday) % 7),
                                 local_time, zone)
    return candidate if candidate <= local else candidate - timedelta(days=7)


def discover_enabled(config_root: Path) -> tuple[list[tuple[str, Path, int]], list[dict]]:
    """Old enabled Topic semantics, with default enabled for pre-flag bundles."""
    found, errors = [], []
    for path in sorted(Path(config_root).glob("*/topic.json")):
        try:
            bundle = load_bundle(path.parent)
            topic = bundle["topic"]
            if topic["id"] != path.parent.name:
                raise ValueError("Topic ID differs from directory")
            if topic.get("enabled", topic.get("extensions", {}).get("enabled", True)) is False:
                continue
            if read_state(path.parent)["state"] != "WEEKLY_MONITORING":
                continue
            days = int(topic.get("extensions", {}).get("weekly", {}).get("primary_days", 7))
            if days != 7:
                raise ValueError("Weekly primary_days must be 7")
            found.append((topic["id"], path.parent, days))
        except (OSError, ValueError, KeyError) as exc:
            errors.append({"topic_id": path.parent.name, "error": str(exc)[:300]})
    return found, errors


def _state(path: Path, actual: datetime) -> dict:
    if not path.exists():
        return {"schema_version": 2, "topics": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") == 2 and isinstance(value.get("topics"), dict):
        return value
    if "topic" not in value:
        raise ValueError("Unsupported Weekly scheduler state")
    topic = str(value.pop("topic"))
    value.setdefault("last_attempted_run", None)
    value.setdefault("last_successful_run", None)
    value.setdefault("last_run_id", None)
    value.setdefault("last_status", "MIGRATED")
    value.setdefault("last_error", None)
    return {"schema_version": 2, "topics": {topic: value}}


def run_scheduled(config_root: Path, database: Path, output_root: Path, *,
                  now: datetime | None = None, state_file: Path | None = None,
                  history_file: Path | None = None, summary_file: Path | None = None,
                  lock_file: Path | None = None, cards_dir: Path | None = None,
                  provider_names: list[str] | None = None,
                  providers_by_topic: dict | None = None,
                  notifiers_by_topic: dict | None = None,
                  max_pages: int = 200, max_seconds: float = 900,
                  dry_run: bool = False, notification_preview: bool = False,
                  runner=None, timezone: str = "America/New_York",
                  weekday: int = 0, local_time: str = "09:00") -> dict:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown IANA timezone") from exc
    if weekday not in range(7):
        raise ValueError("weekday must be 0 (Monday) through 6 (Sunday)")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise ValueError("local_time must be HH:MM in 24-hour format")
    hour, minute = (int(part) for part in local_time.split(":"))
    planned_time = time(hour, minute)
    actual = _local(now or datetime.now(zone), zone)
    config_root, output_root = Path(config_root), Path(output_root)
    state_file = Path(state_file or output_root / "weekly_schedule.json")
    history_file = Path(history_file or output_root / "weekly_history.md")
    summary_file = Path(summary_file or output_root / "weekly_summary.json")
    lock_file = Path(lock_file or output_root / "weekly.lock")
    topics, errors = discover_enabled(config_root)
    if dry_run:
        return {"status": "DRY_RUN", "scheduled_at": actual.isoformat(),
                "topics": [topic for topic, _, _ in topics], "configuration_errors": errors}
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "LOCKED", "topics": []}
        state = _state(state_file, actual)
        for topic, _, days in topics:
            item = state["topics"].setdefault(topic, {})
            item.setdefault("next_scheduled_time", _last_monday(actual, zone, weekday, planned_time).isoformat())
            item["lookback_days"] = days
            for key, value in (("last_attempted_run", None), ("last_successful_run", None),
                               ("last_run_id", None), ("last_status", "NEVER_RUN"),
                               ("last_error", None)):
                item.setdefault(key, value)
            _local(item["next_scheduled_time"], zone)
        _json(state_file, state)
        history = _history(history_file)
        if not history_file.exists() or history_file.read_text(encoding="utf-8") != history:
            _atomic(history_file, history)
        results = []
        failed = bool(errors)
        execute = runner or run_weekly
        for error in errors:
            results.append({**error, "status": "FAILED", "runs": []})
        for topic, config, _ in topics:
            item, cycles = state["topics"][topic], []
            while _local(item["next_scheduled_time"], zone) <= actual:
                planned = _local(item["next_scheduled_time"], zone)
                try:
                    prior_state = read_state(config)
                    prior_report_path = output_root / topic / "weekly-report.json"
                    already_complete = (
                        (prior_state.get("last_weekly_window") or {}).get("end") == planned.date().isoformat()
                        and not prior_state.get("active_weekly_window")
                        and not prior_state.get("pending_provider_windows")
                        and prior_report_path.is_file())
                    report = json.loads(prior_report_path.read_text(encoding="utf-8")) if already_complete else execute(
                        config, database, output_root / topic,
                        now=actual, scheduled_at=planned,
                        provider_names=provider_names,
                        providers=(providers_by_topic or {}).get(topic),
                        notifiers=(notifiers_by_topic or {}).get(topic),
                        max_pages=max_pages, max_seconds=max_seconds,
                        cards_dir=cards_dir,
                        notification_preview=notification_preview)
                    # A crash may leave an older active window. Finish it before
                    # processing this planned cycle, preserving its checkpoint.
                    if report.get("overlap_window", {}).get("end") != planned.date().isoformat():
                        if read_state(config).get("active_weekly_window"):
                            raise RuntimeError("Prior active Weekly window remains unresolved")
                        report = execute(
                            config, database, output_root / topic,
                            now=actual, scheduled_at=planned, force=True,
                            provider_names=provider_names,
                            providers=(providers_by_topic or {}).get(topic),
                            notifiers=(notifiers_by_topic or {}).get(topic),
                            max_pages=max_pages, max_seconds=max_seconds,
                            cards_dir=cards_dir,
                            notification_preview=notification_preview)
                    status = report["status"]
                    if status not in {"SUCCESS", "SUCCESS_WITH_REVIEW_CONFLICTS",
                                      "CATCHUP_REQUIRED", "PARTIAL_SUCCESS",
                                      "PARTIAL_SUCCESS_CATCHUP_REQUIRED", "INCOMPLETE"}:
                        raise RuntimeError(f"Weekly returned {status}")
                    # A partial source keeps its exact window in Topic pending
                    # state. The schedule may advance so healthy sources run
                    # next week independently.
                    item["next_scheduled_time"] = (planned + timedelta(days=7)).isoformat()
                    item["last_attempted_run"] = actual.isoformat()
                    item["last_run_id"] = report.get("run_id")
                    item["last_status"] = status
                    item["last_error"] = None
                    if report.get("coverage_complete"):
                        item["last_successful_run"] = actual.isoformat()
                    candidate_count = report.get("candidate_count", 0)
                    record = {"topic_id": topic, "scheduled_time": planned.isoformat(),
                              "actual_time": actual.isoformat(), "run_id": report.get("run_id"),
                              "status": status, "candidate_count": candidate_count,
                              "pending_provider_windows": report.get("pending_provider_windows", []),
                              "recommended_count": report.get("recommended_count", 0)}
                except Exception as exc:
                    failed = True
                    item.update(last_attempted_run=actual.isoformat(), last_status="FAILED",
                                last_error=str(exc)[:300])
                    record = {"topic_id": topic, "scheduled_time": planned.isoformat(),
                              "actual_time": actual.isoformat(), "run_id": item.get("last_run_id"),
                              "status": "FAILED", "candidate_count": 0, "error": str(exc)[:300]}
                history_row = (f"|{planned.isoformat()}|{actual.isoformat()}|{topic}|"
                               f"{record.get('run_id') or '-'}|{record['status']}|"
                               f"{record['candidate_count']}|")
                if history_row not in history.splitlines():
                    history += history_row + "\n"
                    _atomic(history_file, history)
                _json(state_file, state)
                cycles.append(record)
                if record["status"] == "FAILED":
                    break
            results.append({"topic_id": topic, "status": cycles[-1]["status"] if cycles else "NOT_DUE",
                            "runs": cycles, "next_scheduled_time": item["next_scheduled_time"]})
        summary = {"schema_version": 1, "actual_time": actual.isoformat(),
                   "status": "PARTIAL" if failed or any(r["status"] in
                       {"PARTIAL_SUCCESS", "PARTIAL_SUCCESS_CATCHUP_REQUIRED", "INCOMPLETE"}
                       for r in results) else "SUCCESS",
                   "topics": results}
        _json(summary_file, summary)
        return summary
