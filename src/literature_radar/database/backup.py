"""Consistent SQLite copy and restore without overwriting existing data."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> Path:
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError("source database does not exist")
    if destination.exists():
        raise FileExistsError("destination database already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".literature-backup-", suffix=".sqlite3",
                                              dir=destination.parent)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("backup integrity check failed")
        if destination.exists():
            raise FileExistsError("destination database appeared during backup")
        os.link(temporary, destination)  # Atomic no-overwrite publication.
    finally:
        os.unlink(temporary)
    return destination


def restore_database(backup: Path, destination: Path) -> Path:
    """Restore to a new path; callers switch paths only after verification."""
    return backup_database(backup, destination)
