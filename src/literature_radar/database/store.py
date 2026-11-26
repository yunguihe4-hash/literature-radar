"""SQLite store using an explicitly supplied path."""
from __future__ import annotations
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from literature_radar.models import Paper

class PaperStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def save(self, topic_id: str, papers: list[Paper]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS papers (topic_id TEXT NOT NULL, paper_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(topic_id,paper_key))")
                from literature_radar.candidates.processing import identity
                db.executemany("INSERT OR REPLACE INTO papers VALUES (?,?,?)", [(topic_id, identity(p), json.dumps(p.as_dict(), ensure_ascii=False)) for p in papers])

    def count(self, topic_id: str) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            return int(db.execute("SELECT COUNT(*) FROM papers WHERE topic_id=?", (topic_id,)).fetchone()[0])


    def load(self, topic_id: str, paper_key: str) -> Paper | None:
        """Read an existing v1 or v2 payload without changing the table."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT payload FROM papers WHERE topic_id=? AND paper_key=?",
                             (topic_id, paper_key)).fetchone()
        return Paper.from_dict(json.loads(row[0])) if row else None
