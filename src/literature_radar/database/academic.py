"""Versioned SQLite store: page writes and checkpoint progress share one transaction."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from literature_radar.candidates.identity import IdentityConflict, identifiers, merge, normalize_doi
from literature_radar.discovery.events import (DUPLICATE_SOURCE, MATERIAL_UPDATE,
                                              NEW_PAPER, REVIEW_CONFLICT, UNCHANGED,
                                              content_version)
from literature_radar.models import Paper, SourceRecord
from literature_radar.providers.models import PageResult, QueryPlan
from literature_radar.sources.common import utcnow


SCHEMA_VERSION = 2  # Keep the public v2 database contract for existing callers.
_UNSET = object()


class CheckpointConflict(RuntimeError):
    """A different runner advanced the same query before this page committed."""


DDL = (
    "CREATE TABLE IF NOT EXISTS papers_v2 (paper_id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS identifiers (kind TEXT NOT NULL, value TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), PRIMARY KEY(kind,value))",
    "CREATE TABLE IF NOT EXISTS source_records (provider_id TEXT NOT NULL, source_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), payload TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(provider_id,source_id))",
    "CREATE TABLE IF NOT EXISTS field_provenance (paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), field_name TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(paper_id,field_name))",
    "CREATE TABLE IF NOT EXISTS paper_relations (paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), relation_type TEXT NOT NULL, target_kind TEXT NOT NULL, target_value TEXT NOT NULL, PRIMARY KEY(paper_id,relation_type,target_kind,target_value))",
    "CREATE TABLE IF NOT EXISTS citation_edges (topic_id TEXT NOT NULL, seed_paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), related_paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), relation TEXT NOT NULL CHECK(relation IN ('references','citations')), provider_id TEXT NOT NULL, query_signature TEXT NOT NULL, discovered_at TEXT NOT NULL, PRIMARY KEY(topic_id,seed_paper_id,related_paper_id,relation,provider_id,query_signature))",
    "CREATE TABLE IF NOT EXISTS provider_checkpoints (provider_id TEXT NOT NULL, topic_id TEXT NOT NULL, query_id TEXT NOT NULL, query_signature TEXT NOT NULL, window_start TEXT, window_end TEXT, cursor_json TEXT, page_number INTEGER NOT NULL, records_fetched INTEGER NOT NULL, status TEXT NOT NULL, last_success_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(provider_id,topic_id,query_signature))",
    "CREATE TABLE IF NOT EXISTS record_quarantine (provider_id TEXT NOT NULL, topic_id TEXT NOT NULL, query_signature TEXT NOT NULL, payload_sha256 TEXT NOT NULL, identifier TEXT, category TEXT NOT NULL, page_number INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(provider_id,topic_id,query_signature,payload_sha256))",
    "CREATE TABLE IF NOT EXISTS sync_runs (run_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, summary_json TEXT)",
    "CREATE TABLE IF NOT EXISTS legacy_paper_mapping (topic_id TEXT NOT NULL, paper_key TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), PRIMARY KEY(topic_id,paper_key))",
    "CREATE TABLE IF NOT EXISTS identity_conflicts (provider_id TEXT NOT NULL, source_id TEXT NOT NULL, reason TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY(provider_id,source_id))",
    "CREATE TABLE IF NOT EXISTS topic_paper_state (topic_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), content_version TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY(topic_id,paper_id))",
    "CREATE TABLE IF NOT EXISTS specialty_topic_papers (topic_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY(topic_id,paper_id))",
    "CREATE TABLE IF NOT EXISTS research_evaluations (topic_id TEXT NOT NULL, run_id TEXT NOT NULL, focus_version TEXT NOT NULL, mode TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), payload TEXT NOT NULL, overall_score REAL NOT NULL, selected INTEGER NOT NULL CHECK(selected IN (0,1)), created_at TEXT NOT NULL, PRIMARY KEY(topic_id,run_id,paper_id))",
    "CREATE TABLE IF NOT EXISTS research_evaluation_revisions (topic_id TEXT NOT NULL, source_run_id TEXT NOT NULL, reevaluation_run_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), old_score REAL NOT NULL, new_score REAL NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(topic_id,source_run_id,reevaluation_run_id,paper_id))",
    "CREATE TABLE IF NOT EXISTS paper_events (topic_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), content_version TEXT NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('NEW_PAPER','MATERIAL_UPDATE')), recommendation_json TEXT, created_at TEXT NOT NULL, processed_at TEXT, PRIMARY KEY(topic_id,paper_id,content_version,event_type))",
    "CREATE TABLE IF NOT EXISTS notification_outbox (idempotency_key TEXT PRIMARY KEY, topic_id TEXT NOT NULL, paper_id TEXT NOT NULL, event_type TEXT NOT NULL, content_version TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT','FAILED')), attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), created_at TEXT NOT NULL, sent_at TEXT, updated_at TEXT NOT NULL, last_error TEXT, payload_json TEXT NOT NULL, UNIQUE(topic_id,paper_id,content_version,channel))",
    "CREATE TABLE IF NOT EXISTS schedule_state (topic_id TEXT NOT NULL, provider_id TEXT NOT NULL, next_due_at TEXT NOT NULL, last_attempt_at TEXT, lease_until TEXT, last_status TEXT, PRIMARY KEY(topic_id,provider_id))",
    "CREATE TABLE IF NOT EXISTS schema_features (feature TEXT PRIMARY KEY, version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS paper_field_hashes (paper_id TEXT NOT NULL REFERENCES papers_v2(paper_id), field_name TEXT NOT NULL, value_hash TEXT NOT NULL, PRIMARY KEY(paper_id,field_name))",
    "CREATE INDEX IF NOT EXISTS idx_source_paper ON source_records(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_checkpoint_query ON provider_checkpoints(provider_id,topic_id,query_id,updated_at)",
    "CREATE TABLE IF NOT EXISTS source_query_provenance (provider_id TEXT NOT NULL, source_id TEXT NOT NULL, topic_id TEXT NOT NULL, query_signature TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY(provider_id,source_id,topic_id,query_signature), FOREIGN KEY(provider_id,source_id) REFERENCES source_records(provider_id,source_id))",
    "CREATE INDEX IF NOT EXISTS idx_source_query_provenance_query ON source_query_provenance(topic_id,query_signature)",
    "CREATE INDEX IF NOT EXISTS idx_events_pending ON paper_events(topic_id,processed_at)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox(status,updated_at)",
)


class AcademicStore:
    def __init__(self, path: Path, *, cache_abstracts: bool = False):
        self.path = Path(path)
        self.cache_abstracts = cache_abstracts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("database schema is newer than this program")
            with db:
                db.execute("BEGIN IMMEDIATE")
                for statement in DDL: db.execute(statement)
                db.execute("INSERT OR IGNORE INTO schema_features VALUES ('production_events',3)")
                if version < SCHEMA_VERSION:
                    legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='papers'").fetchone()
                    if legacy:
                        for topic_id, paper_key, payload in db.execute("SELECT topic_id,paper_key,payload FROM papers"):
                            paper = Paper.from_dict(json.loads(payload))
                            doi = normalize_doi(paper.identifiers.get("doi"))
                            row = db.execute("SELECT paper_id FROM identifiers WHERE kind='doi' AND value=?", (doi,)).fetchone() if doi else None
                            paper_id = row[0] if row else uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{topic_id}:{paper_key}").hex
                            now = utcnow()
                            db.execute("INSERT OR IGNORE INTO papers_v2 VALUES (?,?,?,?)",
                                       (paper_id, json.dumps(replace(paper, paper_id=paper_id).as_dict()),
                                        now, now))
                            if doi:
                                db.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?)", ("doi", doi, paper_id))
                            db.execute("INSERT OR IGNORE INTO legacy_paper_mapping VALUES (?,?,?)",
                                       (topic_id, paper_key, paper_id))
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def checkpoint(self, provider_id: str, topic_id: str, signature: str):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM provider_checkpoints WHERE provider_id=? AND topic_id=? AND query_signature=?",
                             (provider_id, topic_id, signature)).fetchone()
            return dict(row) if row else None

    def latest_checkpoint(self, provider_id: str, topic_id: str, query_id: str):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM provider_checkpoints WHERE provider_id=? AND topic_id=? AND query_id=? ORDER BY CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END, updated_at DESC LIMIT 1",
                             (provider_id, topic_id, query_id)).fetchone()
            return dict(row) if row else None

    def latest_provider_success(self, provider_id: str, topic_id: str):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM provider_checkpoints WHERE provider_id=? AND topic_id=? AND status='SUCCESS' ORDER BY updated_at DESC LIMIT 1",
                             (provider_id, topic_id)).fetchone()
            return dict(row) if row else None

    def import_selected_papers(self, source_database: Path, topic_id: str,
                               paper_ids: list[str], *, priorities: Mapping[str, list[str]] | None = None) -> dict[str, str]:
        """Copy only selected Bootstrap papers from an ephemeral discovery DB.

        Identity and source records are merged through the normal upsert path.
        Discovery checkpoints and unselected candidates are never copied.
        """
        source_database = Path(source_database)
        if source_database.resolve() == self.path.resolve():
            raise ValueError("Bootstrap working database must be separate")
        if len(set(paper_ids)) != len(paper_ids):
            raise ValueError("Selected Bootstrap paper IDs are duplicated")
        mapping = {}
        with closing(sqlite3.connect(source_database)) as source, closing(self._connect()) as target:
            with target:
                target.execute("BEGIN IMMEDIATE")
                for original_id in paper_ids:
                    row = source.execute("SELECT payload FROM papers_v2 WHERE paper_id=?",
                                         (original_id,)).fetchone()
                    if row is None:
                        raise ValueError("Selected Bootstrap paper is absent from working database")
                    paper = Paper.from_dict(json.loads(row[0]))
                    if not paper.source_records:
                        raise ValueError("Selected Bootstrap paper has no source records")
                    imported = {self._upsert_record(target, record, priorities or {})[0].paper_id
                                for record in paper.source_records}
                    if len(imported) != 1:
                        raise ValueError("Selected Bootstrap sources resolved to multiple identities")
                    selected_id = imported.pop()
                    mapping[original_id] = selected_id
                    state = source.execute(
                        "SELECT content_version,first_seen_at,last_seen_at FROM topic_paper_state "
                        "WHERE topic_id=? AND paper_id=?", (topic_id, original_id)).fetchone()
                    if state is None:
                        raise ValueError("Selected Bootstrap paper lacks Topic state")
                    target.execute("INSERT INTO topic_paper_state VALUES (?,?,?,?,?) "
                                   "ON CONFLICT(topic_id,paper_id) DO UPDATE SET "
                                   "content_version=excluded.content_version,last_seen_at=excluded.last_seen_at",
                                   (topic_id, selected_id, *state))
        return mapping

    def _upsert_record(self, db, record: SourceRecord,
                       priorities: Mapping[str, list[str]]) -> Paper:
        source_match = db.execute("SELECT paper_id,fingerprint FROM source_records WHERE provider_id=? AND source_id=?",
                                  (record.provider_id, record.source_id)).fetchone()
        found = {row[0] for kind, value in identifiers(record).items()
                 for row in db.execute("SELECT paper_id FROM identifiers WHERE kind=? AND value=?", (kind, value))}
        if source_match: found.add(source_match[0])
        if len(found) > 1:
            raise IdentityConflict("identifiers point to different papers")
        paper_id = next(iter(found), uuid.uuid4().hex)
        row = db.execute("SELECT payload,first_seen_at FROM papers_v2 WHERE paper_id=?", (paper_id,)).fetchone()
        existing = Paper.from_dict(json.loads(row[0])) if row else None
        paper = merge(existing, record, paper_id, priorities)
        old_hash_row = db.execute("SELECT value_hash FROM paper_field_hashes WHERE paper_id=? AND field_name='abstract'",
                                  (paper_id,)).fetchone()
        abstract_hash = (hashlib.sha256(paper.abstract.encode()).hexdigest()
                         if paper.abstract is not None else old_hash_row[0] if old_hash_row else None)
        now = utcnow()
        paper = replace(paper, first_seen_at=row[1] if row else now, last_seen_at=now)
        persisted = paper
        stored_record = record
        if not self.cache_abstracts:
            persisted = replace(paper, abstract=None)
            metadata = dict(record.normalized_metadata)
            metadata["abstract"] = None
            stored_record = replace(record, normalized_metadata=metadata)
            persisted = replace(persisted, source_records=tuple(
                replace(r, normalized_metadata={**r.normalized_metadata, "abstract": None})
                for r in persisted.source_records))
        db.execute("INSERT INTO papers_v2 VALUES (?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET payload=excluded.payload,last_seen_at=excluded.last_seen_at",
                   (paper_id, json.dumps(persisted.as_dict(), ensure_ascii=False), paper.first_seen_at, now))
        if abstract_hash is not None:
            db.execute("INSERT INTO paper_field_hashes VALUES (?,'abstract',?) ON CONFLICT(paper_id,field_name) DO UPDATE SET value_hash=excluded.value_hash",
                       (paper_id, abstract_hash))
        for kind, value in identifiers(record).items():
            db.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?)", (kind, value, paper_id))
        fingerprint_fields = {key: record.normalized_metadata.get(key) for key in
                              ("title", "abstract", "authors", "journal", "publication_date",
                               "online_date", "document_type", "version")}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        event = "new" if source_match is None else "unchanged" if source_match[1] == fingerprint else "updated"
        db.execute("INSERT INTO source_records VALUES (?,?,?,?,?) ON CONFLICT(provider_id,source_id) DO UPDATE SET payload=excluded.payload,fingerprint=excluded.fingerprint",
                   (record.provider_id, record.source_id, paper_id,
                    json.dumps(stored_record.as_dict(), ensure_ascii=False), fingerprint))
        if not self.cache_abstracts:
            for prior_record in persisted.source_records:
                db.execute("UPDATE source_records SET payload=? WHERE provider_id=? AND source_id=?",
                           (json.dumps(prior_record.as_dict(), ensure_ascii=False),
                            prior_record.provider_id, prior_record.source_id))
        for field, provenance in paper.provenance.items():
            db.execute("INSERT INTO field_provenance VALUES (?,?,?) ON CONFLICT(paper_id,field_name) DO UPDATE SET payload=excluded.payload",
                       (paper_id, field, json.dumps(provenance.as_dict())))
        published_doi = normalize_doi(record.normalized_metadata.get("published_doi"))
        own_doi = identifiers(record).get("doi")
        if published_doi and published_doi != own_doi:
            db.execute("INSERT OR IGNORE INTO paper_relations VALUES (?,?,?,?)",
                       (paper_id, "published_as", "doi", published_doi))
            target = db.execute("SELECT paper_id FROM identifiers WHERE kind='doi' AND value=?",
                                (published_doi,)).fetchone()
            if target and target[0] != paper_id and own_doi:
                db.execute("INSERT OR IGNORE INTO paper_relations VALUES (?,?,?,?)",
                           (target[0], "has_preprint", "doi", own_doi))
        if own_doi:
            for (preprint_id,) in db.execute(
                "SELECT paper_id FROM paper_relations WHERE relation_type='published_as' AND target_kind='doi' AND target_value=?",
                (own_doi,)):
                preprint = db.execute("SELECT value FROM identifiers WHERE kind='doi' AND paper_id=? LIMIT 1",
                                      (preprint_id,)).fetchone()
                if preprint and preprint_id != paper_id:
                    db.execute("INSERT OR IGNORE INTO paper_relations VALUES (?,?,?,?)",
                               (paper_id, "has_preprint", "doi", preprint[0]))
        return paper, event, abstract_hash

    def commit_page(self, topic_id: str, plan: QueryPlan, page: PageResult,
                    records: list[SourceRecord], *, priorities: Mapping[str, list[str]] | None = None,
                    event_counts: dict[str, int] | None = None,
                    malformed_records: list[dict[str, str]] | None = None,
                    expected_page_number: int | None = None,
                    expected_cursor: object = _UNSET,
                    paper_events: list[dict] | None = None,
                    evaluator: Callable[[Paper, str, str], dict] | None = None,
                    citation_edge: tuple[str, str] | None = None,
                    queue_events: bool = True):
        papers = []
        conflicts = 0
        events = {"new": 0, "updated": 0, "unchanged": 0}
        page_events = []
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT page_number,records_fetched,last_success_at,cursor_json FROM provider_checkpoints WHERE provider_id=? AND topic_id=? AND query_signature=?",
                                   (plan.provider_id, topic_id, plan.query_signature)).fetchone()
                if expected_page_number is not None and (prior[0] if prior else 0) != expected_page_number:
                    raise CheckpointConflict("checkpoint page changed during fetch")
                if expected_cursor is not _UNSET:
                    current_cursor = json.loads(prior[3]) if prior and prior[3] is not None else None
                    if current_cursor != expected_cursor:
                        raise CheckpointConflict("checkpoint cursor changed during fetch")
                for record in records:
                    db.execute("SAVEPOINT record")
                    try:
                        paper, event, abstract_hash = self._upsert_record(db, record, priorities or {})
                        version = content_version(paper, abstract_hash=abstract_hash)
                        now = utcnow()
                        if queue_events:
                            state = db.execute("SELECT content_version,first_seen_at FROM topic_paper_state WHERE topic_id=? AND paper_id=?",
                                               (topic_id, paper.paper_id)).fetchone()
                            if state and state[0] != version and paper.document_type is not None:
                                # Filling document_type alone cannot become a
                                # scientific content update.
                                without_type = content_version(
                                    replace(paper, document_type=None), abstract_hash=abstract_hash)
                                if without_type == state[0]:
                                    version = state[0]
                            paper_event = (NEW_PAPER if state is None else
                                           MATERIAL_UPDATE if state[0] != version else
                                           DUPLICATE_SOURCE if event == "new" else UNCHANGED)
                            db.execute("INSERT INTO topic_paper_state VALUES (?,?,?,?,?) ON CONFLICT(topic_id,paper_id) DO UPDATE SET content_version=excluded.content_version,last_seen_at=excluded.last_seen_at",
                                       (topic_id, paper.paper_id, version, state[1] if state else now, now))
                            if paper_event in (NEW_PAPER, MATERIAL_UPDATE):
                                recommendation = evaluator(paper, paper_event, version) if evaluator else None
                                db.execute("INSERT OR IGNORE INTO paper_events VALUES (?,?,?,?,?,?,NULL)",
                                           (topic_id, paper.paper_id, version, paper_event,
                                            json.dumps(recommendation, ensure_ascii=False) if recommendation else None, now))
                            elif evaluator:
                                pending = db.execute("SELECT event_type,recommendation_json FROM paper_events WHERE topic_id=? AND paper_id=? AND content_version=? AND processed_at IS NULL LIMIT 1",
                                                     (topic_id, paper.paper_id, version)).fetchone()
                                if pending:
                                    if paper.abstract is None and pending[1]:
                                        recommendation = json.loads(pending[1])
                                        recommendation["paper"]["sources"] = sorted({item.provider_id for item in paper.source_records})
                                    else:
                                        recommendation = evaluator(paper, pending[0], version)
                                    db.execute("UPDATE paper_events SET recommendation_json=? WHERE topic_id=? AND paper_id=? AND content_version=? AND event_type=? AND processed_at IS NULL",
                                               (json.dumps(recommendation, ensure_ascii=False), topic_id,
                                                paper.paper_id, version, pending[0]))
                        else:
                            prior_specialty = db.execute(
                                "SELECT 1 FROM specialty_topic_papers WHERE topic_id=? AND paper_id=?",
                                (topic_id, paper.paper_id)).fetchone()
                            db.execute("INSERT INTO specialty_topic_papers VALUES (?,?,?,?) "
                                       "ON CONFLICT(topic_id,paper_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                                       (topic_id, paper.paper_id, now, now))
                            paper_event = (NEW_PAPER if prior_specialty is None else
                                           MATERIAL_UPDATE if event == "updated" else UNCHANGED)
                        page_events.append({"paper_id": paper.paper_id, "event_type": paper_event,
                                            "content_version": version})
                        papers.append(paper)
                        events[event] += 1
                        db.execute("INSERT OR IGNORE INTO source_query_provenance VALUES (?,?,?,?,?)",
                                   (record.provider_id, record.source_id, topic_id, plan.query_signature, now))
                        db.execute("RELEASE record")
                    except IdentityConflict:
                        db.execute("ROLLBACK TO record")
                        db.execute("RELEASE record")
                        now = utcnow()
                        db.execute("INSERT INTO identity_conflicts VALUES (?,?,?,?,?) ON CONFLICT(provider_id,source_id) DO UPDATE SET reason=excluded.reason,last_seen_at=excluded.last_seen_at",
                                   (record.provider_id, record.source_id, "identifier_conflict", now, now))
                        conflicts += 1
                        page_events.append({"paper_id": None, "event_type": REVIEW_CONFLICT,
                                            "provider_id": record.provider_id, "source_id": record.source_id})
                now = utcnow()
                if citation_edge is not None:
                    seed_id, relation = citation_edge
                    if relation not in ("references", "citations"):
                        raise ValueError("citation relation must be references or citations")
                    for related in {paper.paper_id for paper in papers} - {seed_id}:
                        db.execute("INSERT OR IGNORE INTO citation_edges VALUES (?,?,?,?,?,?,?)",
                                   (topic_id, seed_id, related, relation,
                                    plan.provider_id, plan.query_signature, now))
                for entry in malformed_records or []:
                    db.execute("INSERT OR IGNORE INTO record_quarantine VALUES (?,?,?,?,?,?,?,?)",
                               (plan.provider_id, topic_id, plan.query_signature,
                                entry["payload_sha256"], entry.get("identifier"), entry["category"],
                                (prior[0] if prior else 0) + 1, now))
                db.execute("INSERT INTO provider_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider_id,topic_id,query_signature) DO UPDATE SET cursor_json=excluded.cursor_json,page_number=excluded.page_number,records_fetched=excluded.records_fetched,status=excluded.status,last_success_at=excluded.last_success_at,updated_at=excluded.updated_at",
                           (plan.provider_id, topic_id, plan.query_id, plan.query_signature,
                            plan.date_window.start if plan.date_window else None,
                            plan.date_window.end if plan.date_window else None,
                            json.dumps(page.next_cursor), (prior[0] if prior else 0)+1,
                            (prior[1] if prior else 0)+len(page.records),
                            "IN_PROGRESS" if page.has_more else "SUCCESS",
                            (prior[2] if prior else None) if page.has_more else now, now))
        if event_counts is not None:
            event_counts.update(events)
        if paper_events is not None:
            paper_events.extend(page_events)
        return papers, conflicts

    def pending_events(self, topic_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM paper_events WHERE topic_id=? AND processed_at IS NULL ORDER BY created_at, paper_id",
                (topic_id,))]

    def citation_related(self, topic_id: str, query_signatures: list[str]) -> list[str]:
        if not query_signatures:
            return []
        markers = ",".join("?" for _ in query_signatures)
        with closing(self._connect()) as db:
            return [row[0] for row in db.execute(
                f"SELECT DISTINCT related_paper_id FROM citation_edges WHERE topic_id=? "
                f"AND query_signature IN ({markers}) ORDER BY related_paper_id",
                (topic_id, *query_signatures))]

    def citation_connections(self, topic_id: str, query_signatures: list[str]) -> list[dict]:
        """One-hop seed relationships for an exact set of successful queries."""
        if not query_signatures:
            return []
        markers = ",".join("?" for _ in query_signatures)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT DISTINCT seed_paper_id,related_paper_id,relation,provider_id "
                f"FROM citation_edges WHERE topic_id=? AND query_signature IN ({markers}) "
                "ORDER BY related_paper_id,seed_paper_id,relation,provider_id",
                (topic_id, *query_signatures)).fetchall()
        return [{"seed_paper_id": row[0], "related_paper_id": row[1],
                 "relation": row[2], "provider_id": row[3]} for row in rows]

    def queue_pending(self, topic_id: str, channels: list[str], *,
                      allowed_paper_ids: set[str] | None = None,
                      payload_overrides: Mapping[str, dict] | None = None,
                      once_per_topic_paper: bool = False) -> int:
        """Coalesce each Paper's events; queue only the final eligible revision."""
        queued = 0
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT paper_id,content_version,event_type,recommendation_json FROM paper_events WHERE topic_id=? AND processed_at IS NULL ORDER BY created_at,rowid",
                                  (topic_id,)).fetchall()
                grouped = {}
                for paper_id, version, kind, payload in rows:
                    prior = grouped.get(paper_id)
                    grouped[paper_id] = (version, NEW_PAPER if kind == NEW_PAPER or (prior and prior[1] == NEW_PAPER) else kind, payload)
                now = utcnow()
                for paper_id, (version, kind, payload) in grouped.items():
                    recommendation = json.loads(payload) if payload else None
                    if recommendation and (payload_overrides or {}).get(paper_id):
                        recommendation.update(payload_overrides[paper_id])
                        payload = json.dumps(recommendation, ensure_ascii=False)
                    if recommendation and recommendation.get("eligible") and (allowed_paper_ids is None or paper_id in allowed_paper_ids):
                        for channel in channels:
                            if once_per_topic_paper and db.execute(
                                "SELECT 1 FROM notification_outbox WHERE topic_id=? AND paper_id=? AND channel=? AND status='SENT' LIMIT 1",
                                (topic_id, paper_id, channel)).fetchone():
                                continue
                            key = hashlib.sha256(json.dumps([topic_id, paper_id, version, channel]).encode()).hexdigest()
                            db.execute("INSERT OR IGNORE INTO notification_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (key, topic_id, paper_id, kind, version, channel, "PENDING", 0,
                                        now, None, now, None, payload))
                            queued += db.execute("SELECT changes()").fetchone()[0]
                # All pending events were examined above. Mark them in one
                # indexed statement; one UPDATE per paper becomes quadratic
                # for broad Weekly backchecks with tens of thousands of hits.
                db.execute("UPDATE paper_events SET processed_at=? WHERE topic_id=? AND processed_at IS NULL",
                           (now, topic_id))
        return queued

    def claim_notification(self, *, lease_seconds: int = 300, max_attempts: int = 3,
                           channels: tuple[str, ...] | None = None,
                           exclude: tuple[str, ...] = (),
                           topic_id: str | None = None,
                           allowed_paper_ids: set[str] | None = None,
                           allowed_versions: Mapping[str, str] | None = None) -> dict | None:
        from datetime import datetime, timedelta, timezone
        now = utcnow()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)).isoformat()
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                if channels is not None and not channels:
                    return None
                channel_filter = (" AND channel IN (" + ",".join("?" for _ in channels) + ")") if channels else ""
                exclude_filter = (" AND idempotency_key NOT IN (" + ",".join("?" for _ in exclude) + ")") if exclude else ""
                topic_filter = " AND topic_id=?" if topic_id is not None else ""
                allowed_filter = " AND paper_id IN (SELECT value FROM json_each(?))" if allowed_paper_ids is not None else ""
                version_filter = (" AND EXISTS (SELECT 1 FROM json_each(?) AS selected "
                                  "WHERE selected.key=notification_outbox.paper_id "
                                  "AND selected.value=notification_outbox.content_version)") if allowed_versions is not None else ""
                row = db.execute("SELECT * FROM notification_outbox WHERE attempts<? AND (status IN ('PENDING','FAILED') OR (status='SENDING' AND updated_at<?))" + channel_filter + exclude_filter + topic_filter + allowed_filter + version_filter + " ORDER BY created_at,idempotency_key LIMIT 1",
                                 (max_attempts, expired, *(channels or ()), *exclude,
                                  *((topic_id,) if topic_id is not None else ()),
                                  *((json.dumps(sorted(allowed_paper_ids)),) if allowed_paper_ids is not None else ()),
                                  *((json.dumps(allowed_versions, sort_keys=True),) if allowed_versions is not None else ()))).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE notification_outbox SET status='SENDING',attempts=attempts+1,updated_at=? WHERE idempotency_key=?",
                           (now, row["idempotency_key"]))
                result = dict(row)
                result["attempts"] += 1
                return result

    def count_unselected_notifications(self, topic_id: str, channels: tuple[str, ...],
                                       allowed_versions: Mapping[str, str]) -> int:
        """Audit old queued alerts that this Weekly run will not dispatch."""
        if not channels:
            return 0
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE topic_id=? "
                "AND status IN ('PENDING','FAILED','SENDING') AND channel IN (" +
                ",".join("?" for _ in channels) + ") "
                "AND NOT EXISTS (SELECT 1 FROM json_each(?) AS selected "
                "WHERE selected.key=notification_outbox.paper_id "
                "AND selected.value=notification_outbox.content_version)",
                (topic_id, *channels, json.dumps(allowed_versions, sort_keys=True))).fetchone()[0]

    def count_unresolved_notifications(self, topic_id: str, channels: tuple[str, ...],
                                       allowed_versions: Mapping[str, str]) -> int:
        """Failed or pending alerts for this run's exact eligible HIGH revisions."""
        if not channels or not allowed_versions:
            return 0
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE topic_id=? "
                "AND status IN ('PENDING','FAILED','SENDING') AND channel IN (" +
                ",".join("?" for _ in channels) + ") "
                "AND EXISTS (SELECT 1 FROM json_each(?) AS selected "
                "WHERE selected.key=notification_outbox.paper_id "
                "AND selected.value=notification_outbox.content_version)",
                (topic_id, *channels, json.dumps(allowed_versions, sort_keys=True))).fetchone()[0]

    def complete_notification(self, key: str, *, sent: bool, error: str | None = None) -> None:
        now = utcnow()
        with closing(self._connect()) as db:
            with db:
                db.execute("UPDATE notification_outbox SET status=?,sent_at=?,updated_at=?,last_error=? WHERE idempotency_key=? AND status='SENDING'",
                           ("SENT" if sent else "FAILED", now if sent else None, now,
                            None if sent else (error or "delivery_error")[:120], key))

    def retry_failed_notifications(self, *, channel: str | None = None) -> int:
        """Explicit operator reset after correcting a persistent channel problem."""
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute("UPDATE notification_outbox SET status='PENDING',attempts=0,last_error=NULL,updated_at=? WHERE status='FAILED' AND (? IS NULL OR channel=?)",
                                    (utcnow(), channel, channel))
                return cursor.rowcount

    def claim_due(self, topic_id: str, provider_id: str, *, now: str,
                  lease_until: str) -> bool:
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT next_due_at,lease_until FROM schedule_state WHERE topic_id=? AND provider_id=?",
                                 (topic_id, provider_id)).fetchone()
                if row and (row[0] > now or (row[1] and row[1] > now)):
                    return False
                db.execute("INSERT INTO schedule_state VALUES (?,?,?,?,?,?) ON CONFLICT(topic_id,provider_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,lease_until=excluded.lease_until",
                           (topic_id, provider_id, now, now, lease_until, None))
                return True

    def finish_due(self, topic_id: str, provider_id: str, *, next_due_at: str,
                   status: str, lease_until: str | None = None) -> None:
        with closing(self._connect()) as db:
            with db:
                db.execute("UPDATE schedule_state SET next_due_at=?,lease_until=NULL,last_status=? WHERE topic_id=? AND provider_id=? AND (? IS NULL OR lease_until=?)",
                           (next_due_at, status, topic_id, provider_id, lease_until, lease_until))

    def mark_failed(self, topic_id: str, plan: QueryPlan,
                    *, expected_page_number: int | None = None) -> None:
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT page_number,status FROM provider_checkpoints WHERE provider_id=? AND topic_id=? AND query_signature=?",
                    (plan.provider_id, topic_id, plan.query_signature)).fetchone()
                now = utcnow()
                if row is None:
                    if expected_page_number not in (None, 0):
                        return
                    db.execute("INSERT INTO provider_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (plan.provider_id, topic_id, plan.query_id, plan.query_signature,
                                plan.date_window.start if plan.date_window else None,
                                plan.date_window.end if plan.date_window else None,
                                json.dumps(None), 0, 0, "FAILED", None, now))
                elif row[1] != "SUCCESS" and (expected_page_number is None or row[0] == expected_page_number):
                    db.execute("UPDATE provider_checkpoints SET status='FAILED',updated_at=? "
                               "WHERE provider_id=? AND topic_id=? AND query_signature=?",
                               (now, plan.provider_id, topic_id, plan.query_signature))

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM papers_v2").fetchone()[0]

    def topic_papers(self, topic_id: str) -> list[Paper]:
        """Read the unified identities observed for one Topic only."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT p.payload FROM papers_v2 p "
                "LEFT JOIN topic_paper_state s ON s.paper_id=p.paper_id AND s.topic_id=? "
                "LEFT JOIN specialty_topic_papers x ON x.paper_id=p.paper_id AND x.topic_id=? "
                "WHERE s.paper_id IS NOT NULL OR x.paper_id IS NOT NULL "
                "ORDER BY COALESCE(s.first_seen_at,x.first_seen_at),p.paper_id",
                (topic_id, topic_id)).fetchall()
        return [Paper.from_dict(json.loads(row[0])) for row in rows]

    def commit_research_evaluations(self, snapshot: dict, result: dict,
                                    changes: list[dict] | None = None) -> None:
        """Persist a complete human review atomically; repeats must be identical."""
        topic, run_id = snapshot["topic_id"], snapshot["run_id"]
        selected = set(result["selected_paper_ids"])
        rows = result["evaluations"]
        if {item["paper_id"] for item in rows} != {item["paper_id"] for item in snapshot["candidates"]}:
            raise ValueError("research evaluation is incomplete")
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                now = utcnow()
                for item in rows:
                    payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    key = (topic, run_id, item["paper_id"])
                    prior = db.execute(
                        "SELECT payload,selected FROM research_evaluations "
                        "WHERE topic_id=? AND run_id=? AND paper_id=?", key).fetchone()
                    chosen = int(item["paper_id"] in selected)
                    if prior is not None:
                        if prior != (payload, chosen):
                            raise ValueError("existing research evaluation differs")
                    else:
                        db.execute("INSERT INTO research_evaluations VALUES (?,?,?,?,?,?,?,?,?)",
                                   (topic, run_id, snapshot["focus_version"], snapshot["mode"],
                                    item["paper_id"], payload, item["overall_score"], chosen, now))
                lineage = snapshot.get("lineage")
                if lineage:
                    for change in changes or []:
                        key = (topic, lineage["source_run_id"], run_id, change["paper_id"])
                        payload = json.dumps(change, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))
                        prior = db.execute(
                            "SELECT payload FROM research_evaluation_revisions "
                            "WHERE topic_id=? AND source_run_id=? AND reevaluation_run_id=? AND paper_id=?",
                            key).fetchone()
                        if prior is not None:
                            if prior[0] != payload:
                                raise ValueError("existing research revision differs")
                        else:
                            db.execute("INSERT INTO research_evaluation_revisions VALUES (?,?,?,?,?,?,?,?)",
                                       (*key, change["old_score"], change["new_score"], payload, now))

    def topic_content_versions(self, topic_id: str) -> dict[str, str]:
        """Use committed versions for delivery, including cached abstract hashes."""
        with closing(self._connect()) as db:
            return dict(db.execute(
                "SELECT paper_id,content_version FROM topic_paper_state WHERE topic_id=?",
                (topic_id,)))

    def preprint_links(self) -> set[tuple[str, str]]:
        """Return (preprint ID, published DOI) links without changing paper keys."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT paper_id,target_value FROM paper_relations WHERE relation_type='published_as' "
                "AND target_kind='doi'").fetchall()
        return {(paper_id, doi) for paper_id, doi in rows}

    def relation_count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM paper_relations").fetchone()[0]

    def record_run(self, topic_id: str, summary: dict) -> None:
        now = utcnow()
        compact = {key: summary.get(key) for key in
                   ("status", "providers", "papers_seen", "priority_counts",
                    "paper_event_counts", "notifications")}
        with closing(self._connect()) as db:
            with db:
                db.execute("INSERT INTO sync_runs VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex, topic_id, now, now, summary["status"],
                            json.dumps(compact, ensure_ascii=False)))
