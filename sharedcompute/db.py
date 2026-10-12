"""SQLite access. One short-lived connection per unit of work; writes are serialized."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        # SQLite allows one writer; serializing in-process avoids SQLITE_BUSY churn and
        # makes scheduler decisions atomic with respect to each other.
        self._write_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    def migrate(self) -> bool:
        """Create the schema if needed. Returns True when a fresh database was created."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == SCHEMA_VERSION:
                    return False
                if version != 0:
                    raise RuntimeError(
                        f"Database {self.path} has schema version {version}; "
                        f"this build expects {SCHEMA_VERSION}."
                    )
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'providers'").fetchone():
                    raise RuntimeError(
                        f"{self.path} is a v0.1 prototype database. Move it aside; "
                        "the v0.1 demo data is not migrated."
                    )
                conn.executescript("BEGIN;" + SCHEMA_FILE.read_text() +
                                   f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
                return True
            finally:
                conn.close()
