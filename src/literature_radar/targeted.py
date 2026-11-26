"""Bounded, publication-window search plans from a private query strategy.

The strategy file is supplied at runtime; this module contains no research terms.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import _malformed_reference
from literature_radar.providers.models import DateWindow, QueryPlan
from literature_radar.sources.factory import make_provider
from literature_radar.sources.http import RequestFailure

SEARCH_PROVIDERS = ("openalex", "europepmc", "pubmed")
COUNT_FIELDS = ("provider", "query_id", "signature", "group", "window_start", "window_end",
                "expression", "hit_count", "status", "reason")


def annual_windows(start: str, end: str) -> list[DateWindow]:
    left, right = date.fromisoformat(start), date.fromisoformat(end)
    if left > right:
        raise ValueError("invalid strategy window")
    windows = []
    while left <= right:
        try:
            anniversary = left.replace(year=left.year + 1)
        except ValueError:
            anniversary = left.replace(year=left.year + 1, day=28)
        if anniversary >= right:
            windows.append(DateWindow(left.isoformat(), right.isoformat(), "publication"))
            break
        next_start = anniversary
        windows.append(DateWindow(left.isoformat(), (next_start - timedelta(days=1)).isoformat(), "publication"))
        left = next_start
    return windows


def validate_strategy(value: dict) -> None:
    if not isinstance(value, dict) or not value.get("id") or not value.get("topic_id"):
        raise ValueError("strategy requires id and topic_id")
    annual_windows(value["window_start"], value["window_end"])
    groups = value.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("strategy requires groups")
    ids = set()
    for group in groups:
        ident, clauses = group.get("id"), group.get("clauses")
        if not ident or ident in ids or not isinstance(clauses, list) or len(clauses) < 2:
            raise ValueError("groups require unique IDs and at least two AND clauses")
        ids.add(ident)
        for clause in clauses:
            if not isinstance(clause, list) or not 1 <= len(clause) <= 8 or any(
                    not isinstance(term, str) or not term.strip() or len(term) > 100 or '"' in term
                    for term in clause):
                raise ValueError("invalid strategy clause")


def render_expression(group: dict, provider: str) -> str:
    if provider not in SEARCH_PROVIDERS:
        raise ValueError("unsupported targeted search provider")
    clauses = []
    for clause in group["clauses"]:
        if provider == "openalex":
            terms = [f'"{term}"' for term in clause]
        elif provider == "europepmc":
            terms = [f'TITLE_ABS:"{term}"' for term in clause]
        else:
            terms = [f'"{term}"[tiab]' for term in clause]
        clauses.append("(" + " OR ".join(terms) + ")")
    return " AND ".join(clauses)


def query_plans(strategy: dict, provider: str, *, page_size: int = 100) -> list[QueryPlan]:
    validate_strategy(strategy)
    plans = []
    for group in strategy["groups"]:
        expression = render_expression(group, provider)
        for window in annual_windows(strategy["window_start"], strategy["window_end"]):
            query_id = f"{strategy['topic_id']}:{strategy['id']}:{provider}:{group['id']}:{window.start}"
            if provider == "openalex":
                parameters = {"search": expression, "search_field": "search.title_and_abstract",
                              "page_size": min(page_size, 100), "sync_mode": "publication",
                              "paging": "cursor" if os.getenv("OPENALEX_API_KEY") else "page"}
            elif provider == "europepmc":
                parameters = {"query": expression, "page_size": min(page_size, 1000),
                              "result_type": "core", "date_field": "FIRST_PDATE"}
            else:
                parameters = {"term": expression, "page_size": min(page_size, 200),
                              "date_field": "pdat"}
            plans.append(QueryPlan(provider, query_id, parameters, window))
    return plans


def _count(provider, plan: QueryPlan) -> int:
    window = plan.date_window
    if plan.provider_id == "openalex":
        params = {"search.title_and_abstract": plan.query_parameters["search"], "per_page": 1,
                  "filter": f"from_publication_date:{window.start},to_publication_date:{window.end}"}
        key = os.getenv("OPENALEX_API_KEY")
        data = provider.client.get_json(provider.endpoint, params,
                                        {"Authorization": f"Bearer {key}"} if key else {})
        count = (data.get("meta") or {}).get("count") if isinstance(data, dict) else None
    elif plan.provider_id == "europepmc":
        query = f"({plan.query_parameters['query']}) AND FIRST_PDATE:[{window.start} TO {window.end}]"
        data = provider.client.get_json(provider.endpoint, {"query": query, "format": "json",
                                        "resultType": "lite", "pageSize": 1})
        count = data.get("hitCount") if isinstance(data, dict) else None
    else:
        params = {**provider._common_params(), "db": "pubmed", "term": plan.query_parameters["term"],
                  "retmode": "json", "retmax": 0, "datetype": "pdat",
                  "mindate": window.start.replace("-", "/"),
                  "maxdate": window.end.replace("-", "/")}
        data = provider.client.get_json(provider.base + "esearch.fcgi", params)
        count = (data.get("esearchresult") or {}).get("count") if isinstance(data, dict) else None
    try:
        result = int(count)
    except (TypeError, ValueError) as exc:
        raise RequestFailure(plan.provider_id, "malformed_count") from exc
    if result < 0:
        raise RequestFailure(plan.provider_id, "malformed_count")
    return result


def count_queries(strategy: dict, output: Path, providers: tuple[str, ...] = SEARCH_PROVIDERS,
                  *, page_size: int = 100, max_hits: int = 20_000) -> list[dict]:
    rows = []
    if output.exists():
        with output.open(newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    previous = {row["signature"]: row for row in rows}
    for name in providers:
        provider = make_provider(name, page_size=page_size)
        if name == "openalex":
            # A single count probe must not silently retry HTTP 429 inside
            # the shared HTTP client before the plan-level stop guard runs.
            provider.client.max_retries = 0
        deferred = False
        for plan in query_plans(strategy, name, page_size=page_size):
            group = plan.query_id.rsplit(":", 2)[-2]
            row = dict(provider=name, query_id=plan.query_id, signature=plan.query_signature,
                       group=group, window_start=plan.date_window.start,
                       window_end=plan.date_window.end,
                       expression=plan.query_parameters.get("search") or
                                  plan.query_parameters.get("query") or plan.query_parameters["term"],
                       hit_count="", status="COUNT_FAILED", reason="")
            if plan.query_signature in previous and previous[plan.query_signature]["status"] in ("READY", "TOO_BROAD"):
                continue
            if deferred:
                row["status"] = "NOT_ATTEMPTED"
                row["reason"] = "provider_rate_limited"
                if plan.query_signature in previous:
                    rows = [existing for existing in rows if existing["signature"] != plan.query_signature]
                rows.append(row)
                previous[plan.query_signature] = row
                _write_counts(output, rows)
                continue
            try:
                count = _count(provider, plan)
                row["hit_count"] = count
                limit = min(max_hits, 10_000) if name in ("openalex", "pubmed") and not (
                    name == "openalex" and os.getenv("OPENALEX_API_KEY")) else max_hits
                row["status"] = "TOO_BROAD" if count > limit else "READY"
                row["reason"] = "count_exceeds_safe_limit" if count > limit else ""
            except Exception as exc:
                row["reason"] = exc.category if isinstance(exc, RequestFailure) else type(exc).__name__
                if isinstance(exc, RequestFailure) and exc.status == 429:
                    deferred = True
                    delay = provider.client._retry_after(provider.client.last_headers.get("retry-after"))
                    if delay is not None:
                        row["reason"] += ":retry_after_until=" + datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + delay, timezone.utc).isoformat()
            if plan.query_signature in previous:
                rows = [existing for existing in rows if existing["signature"] != plan.query_signature]
            rows.append(row)
            previous[plan.query_signature] = row
            _write_counts(output, rows)
    return rows


def _write_counts(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COUNT_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def fetch_counted(strategy: dict, database: Path, count_path: Path, provider_name: str,
                  *, page_size: int = 100, max_pages: int = 200, max_seconds: int = 600) -> dict:
    if provider_name not in SEARCH_PROVIDERS:
        raise ValueError("unsupported targeted search provider")
    with count_path.open(newline="") as handle:
        counts = {row["signature"]: row for row in csv.DictReader(handle, delimiter="\t")}
    store = AcademicStore(database, cache_abstracts=True)
    provider = make_provider(provider_name, page_size=page_size)
    if provider_name == "openalex":
        provider.client.max_retries = 0
    deadline = time.monotonic() + max_seconds
    outcome = {"provider": provider_name, "plans": [], "pages": 0, "records": 0}
    rate_limited = False
    for plan in query_plans(strategy, provider_name, page_size=page_size):
        status = counts.get(plan.query_signature, {}).get("status")
        item = {"query_id": plan.query_id, "signature": plan.query_signature, "status": status or "NO_COUNT",
                "pages": 0, "records": 0, "malformed": 0, "conflicts": 0}
        outcome["plans"].append(item)
        if rate_limited:
            item["status"] = "NOT_ATTEMPTED_RATE_LIMIT"
            continue
        if status != "READY":
            continue
        checkpoint = store.checkpoint(provider_name, strategy["topic_id"], plan.query_signature)
        if checkpoint and checkpoint["status"] == "SUCCESS":
            item["status"] = "SUCCESS"
            continue
        cursor = json.loads(checkpoint["cursor_json"]) if checkpoint else None
        page_number = checkpoint["page_number"] if checkpoint else 0
        for _ in range(max_pages):
            if time.monotonic() >= deadline:
                item["status"] = "INCOMPLETE_TIME_BUDGET"
                break
            try:
                page = provider.fetch_page(plan, cursor)
                records, malformed = [], []
                for raw in page.records:
                    try:
                        records.append(provider.normalize(raw))
                    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RequestFailure) as exc:
                        malformed.append(_malformed_reference(raw, exc))
                _, conflicts = store.commit_page(strategy["topic_id"], plan, page, records,
                                                 malformed_records=malformed,
                                                 expected_page_number=page_number,
                                                 expected_cursor=cursor)
            except Exception as exc:
                item["status"] = "FAILED"
                item["reason"] = exc.category if isinstance(exc, RequestFailure) else type(exc).__name__
                if isinstance(exc, RequestFailure) and exc.status == 429:
                    rate_limited = True
                    delay = provider.client._retry_after(provider.client.last_headers.get("retry-after"))
                    if delay is not None:
                        item["retry_after_until"] = datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + delay, timezone.utc).isoformat()
                store.mark_failed(strategy["topic_id"], plan, expected_page_number=page_number)
                break
            item["pages"] += 1
            item["records"] += len(page.records)
            item["malformed"] += len(malformed)
            item["conflicts"] += conflicts
            outcome["pages"] += 1
            outcome["records"] += len(page.records)
            page_number += 1
            cursor = page.next_cursor
            if not page.has_more:
                item["status"] = "SUCCESS"
                break
        else:
            item["status"] = "INCOMPLETE_PAGE_BUDGET"
        if time.monotonic() >= deadline:
            break
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("count", "fetch"))
    parser.add_argument("--strategy", type=Path, required=True)
    parser.add_argument("--counts", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--provider", choices=SEARCH_PROVIDERS)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args()
    strategy = json.loads(args.strategy.read_text())
    validate_strategy(strategy)
    if args.mode == "count":
        count_queries(strategy, args.counts, (args.provider,) if args.provider else SEARCH_PROVIDERS)
    else:
        if not args.database or not args.provider:
            parser.error("fetch requires --database and --provider")
        print(json.dumps(fetch_counted(strategy, args.database, args.counts, args.provider,
                                       max_pages=args.max_pages, max_seconds=args.max_seconds)))


if __name__ == "__main__":
    main()
