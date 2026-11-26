"""Explicit, per-Topic weekly scan with one valid-candidate minimum."""
from __future__ import annotations

import json
import hashlib
import html
import re
from difflib import SequenceMatcher
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from literature_radar.attention.recommendation import recommend
from literature_radar.attention.transfer import direct_species_evidence
from literature_radar.attention.calibration import train_due, record_evidence
from literature_radar.candidates.processing import identity as paper_key
from literature_radar.config import load_bundle
from literature_radar.database.academic import AcademicStore
from literature_radar.delivery.cards import card_key, write_card
from literature_radar.delivery.obsidian import deliver_selected, _read_binding
from literature_radar.delivery.engine import dispatch_outbox
from literature_radar.delivery.notifiers import DesktopNotifier, ObsidianNotifier, configured_notifiers
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.providers.models import DateWindow
from literature_radar.sources.factory import make_provider
from literature_radar.discovery.query_planner import planned_queries
from literature_radar.attention.batch_training import train_due_batches
from literature_radar.feedback.store import load_feedback
from literature_radar.feedback.bridge import sync_outbox
from literature_radar.feedback.legacy_cards import collect_legacy_cards
from literature_radar.private_paths import ensure_private_path
from literature_radar.workflow.bootstrap import PROVIDERS, _date_bounds, _publication_date, _write_atomic
from literature_radar.workflow.state import advance, read_state


def activate_weekly(config_dir: Path, *, skip_bootstrap: bool = False,
                    reason: str | None = None) -> dict:
    config_dir = ensure_private_path(config_dir)
    state = read_state(config_dir)
    bootstrap_ready = state["state"] in ("BOOTSTRAP_REVIEW_READY", "FEEDBACK_IMPORTED")
    memory_ready = state["state"] == "MEMORY_REVIEW" and state.get("memory_review_confirmed")
    if not (bootstrap_ready or memory_ready):
        if not skip_bootstrap or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Complete Bootstrap or explicitly record --skip-bootstrap --reason")
        state["bootstrap_skipped"] = True
        state["skip_reason"] = reason.strip()
    return advance(config_dir, "WEEKLY_MONITORING", **{
        key: state[key] for key in ("bootstrap_skipped", "skip_reason") if key in state})


def _weekly_event(paper, original: str, primary_start: date, window_end: date) -> str:
    if original == "MATERIAL_UPDATE":
        return "MATERIAL_UPDATE"
    if original in ("DUPLICATE_SOURCE", "REVIEW_CONFLICT", "UNCHANGED"):
        return original
    published, _ = _weekly_publication(paper)
    if published:
        try:
            earliest, latest = _date_bounds(published)
            if earliest == latest and primary_start <= earliest <= window_end:
                return "NEW_PAPER"
            if earliest > window_end:
                return "FUTURE_DATED"
            if latest >= primary_start:
                return "DATE_UNVERIFIED"
        except ValueError:
            pass
    if original == "NEW_PAPER":
        return "LATE_INDEXED" if published else "DATE_UNVERIFIED"
    if paper.indexed_date:
        try:
            if date.fromisoformat(paper.indexed_date[:10]) >= primary_start:
                return "LATE_INDEXED"
        except ValueError:
            pass
    return "UNCHANGED"


def _weekly_publication(paper) -> tuple[str | None, str]:
    """Return the first public release, never an indexing or revision date."""
    published, provenance = _publication_date(paper)
    if provenance == "preprint_release":
        releases = [record.normalized_metadata.get("online_date")
                    for record in paper.source_records
                    if record.provider_id == "biorxiv" and
                    record.normalized_metadata.get("online_date")]
        versions = [str(record.normalized_metadata.get("version"))
                    for record in paper.source_records if record.provider_id == "biorxiv" and
                    record.normalized_metadata.get("version") is not None]
        if versions and "1" not in versions:
            # A newly indexed v2+ record cannot establish its first posting.
            return None, "preprint_first_release_unverified"
        if releases:
            published = min(releases)
    elif paper.online_date and paper.publication_date:
        try:
            online_start, online_end = _date_bounds(paper.online_date)
            formal_start, formal_end = _date_bounds(paper.publication_date)
            if online_start == online_end and (formal_start != formal_end or
                                               online_start < formal_start):
                published, provenance = paper.online_date, "first_online"
        except ValueError:
            pass
    return published, provenance


def _recent_publication(paper, start: date, end: date) -> bool:
    published, _ = _weekly_publication(paper)
    if not published:
        return False
    try:
        earliest, latest = _date_bounds(published)
    except ValueError:
        return False
    # A year or month alone cannot prove publication within seven days.
    return earliest == latest and start <= earliest <= end


def _rank_weekly_rows(rows: list[dict]) -> list[dict]:
    """Rank solely by Research Attention and article evidence.

    Journal and JCR fields may be present as metadata but are never read here.
    """
    return sorted(rows, key=lambda row: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[row["priority"]],
                                          tuple(row["attention_rank_sort_key"]),
                                          row["title"].casefold(), row["paper_id"]))


def _select_weekly_rows(rows: list[dict]) -> list[dict]:
    """Keep qualified recommendations, then choose one Topic-evidenced card."""
    selected = [row for row in rows if row["eligible"]]
    if selected:
        return selected
    fallback = next((row for row in rows if row["basic_qualified"] and
                     row["fallback_scientific_link"]), None)
    if fallback is None:
        return []
    fallback["eligible"] = True
    fallback["priority"] = "LOW"
    fallback["scientific_qualification_before_fallback"] = fallback["scientific_qualification"]
    fallback["scientific_qualification"] = "minimum_topic_evidence_fallback"
    fallback["why_recommended"] = "文章含本 Topic 的研究问题、对象或迁移证据；相关性需人工复核。"
    return [fallback]


def _weekly_fallback_scientific_link(paper, bundle: dict, result: dict) -> str | None:
    """Require an article-level Topic link beyond generic keyword matches."""
    reason = result["reason"]
    if reason.get("contrary_evidence"):
        return None
    if reason.get("topic_corroboration"):
        return "corroborated_topic_entity"
    if any(item.get("status") == "EVIDENCE_MATCHED"
           for item in reason.get("structured_transfers", [])):
        return "evidenced_transfer"
    if reason.get("questions") and any(item.get("field") in {"title", "abstract"}
                                       for item in reason.get("evidence", [])):
        return "article_research_question"
    legacy = result["original_attention"]
    transfer = legacy.get("transfer") or {}
    if (legacy.get("attention_rank", {}).get("transfer_value") and
            transfer.get("type") in {"method_transfer", "application_transfer", "concept_transfer"} and
            transfer.get("source_domain") and transfer.get("target_problem") and
            any(item.get("field") in {"title", "abstract"} for item in reason.get("evidence", []))):
        return "original_article_transfer"
    # Insect genes often carry a species prefix (for example CpunOrco). The
    # strict general Topic matcher deliberately does not alter its semantics.
    if re.search(r"(?:^|-)orco(?:$|-)", bundle["topic"]["id"], re.I):
        text = paper.title + " " + (paper.abstract or "")
        if re.search(r"\borco\b", text, re.I) or re.search(r"\b[A-Z][a-z]{2,8}Orco\b", text):
            return "orco_gene_in_article"
    return None


def _weekly_priority(result: dict) -> str:
    """Keep provisional C papers visible without a desktop HIGH alert."""
    original = result.get("original_attention")
    if result["attention_ranking"]["category"] == "C" and result.get("decision") != "must_read":
        level = original["final_attention"] if original else result["priority"]
        return "MEDIUM" if level == "HIGH" else "LOW" if level == "IRRELEVANT" else level
    if original and result.get("decision") != "must_read":
        level = original["final_attention"]
        return "LOW" if level == "IRRELEVANT" else level
    return result["priority"]


def _weekly_scientific_qualification(paper, bundle: dict, result: dict) -> tuple[bool, str]:
    """Require article evidence, while keeping a relevant LOW distinct from irrelevant.

    The seven-dimensional original Attention score and its tier are unchanged.
    This gate decides whether that tier may appear in a weekly recommendation.
    """
    if result.get("decision") in {"discard", "later"}:
        return False, "prior_human_rejection"
    if str(paper.document_type or "").casefold() in {"retracted", "withdrawn", "retraction"} or re.match(
            r"^\s*(?:retraction|withdrawal|withdrawn)\s*[:：]", paper.title, re.I):
        return False, "retracted_or_withdrawn"
    if any(record.normalized_metadata.get(flag) is True for record in paper.source_records
           for flag in ("is_retracted", "is_withdrawn", "retracted", "withdrawn")):
        return False, "retracted_or_withdrawn"
    if result["original_attention"]["final_attention"] == "IRRELEVANT":
        return False, "original_attention_irrelevant"
    reason = result["reason"]
    category = result["attention_ranking"]["category"]
    direct = direct_species_evidence(paper, bundle)
    proven_transfer = any(item["status"] == "EVIDENCE_MATCHED"
                          for item in reason["structured_transfers"])
    if category in {"A", "B", "C"} and proven_transfer:
        return True, "article_evidenced_transfer"
    if category in {"A", "B", "C"} and direct and reason["questions"] and paper.abstract:
        return True, "direct_species_question"
    if category == "C" and reason.get("topic_corroboration") and not reason["contrary_evidence"]:
        return True, "corroborated_topic_entity"
    if (category == "C" and reason["questions"] and reason["methods"] and
            any(item["field"] == "abstract" for item in reason["evidence"]) and
            reason["rules_evidence"] and not reason["contrary_evidence"]):
        return True, "question_and_method_in_article"
    if (direct and paper.abstract and reason["questions"] and
            (reason["methods"] or reason["applications"]) and
            not reason["contrary_evidence"]):
        return True, "direct_low_with_article_evidence"
    legacy = result["original_attention"]
    legacy_rank = legacy.get("attention_rank", {})
    legacy_transfer = legacy.get("transfer", {})
    if (legacy.get("eligible") and legacy_rank.get("research_question_match") and
            legacy_rank.get("transfer_value") and
            legacy_transfer.get("type") in {"method_transfer", "application_transfer",
                                            "concept_transfer"} and
            legacy_transfer.get("source_domain") and
            (legacy.get("method_matches") or legacy.get("application_matches")) and
            reason["questions"] and not reason["contrary_evidence"] and
            paper.abstract and len(paper.abstract) >= 150 and
            any(item.get("field") == "abstract" for item in reason["evidence"])):
        # Preserve the validated original seven-dimension transfer decision.
        # A new structured transfer explanation is still marked for review;
        # this branch does not invent a missing method or change the tier.
        return True, "original_seven_dimension_article_transfer"
    return False, "topic_evidence_insufficient"


def _provider_window_covered(result: dict) -> bool:
    """A partial Provider run covers its window only when every query ran.

    Review conflicts may make the result PARTIAL_SUCCESS without leaving a
    search gap. A failed semantic query group does leave one, even if other
    groups returned papers and committed their own checkpoints.
    """
    return result.get("status") in ("SUCCESS", "PARTIAL_SUCCESS") and not result.get("failed_queries")


def _duplicate_key(paper) -> tuple[str, str] | None:
    """Recognize article versions without merging their permanent identities."""
    title = html.unescape(re.sub(r"<[^>]+>", " ", paper.title))
    title = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    author = " ".join(re.findall(r"[a-z0-9]+", paper.authors[0].casefold())) if paper.authors else ""
    return (title, author) if len(title) >= 40 and author else None


def _is_preprint(paper) -> bool:
    kind = str(paper.document_type or "").casefold()
    return kind in {"preprint", "posted-content", "posted content"} or any(
        record.provider_id.casefold() == "biorxiv" or
        record.source_id.upper().startswith("PPR:") or
        str(record.normalized_metadata.get("preprint_source") or "").upper() == "PPR"
        for record in paper.source_records)


def _author_surnames(paper) -> tuple[str, ...]:
    surnames = []
    for author in paper.authors[:2]:
        if "," in author:
            token = re.findall(r"[a-z]+", author.split(",", 1)[0].casefold())
            surname = token[-1] if token else ""
        else:
            tokens = re.findall(r"[a-z]+", author.casefold())
            surname = (tokens[0] if len(tokens) == 2 and len(tokens[1]) <= 3
                       else tokens[-1] if tokens else "")
        surnames.append(surname)
    return tuple(surnames) if len(surnames) == 2 and all(surnames) else ()


def _canonical_versions(papers) -> dict[str, str]:
    papers = list(papers)
    grouped = {}
    parent = {}

    def find(paper_id):
        parent.setdefault(paper_id, paper_id)
        if parent[paper_id] != paper_id:
            parent[paper_id] = find(parent[paper_id])
        return parent[paper_id]

    def union(left, right):
        parent[find(left)] = find(right)

    fuzzy = {}
    for paper in papers:
        key = _duplicate_key(paper)
        if key is not None:
            grouped.setdefault(key, []).append(paper)
        if len(paper.authors) < 2:
            continue
        title = re.findall(r"[a-z0-9]+", html.unescape(paper.title).casefold())
        authors = _author_surnames(paper)
        if len(title) >= 7 and authors:
            fuzzy.setdefault((tuple(title[:5]), authors), []).append((paper, title))
    for members in grouped.values():
        for paper in members[1:]:
            union(members[0].paper_id, paper.paper_id)
    for members in fuzzy.values():
        if len(members) > 20:
            continue
        preprints = [(paper, title) for paper, title in members if _is_preprint(paper)]
        published = [(paper, title) for paper, title in members if not _is_preprint(paper)]
        for preprint, pre_title in preprints:
            for article, article_title in published:
                overlap = len(set(pre_title) & set(article_title)) / len(set(pre_title) | set(article_title))
                ratio = SequenceMatcher(None, " ".join(pre_title), " ".join(article_title)).ratio()
                if overlap >= 0.8 and ratio >= 0.9:
                    union(preprint.paper_id, article.paper_id)
    components = {}
    for paper in papers:
        if paper.paper_id in parent:
            components.setdefault(find(paper.paper_id), []).append(paper)
    canonical = {}
    for members in components.values():
        if len(members) < 2:
            continue
        def rank(paper):
            doi = str(paper.doi or paper.identifiers.get("doi") or "")
            suffix = re.search(r"\.(\d+)$", doi)
            return (not _is_preprint(paper), paper.publication_date or paper.online_date or "",
                    int(suffix.group(1)) if suffix else 0, len(paper.source_records), paper.paper_id)
        winner = max(members, key=rank)
        for paper in members:
            canonical[paper.paper_id] = winner.paper_id
    return canonical


def _provider_signatures(name: str, bundle: dict, window: dict,
                         providers: Mapping[str, object] | None,
                         config_dir: Path | None = None) -> list[str]:
    provider = (providers[name] if providers is not None else make_provider(
        name, options=bundle["topic"].get("extensions", {}).get("provider_options", {}).get(name, {})))
    plans = planned_queries(provider, bundle,
                            DateWindow(window["start"], window["end"]),
                            config_dir=config_dir)
    return [plan.query_signature for plan in plans]


def run_weekly(config_dir: Path, database: Path, output_dir: Path, *,
               registry_path: Path | None = None,
               provider_names: list[str] | None = None,
               providers: Mapping[str, object] | None = None,
               notifiers: Mapping[str, object] | None = None,
               now: datetime | None = None,
               scheduled_at: datetime | None = None,
               max_pages: int = 200, max_seconds: float = 900,
               force: bool = False, dry_run: bool = False,
               cards_dir: Path | None = None,
               sync_feedback: bool = True,
               notification_preview: bool = False) -> dict:
    config_dir = ensure_private_path(config_dir)
    database = ensure_private_path(database)
    output_dir = ensure_private_path(output_dir)
    # registry_path is accepted for CLI compatibility; Weekly never reads it.
    topic_id = load_bundle(config_dir)["topic"]["id"]
    state = read_state(config_dir)
    if state["state"] != "WEEKLY_MONITORING":
        raise ValueError("Weekly monitoring requires explicit activation after review")
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if scheduled_at is not None and scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at must include a timezone")
    scheduled = scheduled_at.astimezone(timezone.utc) if scheduled_at else None
    if scheduled is not None and scheduled > instant:
        raise ValueError("scheduled_at cannot be after actual run time")
    binding_path = config_dir.parent / "obsidian.json"
    feedback_sync = {"status": "NOT_RUN" if dry_run else "NO_BINDING"}
    legacy_feedback = {"status": "NOT_RUN" if dry_run else "NO_BINDING"}
    if not dry_run and binding_path.is_file():
        try:
            binding = _read_binding(config=binding_path)
            if binding is not None:
                legacy_feedback = collect_legacy_cards(
                    config_dir, database, Path(binding["vault_root"]))
        except (OSError, ValueError) as exc:
            legacy_feedback = {"status": "FAILED", "error": str(exc)[:300]}
    if not dry_run and sync_feedback:
        if binding_path.is_file():
            try:
                feedback_sync = sync_outbox(binding_path, [config_dir], database)
            except (OSError, ValueError) as exc:
                feedback_sync = {"status": "FAILED", "error": str(exc)}
    elif not dry_run:
        feedback_sync = {"status": "PRE_SYNCED"}
    attention_training = {"status": "NOT_RUN"}
    memory_training = {"status": "NOT_RUN"}
    if not dry_run:
        try:
            attention_training = train_due(config_dir)
        except Exception as exc:
            # A failed calibration must leave the last valid Memory available
            # to discovery and must not cancel this Topic's Weekly scan.
            attention_training = {"status": "FAILED", "error_category": type(exc).__name__,
                                  "error": str(exc)[:300]}
        try:
            memory_training = train_due_batches(config_dir)
            memory_training["status"] = "TRAINED" if memory_training["trained"] else "WAITING"
        except Exception as exc:
            memory_training = {"status": "FAILED", "error_category": type(exc).__name__,
                               "error": str(exc)[:300]}
    bundle = load_bundle(config_dir)
    local_day = instant.astimezone(ZoneInfo("America/New_York")).date()
    active_window = state.get("active_weekly_window") if not force else None
    if active_window is not None and (not isinstance(active_window, dict) or
                                      not all(key in active_window for key in ("start", "end", "run_at"))):
        raise ValueError("Invalid active Weekly window")
    last = state.get("last_weekly_run_at")
    pending_windows = list(state.get("pending_provider_windows") or [])
    if last and not force and active_window is None:
        last_day = datetime.fromisoformat(last).astimezone(ZoneInfo("America/New_York")).date()
        if last_day.isocalendar()[:2] == local_day.isocalendar()[:2]:
            if not pending_windows:
                next_monday = local_day + timedelta(days=7 - local_day.weekday())
                next_due = datetime.combine(next_monday, time(9), ZoneInfo("America/New_York"))
                return {"topic_id": topic_id, "status": "NOT_DUE", "next_due_at": next_due.isoformat(),
                        "feedback_sync": feedback_sync, "legacy_feedback": legacy_feedback,
                        "attention_training": attention_training,
                        "memory_training": memory_training}
            # A same-week recovery must keep the exact original window.
            active_window = state.get("last_weekly_window")
            if not isinstance(active_window, dict):
                raise ValueError("Missing last Weekly window for pending Provider recovery")
    settings = bundle["topic"].get("extensions", {}).get("weekly", {})
    primary_days = int(settings.get("primary_days", 7))
    overlap_days = int(settings.get("overlap_days", 14))
    if primary_days < 1 or overlap_days < primary_days:
        raise ValueError("Invalid weekly window")
    selected = provider_names or list(PROVIDERS)
    if any(name not in PROVIDERS for name in selected):
        raise ValueError("Unknown weekly Provider")
    window_end = date.fromisoformat(active_window["end"]) if active_window else (
        scheduled.astimezone(ZoneInfo("America/New_York")).date() if scheduled else local_day)
    since = active_window["start"] if active_window else (window_end - timedelta(days=overlap_days)).isoformat()
    if date.fromisoformat(since) != window_end - timedelta(days=overlap_days):
        raise ValueError("Active Weekly window does not match configured overlap")
    run_at = active_window["run_at"] if active_window else (scheduled or instant).isoformat()
    if not dry_run and active_window is None:
        advance(config_dir, "WEEKLY_MONITORING", active_weekly_window={
            "start": since, "end": window_end.isoformat(), "run_at": run_at})
    # Protect the current Weekly window first. Historical recovery has a
    # separate, bounded Provider budget and cannot block current coverage.
    summary = discover_incremental(config_dir, selected, database_path=database,
                                   since=since, until=window_end.isoformat(),
                                   max_pages=max_pages, max_seconds=max_seconds,
                                   providers=providers, cache_abstracts=True,
                                   strict_records=True, channels=[], notifiers={},
                                   current_time=instant, dry_run=dry_run,
                                   defer_notifications=True)
    current_keys = {(name, since, window_end.isoformat()) for name in selected}
    current_pending = {(item["provider_id"], item["start"], item["end"]): item
                       for item in pending_windows
                       if (item["provider_id"], item["start"], item["end"]) in current_keys}
    historical_pending = [item for item in pending_windows
                          if (item["provider_id"], item["start"], item["end"]) not in current_keys]
    history_counts = {name: sum(item["provider_id"] == name for item in historical_pending)
                      for name in selected}
    recovery = []
    remaining_pending = []
    recovered_recommendations = []

    def unfinished(item, status, details=None, error_category=None):
        reasons = (details or {}).get("incomplete_reasons") or []
        failed = (details or {}).get("failed_queries") or []
        reason = (error_category or (details or {}).get("error_category") or
                  (reasons[0]["reason"] if reasons else None) or
                  (failed[0]["error_category"] if failed else None) or "window_incomplete")
        return {**item, "last_status": status, "last_reason": reason,
                "last_attempt_at": instant.isoformat()}

    for pending in historical_pending:
        name = pending.get("provider_id")
        if name not in selected:
            remaining_pending.append(pending)
            continue
        prior_window = {"start": pending["start"], "end": pending["end"]}
        budget_seconds = max_seconds / history_counts[name]
        try:
            signatures = _provider_signatures(name, bundle, prior_window, providers,
                                              config_dir=config_dir)
            # Semantic groups may be added after an older broad query was
            # checkpointed. Its original signature must still be present;
            # newly added groups have independent cursors.
            if not set(pending.get("query_signatures", [])) <= set(signatures):
                raise ValueError("original query signatures changed")
            result = discover_incremental(
                config_dir, [name], database_path=database,
                since=pending["start"], until=pending["end"],
                max_pages=max_pages, max_seconds=budget_seconds,
                providers=providers, cache_abstracts=True,
                strict_records=True, channels=[], notifiers={},
                current_time=instant, dry_run=dry_run,
                defer_notifications=True)
            provider_result = result["providers"][name]
            status = provider_result["status"]
            if status in ("SUCCESS", "PARTIAL_SUCCESS"):
                recovered_recommendations.extend(result["recommendations"])
            if not _provider_window_covered(provider_result):
                remaining_pending.append(unfinished(pending, status, provider_result))
            recovery.append({"provider_id": name, "window": prior_window,
                             "status": status, "budget_seconds": budget_seconds,
                             "details": provider_result})
        except Exception as exc:
            remaining_pending.append(unfinished(pending, "FAILED",
                                                error_category=type(exc).__name__))
            recovery.append({"provider_id": name, "window": prior_window,
                             "status": "FAILED", "budget_seconds": budget_seconds,
                             "error_category": type(exc).__name__,
                             "error": str(exc)[:300]})
    if dry_run:
        return {"topic_id": topic_id, "status": "DRY_RUN", "discovery": summary,
                "feedback_sync": feedback_sync, "legacy_feedback": legacy_feedback,
                "attention_training": attention_training,
                "memory_training": memory_training}
    store = AcademicStore(database, cache_abstracts=True)
    observed = {item["paper_id"]: item for item in
                [*recovered_recommendations, *summary["recommendations"]]}
    resumed_pending = 0
    # A process can stop after a Provider checkpoint commits but before its
    # cards are delivered. Successful Providers are skipped on retry, so the
    # unprocessed event ledger must restore their observed paper IDs.
    for event in store.pending_events(topic_id):
        if (event["paper_id"] and event["created_at"] >= run_at and
                event["paper_id"] not in observed):
            observed[event["paper_id"]] = {"paper_id": event["paper_id"],
                                            "event_type": event["event_type"]}
            resumed_pending += 1
    decisions = {item["paper_key"]: item["decision"] for item in
                 load_feedback(config_dir / "feedback.json")["entries"]
                 if item["topic_id"] == topic_id}
    rows = []
    primary_start = window_end - timedelta(days=primary_days - 1)
    links = store.preprint_links()
    topic_papers = store.topic_papers(topic_id)
    current_versions = store.topic_content_versions(topic_id)
    canonical_versions = _canonical_versions(topic_papers)
    paper_by_id = {paper.paper_id: paper for paper in topic_papers}
    doi_to_id = {str(paper.doi or paper.identifiers.get("doi") or "").casefold(): paper.paper_id
                 for paper in topic_papers if paper.doi or paper.identifiers.get("doi")}
    version_preprints = {}
    for paper in topic_papers:
        if _is_preprint(paper):
            owner = canonical_versions.get(paper.paper_id)
            if owner and owner != paper.paper_id:
                version_preprints.setdefault(owner, []).append(paper)
    for preprint_id, published_doi in links:
        published_id = doi_to_id.get(published_doi.casefold())
        preprint = paper_by_id.get(preprint_id)
        if published_id and preprint:
            version_preprints.setdefault(published_id, []).append(preprint)
    duplicate_suppressed = []
    published_dois = {str(p.doi or p.identifiers.get("doi") or "").casefold()
                      for p in topic_papers if not _is_preprint(p)}
    for paper in topic_papers:
        if paper.paper_id not in observed:
            continue
        canonical_id = canonical_versions.get(paper.paper_id, paper.paper_id)
        if canonical_id != paper.paper_id:
            duplicate_suppressed.append({"paper_id": paper.paper_id,
                                         "canonical_paper_id": canonical_id,
                                         "title": paper.title})
            continue
        if any(preprint_id == paper.paper_id and doi.casefold() in published_dois
               for preprint_id, doi in links):
            continue
        event = _weekly_event(paper, observed[paper.paper_id]["event_type"], primary_start,
                              window_end)
        result = recommend(paper, bundle, bundle.get("attention-memory"),
                           decisions.get(card_key(topic_id, paper.paper_id, paper.doi or paper.identifiers.get("doi")))
                           or decisions.get(paper.paper_id) or decisions.get(paper_key(paper)),
                           config_dir=config_dir)
        relevance = result["attention_ranking"]["category"]
        transfer = next((item for item in result["reason"]["structured_transfers"]
                         if item["status"] == "EVIDENCE_MATCHED"), None)
        published, date_source = _weekly_publication(paper)
        recent = _recent_publication(paper, primary_start, window_end)
        linked_releases = [_weekly_publication(preprint)[0]
                           for preprint in version_preprints.get(paper.paper_id, [])]
        if linked_releases and any(value is None for value in linked_releases):
            published, date_source, recent = None, "linked_preprint_first_release_unverified", False
            if event == "NEW_PAPER":
                event = "DATE_UNVERIFIED"
        elif linked_releases:
            earliest_release = min(linked_releases)
            if published is None or earliest_release < published:
                published, date_source = earliest_release, "linked_preprint_first_release"
                recent = _date_bounds(published)[0] == _date_bounds(published)[1] and \
                    primary_start <= _date_bounds(published)[0] <= window_end
                if event == "NEW_PAPER" and not recent:
                    event = "LATE_INDEXED"
        scientific_qualified, qualification_reason = _weekly_scientific_qualification(
            paper, bundle, result)
        novel_event = event in ("NEW_PAPER", "MATERIAL_UPDATE")
        valid_source = any(record.provider_id in PROVIDERS and record.source_id.strip() and
                           str(record.normalized_metadata.get("title") or "").strip()
                           for record in paper.source_records)
        basic_qualified = bool(paper.title.strip() and valid_source and recent and novel_event and
                               qualification_reason not in {"prior_human_rejection",
                                                            "retracted_or_withdrawn"})
        fallback_scientific_link = (_weekly_fallback_scientific_link(paper, bundle, result)
                                    if basic_qualified else None)
        rows.append({"paper_id": paper.paper_id, "topic_id": topic_id,
                     "paper_key": card_key(topic_id, paper.paper_id, paper.doi or paper.identifiers.get("doi")),
                     "title": paper.title, "abstract": paper.abstract,
                     "doi": paper.doi or paper.identifiers.get("doi"),
                     "url": paper.landing_url, "date": published,
                     "publication_date": paper.publication_date,
                     "online_date": paper.online_date,
                     "date_source": date_source,
                     "search_window_start": since,
                     "search_window_end": window_end.isoformat(),
                     "date_precision": ("day" if len(published or "") >= 10
                                        else "month" if len(published or "") >= 7
                                        else "year" if published else "unverified"),
                     "indexed_date": paper.indexed_date, "updated_date": paper.updated_date,
                     "first_seen": paper.first_seen_at,
                     "delivery_date": local_day.isoformat(),
                     "priority": _weekly_priority(result), "score": result["base_score"],
                     "attention_rank": result["original_attention"]["attention_rank"],
                     "attention_rank_sort_key": result["original_attention"]["sort_key"],
                     "intrinsic_attention": result["original_attention"]["intrinsic_attention"],
                     "final_attention": result["original_attention"]["final_attention"],
                     "content_version": current_versions[paper.paper_id],
                     "fulltext_status": "NOT_VERIFIED",
                     "event": event, "relevance": relevance,
                     "eligible": bool(scientific_qualified and basic_qualified),
                     "basic_qualified": basic_qualified,
                     "fallback_scientific_link": fallback_scientific_link,
                     "scientific_qualification": qualification_reason,
                     "date_qualified": recent,
                     "novel_event_qualified": novel_event,
                     "research_question": result["reason"]["questions"][0] if result["reason"]["questions"] else
                                          result["reason"]["topic_corroboration"]["question_id"] if result["reason"].get("topic_corroboration") else None,
                     "transfer_type": transfer["transfer_type"] if transfer else
                                      ("direct_reference" if relevance == "A" else None),
                     "transfer_explanation": transfer,
                     "why_recommended": (transfer["transfer_rationale"] if transfer and
                                         transfer.get("transfer_rationale") else
                                         result["reason"]["topic_corroboration"]["rationale"] if result["reason"].get("topic_corroboration") else
                                         result["reason"]["evidence"][0].get("excerpt")
                                         if result["reason"]["evidence"] else None),
                     "attention_ranking": result["attention_ranking"]})
    rows = _rank_weekly_rows(rows)
    selected_rows = _select_weekly_rows(rows)
    high_rows = [row for row in selected_rows if row["priority"] == "HIGH"]
    selection_summary = {
        "topic_id": topic_id, "minimum_target": 1,
        "target_met": bool(selected_rows),
        "selection_order": ["HIGH", "MEDIUM", "LOW"],
        "no_cross_topic_quota": True,
        "selected_by_tier": {tier: sum(row["priority"] == tier for row in selected_rows)
                             for tier in ("HIGH", "MEDIUM", "LOW")},
        "top_recommendation_paper_id": selected_rows[0]["paper_id"] if selected_rows else None,
        "zero_reason": None if selected_rows else "no_current_week_candidate_with_topic_evidence",
    }
    # A previous attempt may have committed cards and the notification outbox
    # before delivery failed. Keep its exact HIGH revisions eligible to retry.
    prior_report_path = output_dir / "weekly-report.json"
    if prior_report_path.is_file():
        prior = json.loads(prior_report_path.read_text(encoding="utf-8"))
        if prior.get("run_id") == datetime.fromisoformat(run_at).strftime("%Y%m%dT%H%M%SZ") + "-" + topic_id:
            known = {row["paper_id"] for row in high_rows}
            high_rows.extend(row for row in prior.get("notification_recovery_high",
                                                      prior.get("recommended", []))
                             if row.get("priority") == "HIGH"
                             and row.get("paper_id") not in known
                             and current_versions.get(row.get("paper_id")) == row.get("content_version"))
    allowed = {row["paper_id"] for row in high_rows}
    allowed_versions = {row["paper_id"]: row["content_version"] for row in high_rows}
    run_id = datetime.fromisoformat(run_at).strftime("%Y%m%dT%H%M%SZ") + "-" + topic_id
    record_evidence(config_dir, run_id, rows)
    card_root = ensure_private_path(cards_dir or output_dir / "cards")
    card_paths = []
    delivery_paths = []
    for row in rows:
        if row["eligible"]:
            path = write_card(card_root, row, topic_id=topic_id,
                              topic_name=bundle["topic"]["display_name"],
                              run_id=run_id, seen_on=local_day.isoformat(),
                              attention=row["priority"], create=row["eligible"])
            if path is not None:
                delivery_paths.append(path)
            if row["eligible"] and path is not None:
                card_paths.append(str(path))
    # A shared dated archive contains older runs and other Topics. Deliver only
    # cards touched by this run; the manifest keeps their stable Vault identity.
    obsidian_delivery = (deliver_selected(delivery_paths, section="Weekly", config=binding_path)
                        if binding_path.is_file() else
                        {"status": "OBSIDIAN_NOT_CONNECTED", "new": 0,
                         "updated": 0, "failed": 0,
                         "next": "literature-radar obsidian setup"})
    delivery_ready = (obsidian_delivery["status"] == "SUCCESS" or
                      not binding_path.is_file() and
                      obsidian_delivery["status"] == "OBSIDIAN_NOT_CONNECTED")
    channels, configured = configured_notifiers(bundle)
    active_notifiers = dict(notifiers) if notifiers is not None else dict(configured)
    overrides = {row["paper_id"]: {"relevance": row["relevance"],
                                   "transfer_type": row["transfer_type"],
                                   "transfer_explanation": row["transfer_explanation"],
                                   "research_question": row["research_question"],
                                   "why_recommended": row["why_recommended"],
                                   "feedback_file": str(config_dir / "feedback.json"),
                                   "eligible": True}
                 for row in high_rows}
    notification_enabled = bundle.get("delivery", {}).get("notification", {}).get("enabled", False)
    notification_channels = []
    if delivery_ready and binding_path.is_file():
        binding = _read_binding(config=binding_path)
        if binding is not None:
            notification_channels.append("obsidian")
            if "obsidian" not in active_notifiers:
                active_notifiers["obsidian"] = ObsidianNotifier(Path(binding["vault_root"]))
    # Local generated cards remain a valid delivery target when no Vault is
    # bound. A bound Vault failure, however, must suppress desktop success.
    if notification_enabled and (delivery_ready or not binding_path.is_file()):
        notification_channels.append("desktop")
    if "desktop" in notification_channels and "desktop" not in active_notifiers:
        active_notifiers = {**active_notifiers, "desktop": DesktopNotifier()}
    queued = (0 if notification_preview or not delivery_ready else
              store.queue_pending(topic_id, notification_channels, allowed_paper_ids=allowed,
                                  payload_overrides=overrides, once_per_topic_paper=True))
    suppressed_prior = store.count_unselected_notifications(topic_id, tuple(notification_channels), allowed_versions)
    delivered = {"sent": 0, "failed": 0} if notification_preview else dispatch_outbox(
                                store, {k:v for k,v in active_notifiers.items() if k in notification_channels},
                                topic_id=topic_id, allowed_paper_ids=allowed,
                                allowed_versions=allowed_versions,
                                limit=max(100, len(high_rows)*2)) if notification_channels else {"sent": 0, "failed": 0}
    unresolved_notifications = (0 if notification_preview else
                                store.count_unresolved_notifications(topic_id,
                                    tuple(notification_channels), allowed_versions))
    current_window = {"start": since, "end": window_end.isoformat(), "run_at": run_at}
    pending_keys = {(item["provider_id"], item["start"], item["end"])
                    for item in remaining_pending}
    for name in selected:
        if _provider_window_covered(summary["providers"][name]):
            continue
        key = (name, since, window_end.isoformat())
        if key in pending_keys:
            continue
        prior = current_pending.get(key)
        try:
            signatures = _provider_signatures(name, bundle, current_window, providers,
                                              config_dir=config_dir)
            if prior and not set(prior.get("query_signatures", [])) <= set(signatures):
                raise ValueError("original query signatures changed")
        except Exception as exc:
            # No query was safely recorded. Keep the original active window
            # rather than inventing a recovery signature.
            summary["providers"][name]["signature_error"] = type(exc).__name__
            signatures = prior.get("query_signatures") if prior else None
        if signatures is not None:
            entry = prior or {"provider_id": name, "start": since,
                              "end": window_end.isoformat(),
                              "query_signatures": signatures}
            remaining_pending.append(unfinished(entry, summary["providers"][name]["status"],
                                                summary["providers"][name]))
            pending_keys.add(key)
    # Review conflicts do not shorten source coverage. Failed or incomplete
    # sources retain exact windows while successful sources can advance.
    provider_results = list(summary["providers"].values())
    provider_states = [item["status"] for item in provider_results]
    current_complete = bool(provider_results) and all(
        _provider_window_covered(item) for item in provider_results)
    complete = current_complete and not remaining_pending
    catchup_required = (
                        datetime.fromisoformat(run_at).astimezone(ZoneInfo("America/New_York")).date().isocalendar()[:2]
                        < local_day.isocalendar()[:2])
    any_success = any(status in ("SUCCESS", "PARTIAL_SUCCESS") for status in provider_states)
    scan_status = ("CATCHUP_REQUIRED" if catchup_required and complete else
                   "PARTIAL_SUCCESS_CATCHUP_REQUIRED" if catchup_required and any_success else
                   "SUCCESS_WITH_REVIEW_CONFLICTS" if complete and
                   any(status == "PARTIAL_SUCCESS" for status in provider_states)
                   else "SUCCESS" if complete else "PARTIAL_SUCCESS" if any_success else "INCOMPLETE")
    if binding_path.is_file() and obsidian_delivery["status"] in ("FAILED", "PARTIAL"):
        scan_status = "DELIVERY_FAILED"
    elif delivered["failed"] or unresolved_notifications:
        scan_status = "NOTIFICATION_FAILED"
    elif any(item.get("status") == "FAILED" for item in
             (feedback_sync, legacy_feedback, attention_training, memory_training)):
        scan_status = "PARTIAL_SUCCESS"
    report = {"topic_id": topic_id, "status": scan_status,
              "scheduled_at": scheduled.isoformat() if scheduled else None,
              "actual_run_at": instant.isoformat(),
              "primary_window": {"start": primary_start.isoformat(), "end": window_end.isoformat()},
              "overlap_window": {"start": since, "end": window_end.isoformat()},
              "topic": bundle["topic"]["display_name"], "focus": bundle["focus"],
              "run_id": run_id, "providers": summary["providers"],
              "provider_recovery": recovery,
              "pending_provider_windows": remaining_pending,
              "coverage_complete": complete,
              "candidate_count": max(summary["papers_seen"], len(observed)),
              "resumed_pending_event_count": resumed_pending,
              "assessed_count": len(rows),
              "qualified_count": sum(r["relevance"] in ("A", "B", "C") for r in rows),
              "recommended_count": len(selected_rows), "events": rows,
              "selection_summary": selection_summary,
              "duplicate_suppressed_count": len(duplicate_suppressed),
              "duplicate_suppressed": duplicate_suppressed,
              "recommended": selected_rows, "monitor": [r for r in rows if not r["eligible"]],
              "notification_recovery_high": high_rows,
              "cards": card_paths, "alerted": len(selected_rows),
              "notification": {"queued": queued, **delivered,
                               "preview": notification_preview,
                               "unresolved": unresolved_notifications,
                               "eligible_high_count": len(high_rows),
                               "suppressed_prior": suppressed_prior,
                               "suppression_reason": "not_in_current_eligible_high_set" if suppressed_prior else None},
              "catchup_required": catchup_required,
              "feedback_sync": feedback_sync, "legacy_feedback": legacy_feedback,
              "attention_training": attention_training,
              "memory_training": memory_training,
              "review_conflicts": summary["paper_event_counts"].get("REVIEW_CONFLICT", 0)}
    report["obsidian_delivery"] = obsidian_delivery
    focus_bytes = json.dumps(bundle["focus"], sort_keys=True,
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    papers_by_id = {paper.paper_id: paper for paper in topic_papers}
    manifest = {"schema_version": 1, "run_id": run_id, "topic_id": topic_id,
                "focus_version": bundle["focus"]["version"],
                "focus_sha256": hashlib.sha256(focus_bytes).hexdigest(),
                "scheduled_at": report["scheduled_at"],
                "actual_run_at": report["actual_run_at"],
                "search_window": report["overlap_window"],
                "primary_window": report["primary_window"],
                "provider_status": {name: item["status"] for name, item in summary["providers"].items()},
                "query_signatures": {},
                "recommendations": [{"paper_id": row["paper_id"],
                                     "paper_key": row["paper_key"],
                                     "attention": row["priority"],
                                     "relevance": row["relevance"],
                                     "why_recommended": row["why_recommended"],
                                     "source_records": [{"provider_id": source.provider_id,
                                                         "source_id": source.source_id}
                                                        for source in papers_by_id[row["paper_id"]].source_records]}
                                    for row in selected_rows],
                "delivery_status": obsidian_delivery["status"],
                "notification": report["notification"],
                "status": scan_status}
    for name in selected:
        try:
            manifest["query_signatures"][name] = _provider_signatures(
                name, bundle, {"start": since, "end": window_end.isoformat()}, providers,
                config_dir=config_dir)
        except Exception as exc:
            manifest["query_signatures"][name] = {"error": type(exc).__name__}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n").encode("utf-8")
    digest = hashlib.sha256(manifest_bytes).hexdigest()[:16]
    manifest_path = output_dir / "runs" / run_id / f"manifest-{digest}.json"
    if manifest_path.is_file():
        if manifest_path.read_bytes() != manifest_bytes:
            raise ValueError("Weekly manifest digest collision")
    else:
        _write_atomic(manifest_path, manifest_bytes.decode("utf-8"))
    report["manifest"] = str(manifest_path)
    _write_atomic(output_dir / "weekly-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [f"# Weekly：{bundle['topic']['display_name']}", "",
             f"Topic：{topic_id}", f"Run ID：{run_id}",
             f"主窗口：{primary_start.isoformat()} 至 {window_end.isoformat()}",
             f"14 天回查：{since} 至 {window_end.isoformat()}",
             f"Current Focus：{', '.join(bundle['focus'].get('search_terms', []))}",
             f"检索候选：{summary['papers_seen']}；达到门槛：{report['qualified_count']}；最终推荐：{len(selected_rows)}", "",
             "## 本周推荐", ""]
    for row in selected_rows:
        lines.extend([f"### {row['title']}", "",
                      f"- 分数 / Attention：{row['score']} / {row['priority']}",
                      f"- 发表时间 / 精度 / 事件：{row['date'] or '未核实'} / {row['date_precision']} / {row['event']}",
                      f"- 为什么推荐：{row['why_recommended'] or '待核对'}",
                      f"- 研究问题：{row['research_question'] or '待核对'}",
                      f"- 摘要或方法证据：{row['abstract'] or '未提供'}",
                      f"- DOI / 原文：{row['doi'] or '未提供'} / {row['url'] or '未提供'}",
                      f"- Run ID：{run_id}", ""])
    lines.extend(["## Monitor", ""])
    for row in report["monitor"]:
        lines.append(f"- {row['title']} — {row['event']} / {row['relevance']}")
    _write_atomic(output_dir / "weekly-report.md", "\n".join(lines) + "\n")
    if scan_status not in ("DELIVERY_FAILED", "NOTIFICATION_FAILED") and all(
            "signature_error" not in item for item in summary["providers"].values()):
        advance(config_dir, "WEEKLY_MONITORING", last_weekly_run_at=run_at,
                last_weekly_window=current_window,
                pending_provider_windows=remaining_pending,
                active_weekly_window=None)
    return report
