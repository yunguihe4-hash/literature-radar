"""Connection closure and transaction behavior for the local paper store."""
import gc
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from literature_radar.database.store import PaperStore
from literature_radar.models import Paper


class TrackingConnection(sqlite3.Connection):
    def close(self):
        self.closed_by_store = True
        super().close()


class FaultConnection(TrackingConnection):
    def executemany(self, statement, rows):
        super().executemany(statement, rows)
        raise RuntimeError("synthetic transaction failure")


class SQLiteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "papers.sqlite3"
        self.store = PaperStore(self.path)
        self.paper = Paper("SYN-1", "Fictional coating note", "Synthetic abstract", "synthetic-index")

    def tracked_connect(self, factory=TrackingConnection):
        original = sqlite3.connect
        connections = []
        def connect(path):
            connection = original(path, factory=factory)
            connections.append(connection)
            return connection
        return connect, connections

    def test_write_read_and_repeated_closure(self):
        connect, connections = self.tracked_connect()
        with patch("literature_radar.database.store.sqlite3.connect", side_effect=connect):
            for _ in range(3):
                self.store.save("synthetic", [self.paper])
                self.assertEqual(self.store.count("synthetic"), 1)
        self.assertEqual(len(connections), 6)
        self.assertTrue(all(getattr(connection, "closed_by_store", False) for connection in connections))
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.path.unlink()
        self.assertFalse(self.path.exists())

    def test_exception_rolls_back_and_closes(self):
        self.store.save("synthetic", [self.paper])
        second = Paper("SYN-2", "Fictional second note", "Synthetic abstract", "synthetic-index")
        connect, connections = self.tracked_connect(FaultConnection)
        with patch("literature_radar.database.store.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(RuntimeError, "synthetic transaction failure"):
                self.store.save("synthetic", [second])
        self.assertTrue(all(getattr(connection, "closed_by_store", False) for connection in connections))
        self.assertEqual(self.store.count("synthetic"), 1)

    def test_no_unclosed_database_resource_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for _ in range(3):
                self.store.save("synthetic", [self.paper])
                self.store.count("synthetic")
            gc.collect()
        self.assertFalse([warning for warning in caught if "unclosed database" in str(warning.message)])
