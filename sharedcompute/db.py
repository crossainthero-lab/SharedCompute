"""SQLite access. One short-lived connection per unit of work; writes are serialized."""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def migrations() -> list[tuple[int, Path]]:
    """Ordered (version, file) pairs from migrations/NNNN_name.sql."""
    found = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = re.match(r"^(\d{4})_", path.name)
        if m:
            found.append((int(m.group(1)), path))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"Migrations must be numbered 0001.. without gaps, found {versions}")
    return found


SCHEMA_VERSION = len(migrations())


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

    def version(self) -> int:
        with self.read() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self, target: int | None = None) -> list[int]:
        """Apply pending migrations up to `target` (default: latest). Returns the versions applied."""
        target = SCHEMA_VERSION if target is None else target
        self.path.parent.mkdir(parents=True, exist_ok=True)
        applied = []
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"Database {self.path} has schema version {version}; this build "
                                       f"only knows up to {SCHEMA_VERSION}. Upgrade SharedCompute.")
                if version == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'providers'").fetchone():
                    raise RuntimeError(f"{self.path} is a v0.1 prototype database. Move it aside; "
                                       "the v0.1 demo data is not migrated.")
                for number, path in migrations():
                    if version < number <= target:
                        # Each migration and its version bump commit atomically.
                        try:
                            conn.executescript("BEGIN;" + path.read_text() + f"\nPRAGMA user_version = {number}; COMMIT;")
                        except sqlite3.Error as exc:
                            if conn.in_transaction:
                                conn.execute("ROLLBACK")
                            raise RuntimeError(f"Migration {path.name} failed: {exc}") from exc
                        applied.append(number)
                return applied
            finally:
                conn.close()
