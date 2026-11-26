"""Conservative, additive migration of one Topic from a verified SQLite database."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


_PAPER_TABLES = (
    "identifiers", "source_records", "field_provenance", "paper_relations",
    "paper_field_hashes",
)
_TOPIC_TABLES = (
    "topic_paper_state", "source_query_provenance", "paper_events",
    "notification_outbox", "provider_checkpoints", "record_quarantine",
    "legacy_paper_mapping", "sync_runs", "schedule_state",
)


def _columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))


def merge_topic(source: Path, destination: Path, topic_id: str) -> dict:
    """Copy missing rows for a Topic while preserving all destination identities.

    The operation is one SQLite transaction. Any identifier or source-record
    collision with a different paper ID aborts before a write. Existing rows
    are never replaced, which also makes a verified retry idempotent.
    """
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ")
    if not source.is_file() or not destination.is_file() or not topic_id:
        raise ValueError("Existing databases and a Topic ID are required")
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as donor, \
         closing(sqlite3.connect(destination, timeout=30)) as target:
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("PRAGMA busy_timeout=30000")
        if donor.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Source database failed quick_check")
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Destination database failed quick_check")
        if donor.execute("PRAGMA user_version").fetchone() != target.execute("PRAGMA user_version").fetchone():
            raise ValueError("Database schema versions differ")
        for table in ("papers_v2", *_PAPER_TABLES, *_TOPIC_TABLES):
            if not _columns(donor, table) or _columns(donor, table) != _columns(target, table):
                raise ValueError(f"Table schema differs: {table}")
        donor_ids = {row[0] for row in donor.execute(
            "SELECT paper_id FROM topic_paper_state WHERE topic_id=?", (topic_id,))}
        if not donor_ids:
            raise ValueError("Source has no Topic papers")
        before = target.execute(
            "SELECT COUNT(*) FROM topic_paper_state WHERE topic_id=?", (topic_id,)).fetchone()[0]
        for kind, value, paper_id in donor.execute(
            "SELECT kind,value,paper_id FROM identifiers WHERE paper_id IN "
            "(SELECT paper_id FROM topic_paper_state WHERE topic_id=?)", (topic_id,)):
            row = target.execute("SELECT paper_id FROM identifiers WHERE kind=? AND value=?",
                                 (kind, value)).fetchone()
            if row and row[0] != paper_id:
                raise ValueError(f"Identifier collision: {kind}:{value}")
        for provider, source_id, paper_id in donor.execute(
            "SELECT provider_id,source_id,paper_id FROM source_records WHERE paper_id IN "
            "(SELECT paper_id FROM topic_paper_state WHERE topic_id=?)", (topic_id,)):
            row = target.execute("SELECT paper_id FROM source_records WHERE provider_id=? AND source_id=?",
                                 (provider, source_id)).fetchone()
            if row and row[0] != paper_id:
                raise ValueError(f"Source-record collision: {provider}:{source_id}")
        selected = {"papers_v2": (
            "SELECT * FROM papers_v2 WHERE paper_id IN "
            "(SELECT paper_id FROM topic_paper_state WHERE topic_id=?)", (topic_id,))}
        selected.update({table: (
            f"SELECT * FROM {table} WHERE paper_id IN "
            "(SELECT paper_id FROM topic_paper_state WHERE topic_id=?)", (topic_id,))
            for table in _PAPER_TABLES})
        selected.update({table: (f"SELECT * FROM {table} WHERE topic_id=?", (topic_id,))
                         for table in _TOPIC_TABLES})
        inserted = {}
        existing = {}
        try:
            target.execute("BEGIN IMMEDIATE")
            for table in ("papers_v2", *_PAPER_TABLES, *_TOPIC_TABLES):
                columns = _columns(donor, table)
                sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
                rows = donor.execute(*selected[table]).fetchall()
                count = 0
                for row in rows:
                    count += target.execute(sql, row).rowcount
                inserted[table] = count
                existing[table] = len(rows) - count
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Foreign-key check failed during Topic migration")
            staged = target.execute(
                "SELECT COUNT(*) FROM topic_paper_state WHERE topic_id=?", (topic_id,)).fetchone()[0]
            if staged != len(donor_ids):
                raise ValueError("Topic migration did not stage every source association")
            target.commit()
        except BaseException:
            target.rollback()
            raise
        after = target.execute(
            "SELECT COUNT(*) FROM topic_paper_state WHERE topic_id=?", (topic_id,)).fetchone()[0]
        if after != len(donor_ids) or target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Post-migration verification failed; restore from the preflight backup")
        return {"topic_id": topic_id, "source": str(source), "destination": str(destination),
                "donor_topic_papers": len(donor_ids), "destination_topic_papers_before": before,
                "destination_topic_papers_after": after, "inserted": inserted,
                "already_present": existing, "status": "SUCCESS"}
