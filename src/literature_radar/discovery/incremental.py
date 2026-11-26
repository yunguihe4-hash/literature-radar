"""Explicit online orchestration with source-level failure isolation."""
from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_attention_memory, load_bundle
from literature_radar.database.academic import AcademicStore, CheckpointConflict
from literature_radar.delivery.engine import dispatch_outbox
from literature_radar.feedback.store import load_feedback
from literature_radar.discovery.events import NEW_PAPER, MATERIAL_UPDATE, REVIEW_CONFLICT, content_version
from literature_radar.candidates.processing import identity as paper_key
from literature_radar.providers.models import DateWindow
from literature_radar.sources.factory import make_provider
from literature_radar.sources.http import RequestFailure
from literature_radar.discovery.query_planner import planned_queries


def _date(value: str | None, fallback: date) -> date:
    return date.fromisoformat(value) if value else fallback


def _malformed_reference(raw, exc: Exception) -> dict[str, str]:
    """Keep a traceable, content-free audit entry for an uncommitted record."""
    payload = raw.payload
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       default=str).encode("utf-8")).hexdigest()
    identifier = next((str(payload[key]) for key in
                       ("id", "DOI", "doi", "pmid", "preprint_doi", "biorxiv_doi")
                       if isinstance(payload.get(key), (str, int)) and payload[key]), None)
    if identifier is None and isinstance(payload.get("xml"), str):
        found = re.search(r"<PMID(?:\s[^>]*)?>(\d+)</PMID>", payload["xml"])
        identifier = found.group(1) if found else None
    category = (exc.category if isinstance(exc, RequestFailure) else
                str(exc).replace(" ", "_") if isinstance(exc, ValueError) and
                str(exc) in ("missing source_id", "missing title") else type(exc).__name__)
    result = {"category": category, "payload_sha256": digest}
    if identifier is not None:
        result["identifier"] = identifier[:200]
    return result


def discover_incremental(config_dir: Path, provider_names: list[str], *,
                         database_path: Path | None = None, since: str | None = None,
                         until: str | None = None, max_pages: int = 200,
                         page_size: int = 25, overlap_days: int = 7,
                         lookback_days: int = 30, reconcile_days: int | None = None,
                         dry_run: bool = False, no_persist: bool = False,
                         cache_abstracts: bool = False,
                         providers: Mapping[str, object] | None = None,
                         max_seconds: float = 300,
                         strict_records: bool = False,
                         memory_path: Path | None = None,
                         feedback_path: Path | None = None,
                         channels: list[str] | None = None,
                         notifiers: Mapping[str, object] | None = None,
                         current_time: datetime | None = None,
                         defer_notifications: bool = False,
                         queue_events: bool = True) -> dict:
    if max_pages < 1 or page_size < 1 or overlap_days < 0 or lookback_days < 1:
        raise ValueError("invalid discovery limits")
    if reconcile_days is not None and reconcile_days < 1:
        raise ValueError("reconcile_days must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    if not (dry_run or no_persist) and database_path is None:
        raise ValueError("--database is required unless --dry-run or --no-persist is used")
    if current_time is not None and current_time.tzinfo is None:
        raise ValueError("current_time must include a timezone")
    bundle = load_bundle(config_dir)
    topic = bundle["topic"]
    memory = (load_attention_memory(memory_path, bundle) if memory_path is not None else
              bundle.get("attention-memory"))
    feedback = load_feedback(feedback_path) if feedback_path is not None else {"entries": []}
    decisions = {entry["paper_key"]: entry["decision"] for entry in feedback["entries"]
                 if entry["topic_id"] == topic["id"]}
    now = (current_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = _date(until, now.date())
    if since and _date(since, end) > end:
        raise ValueError("--since must be on or before --until")
    store = None if dry_run or no_persist else AcademicStore(database_path, cache_abstracts=cache_abstracts)
    selected = list(dict.fromkeys(provider_names))
    if not selected:
        raise ValueError("select at least one provider")
    summary = {"topic_id": topic["id"], "status": "SUCCESS", "providers": {},
               "papers_seen": 0, "priority_counts": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
               "paper_event_counts": {}, "recommendations": [], "notifications": {"queued": 0, "sent": 0, "failed": 0}}
    all_papers = {}
    all_events = []
    local_ids = {}
    priorities = topic.get("extensions", {}).get("field_priority", {})
    def evaluate(paper, event_type, version=None):
        decision = decisions.get(paper.paper_id) or decisions.get(paper_key(paper))
        result = recommend(paper, bundle, memory, decision, config_dir=config_dir)
        result.update({"topic_id": topic["id"], "event_type": event_type,
                       "content_version": version or content_version(paper),
                       "paper": {"paper_id": paper.paper_id, "title": paper.title,
                                 "authors": list(paper.authors),
                                 "publication_date": paper.publication_date,
                                 "sources": sorted({item.provider_id for item in paper.source_records}),
                                 "doi": paper.doi, "landing_url": paper.landing_url},
                       "provenance": {key: value.as_dict() for key, value in paper.provenance.items()
                                      if key in ("title", "abstract", "authors", "publication_date")}})
        return result
    for name in selected:
        # A slow or failed source must not consume another source's runtime.
        # Page checkpoints remain scoped to each original query signature.
        deadline = time.monotonic() + max_seconds
        item = {"status": "SUCCESS", "pages": 0, "records": 0,
                "new_records": 0, "updated_records": 0, "unchanged_records": 0,
                "malformed": 0, "quarantined": 0, "malformed_records": [],
                "conflicts": 0, "sync_capability": ""}
        summary["providers"][name] = item
        try:
            provider = providers[name] if providers is not None else make_provider(
                name, page_size=page_size,
                options=topic.get("extensions", {}).get("provider_options", {}).get(name, {}))
            item["sync_capability"] = provider.sync_capability
            query_id = f"{topic['id']}:{name}"
            previous = store.latest_provider_success(name, topic["id"]) if store else None
            start = _date(since, end - timedelta(days=lookback_days))
            if not since and previous and previous["status"] == "SUCCESS":
                previous_window = DateWindow(previous["window_start"], previous["window_end"])
                same_query = any(
                    candidate.query_id == previous["query_id"] and
                    candidate.query_signature == previous["query_signature"]
                    for candidate in planned_queries(provider, bundle, previous_window,
                                                     config_dir=config_dir))
                if same_query:
                    start = date.fromisoformat(previous["window_end"]) - timedelta(days=overlap_days)
            if reconcile_days is not None:
                start = min(start, end - timedelta(days=reconcile_days))
            window = DateWindow(start.isoformat(), end.isoformat())
            plans = planned_queries(provider, bundle, window, config_dir=config_dir)
            runnable = []
            for plan in plans:
                # Resume only a plan with exactly the same signature and fixed window.
                if store and not since:
                    active = store.latest_checkpoint(name, topic["id"], plan.query_id)
                    if active and active["status"] in ("IN_PROGRESS", "FAILED"):
                        prior_window = DateWindow(active["window_start"], active["window_end"])
                        resumed = planned_queries(provider, bundle, prior_window,
                                                  config_dir=config_dir)
                        match = next((p for p in resumed if p.query_id == plan.query_id), None)
                        if match and match.query_signature == active["query_signature"]:
                            plan = match
                checkpoint = store.checkpoint(name, topic["id"], plan.query_signature) if store else None
                if (checkpoint is None and store and name == "crossref" and
                        plan.query_parameters.get("pagination_mode") == "offset"):
                    # A legacy cursor scan must retain its original signature
                    # and committed cursor. Translating a partial cursor to an
                    # offset would replay pages in a potentially different order.
                    legacy = provider.equivalent_cursor_plan(plan)
                    prior = store.checkpoint(name, topic["id"], legacy.query_signature)
                    if (prior and prior["query_id"] == plan.query_id and
                            prior["window_start"] == plan.date_window.start and
                            prior["window_end"] == plan.date_window.end):
                        if prior["status"] == "SUCCESS":
                            item.setdefault("reused_completed_cursor_windows", []).append({
                                "start": plan.date_window.start,
                                "end": plan.date_window.end,
                                "source_signature": legacy.query_signature})
                            continue
                        if prior["status"] in ("IN_PROGRESS", "FAILED"):
                            item.setdefault("resumed_cursor_windows", []).append({
                                "start": plan.date_window.start,
                                "end": plan.date_window.end,
                                "source_signature": legacy.query_signature,
                                "page_number": prior["page_number"]})
                            plan, checkpoint = legacy, prior
                if checkpoint and checkpoint["status"] == "SUCCESS":
                    continue
                runnable.append((plan, checkpoint))
            for plan_index, (plan, checkpoint) in enumerate(runnable):
                # Completed groups never reserve time. After each runnable
                # group, divide the remaining Provider budget among only the
                # groups still needing pages.
                now = time.monotonic()
                plan_deadline = now + max(0.0, deadline - now) / (len(runnable) - plan_index)
                cursor = json.loads(checkpoint["cursor_json"]) if checkpoint else None
                expected_page_number = checkpoint["page_number"] if checkpoint else 0
                committed_cursor = json.loads(checkpoint["cursor_json"]) if checkpoint and checkpoint["cursor_json"] else None
                if dry_run:
                    item.setdefault("query_signatures", []).append(plan.query_signature)
                    continue
                for _ in range(max_pages):
                    if time.monotonic() >= plan_deadline:
                        item["status"] = "INCOMPLETE"
                        item.setdefault("incomplete_reasons", []).append({
                            "query_id": plan.query_id, "query_signature": plan.query_signature,
                            "reason": "time_budget_exhausted"})
                        break
                    try:
                        # Live HTTP requests and retries must respect this
                        # query group's fair share of the Provider budget.
                        from literature_radar.sources.http import HTTPClient
                        client = getattr(provider, "client", None)
                        bounded_client = isinstance(client, HTTPClient)
                        previous_deadline = client.budget_deadline if bounded_client else None
                        if bounded_client:
                            client.budget_deadline = min(plan_deadline, previous_deadline) if previous_deadline else plan_deadline
                        try:
                            page = provider.fetch_page(plan, cursor)
                        finally:
                            if bounded_client:
                                client.budget_deadline = previous_deadline
                    except Exception as exc:
                        category = exc.category if isinstance(exc, RequestFailure) else type(exc).__name__
                        item.setdefault("failed_queries", []).append({
                            "query_id": plan.query_id,
                            "query_signature": plan.query_signature,
                            "error_category": category})
                        item.setdefault("error_category", category)
                        item.setdefault("error", str(exc)[:300])
                        if store:
                            store.mark_failed(topic["id"], plan,
                                              expected_page_number=expected_page_number)
                        break
                    normalized = []
                    page_malformed = []
                    fatal_malformed = 0
                    for raw in page.records:
                        try:
                            normalized.append(provider.normalize(raw))
                        except (ValueError, TypeError, KeyError, AttributeError, IndexError, RequestFailure) as exc:
                            item["malformed"] += 1
                            entry = _malformed_reference(raw, exc)
                            item["malformed_records"].append(entry)
                            page_malformed.append(entry)
                            if (strict_records and entry["category"] == "missing_title" and
                                    entry.get("identifier")):
                                item["quarantined"] += 1
                            else:
                                fatal_malformed += 1
                    if strict_records and fatal_malformed:
                        raise RequestFailure(name, "malformed_record")
                    if store:
                        events = {}
                        page_events = []
                        papers, conflicts = store.commit_page(topic["id"], plan, page, normalized,
                                                               priorities=priorities, event_counts=events,
                                                               malformed_records=page_malformed,
                                                               expected_page_number=expected_page_number,
                                                               expected_cursor=committed_cursor,
                                                               paper_events=page_events,
                                                               evaluator=evaluate if queue_events else None,
                                                               queue_events=queue_events)
                        all_events.extend(page_events)
                        expected_page_number += 1
                        committed_cursor = page.next_cursor
                        item["conflicts"] += conflicts
                        for event in ("new", "updated", "unchanged"):
                            item[event + "_records"] += events[event]
                    else:
                        from literature_radar.candidates.identity import IdentityConflict, identifiers, merge
                        import uuid
                        papers = []
                        for record in normalized:
                            keys = tuple(identifiers(record).items()) or ((record.provider_id, record.source_id),)
                            paper_id = next((local_ids[key] for key in keys if key in local_ids), uuid.uuid4().hex)
                            try:
                                paper = merge(all_papers.get(paper_id), record, paper_id, priorities)
                            except IdentityConflict:
                                item["conflicts"] += 1
                                continue
                            for key in keys: local_ids[key] = paper_id
                            papers.append(paper)
                    for paper in papers:
                        all_papers[paper.paper_id] = paper
                    item["pages"] += 1
                    item["records"] += len(page.records)
                    cursor = page.next_cursor
                    if not page.has_more:
                        break
                else:
                    item["status"] = "INCOMPLETE"
                    item.setdefault("incomplete_reasons", []).append({
                        "query_id": plan.query_id, "query_signature": plan.query_signature,
                        "reason": "page_limit_exhausted"})
            if item.get("failed_queries"):
                if len(item["failed_queries"]) == len(plans):
                    item["status"] = "FAILED"
                elif item["status"] == "SUCCESS":
                    item["status"] = "PARTIAL_SUCCESS"
            if (item["status"] == "SUCCESS" and
                    (item["malformed"] > item["quarantined"] or item["conflicts"])):
                item["status"] = "PARTIAL_SUCCESS"
        except Exception as exc:
            item["status"] = "FAILED"
            item["error_category"] = (exc.category if isinstance(exc, RequestFailure) else
                                      "concurrent_checkpoint" if isinstance(exc, CheckpointConflict) else
                                      type(exc).__name__)
            item["error"] = str(exc)[:300]
            if store and not isinstance(exc, CheckpointConflict) and "plan" in locals() and plan.provider_id == name:
                try: store.mark_failed(topic["id"], plan,
                                       expected_page_number=expected_page_number if "expected_page_number" in locals() else None)
                except Exception: pass
        if item["status"] != "SUCCESS":
            summary["status"] = "PARTIAL_SUCCESS"
    if all(item["status"] == "FAILED" for item in summary["providers"].values()):
        summary["status"] = "FAILED"
    summary["papers_seen"] = len(all_papers)
    grouped = {}
    for event in all_events:
        if event["event_type"] == REVIEW_CONFLICT:
            summary["paper_event_counts"][REVIEW_CONFLICT] = summary["paper_event_counts"].get(REVIEW_CONFLICT, 0) + 1
            continue
        prior = grouped.get(event["paper_id"])
        if prior is None or (event["event_type"] == NEW_PAPER or
                             prior["event_type"] not in (NEW_PAPER, MATERIAL_UPDATE)):
            grouped[event["paper_id"]] = event
    for event in grouped.values():
        kind = event["event_type"]
        summary["paper_event_counts"][kind] = summary["paper_event_counts"].get(kind, 0) + 1
    for paper in all_papers.values():
        result = evaluate(paper, grouped.get(paper.paper_id, {}).get("event_type", "UNCHANGED"))
        summary["priority_counts"][result["priority"]] += 1
        summary["recommendations"].append({"paper_id": paper.paper_id,
                                           "event_type": result["event_type"],
                                           "priority": result["priority"],
                                           "eligible": result["eligible"],
                                           "reason": {key: result["reason"][key] for key in
                                                      ("questions", "methods", "applications", "transfers")}})
    if store:
        if not defer_notifications:
            summary["notifications"]["queued"] = store.queue_pending(topic["id"], channels or [])
            if notifiers:
                summary["notifications"].update(dispatch_outbox(store, notifiers))
        store.record_run(topic["id"], summary)
    return summary
