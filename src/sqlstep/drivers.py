"""Talking to the two databases sqlstep supports.

The interesting differences between them are locking and transactional DDL, and
both are handled explicitly rather than pretended away.

*Locking.* Two deploys landing at once must not run the same migration twice.
Postgres has session advisory locks, which are released when the connection
drops, so a crashed migrator does not leave the lock held forever. SQLite has one
writer at a time by design, so an immediate transaction is the lock.

*Transactional DDL.* Postgres and SQLite both allow DDL inside a transaction, so
a failed migration rolls back cleanly. That is not true of MySQL, which is part
of why it is not supported yet rather than half-supported.
"""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlstep.errors import LockUnavailable, MissingDriver, UnsupportedDatabase
from sqlstep.migrations import Migration
from sqlstep.split import split

DEFAULT_TABLE = "schema_migrations"
#: Key for the Postgres advisory lock. Arbitrary, but must be the same everywhere.
ADVISORY_LOCK_KEY = 3_141_592_653


@dataclass
class Applied:
    """One row of the migrations table."""

    version: str
    name: str
    checksum: str
    applied_at: str = ""
    execution_ms: int = 0


class Driver(ABC):
    """What sqlstep needs from a database."""

    name: str
    supports_transactional_ddl: bool = True

    def __init__(self, url: str, table: str = DEFAULT_TABLE) -> None:
        from sqlstep.config import is_identifier

        # Checked here as well as at config load, because open_driver and the
        # concrete constructors are public and both drivers interpolate this
        # into SQL without quoting.
        if not is_identifier(table):
            raise UnsupportedDatabase(
                f"{table!r} is not a usable table name: letters, digits and "
                "underscores, starting with a letter or underscore"
            )
        self.url = url
        self.table = table

    def __enter__(self) -> Driver:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def ensure_table(self) -> None: ...

    @abstractmethod
    def applied(self) -> list[Applied]: ...

    @abstractmethod
    def record(self, migration: Migration, execution_ms: int) -> None: ...

    @abstractmethod
    def forget(self, version: str) -> None: ...

    @abstractmethod
    def run_sql(self, sql: str) -> None:
        """Execute a script. Transaction handling is the caller's business."""

    @abstractmethod
    def update_checksum(self, version: str, checksum: str) -> None:
        """Change a recorded checksum, leaving applied_at and execution_ms alone."""

    @contextmanager
    @abstractmethod
    def atomic(self, enabled: bool = True) -> Iterator[None]:
        """Run the body in one transaction, so a failure undoes all of it."""

    @contextmanager
    @abstractmethod
    def lock(self, timeout: float = 30.0) -> Iterator[None]: ...

    def is_applied(self, version: str) -> bool:
        return any(row.version == version for row in self.applied())

    def apply(self, migration: Migration, sql: str) -> int:
        """Run one migration's SQL and return how long it took, in milliseconds."""
        started = time.perf_counter()
        self.run_sql(sql)
        return int((time.perf_counter() - started) * 1000)


class SQLiteDriver(Driver):
    name = "sqlite"
    supports_transactional_ddl = True

    def __init__(self, url: str, table: str = DEFAULT_TABLE) -> None:
        super().__init__(url, table)
        self.path = _sqlite_path(url)
        self.connection: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @property
    def _conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("driver is not connected")
        return self.connection

    def ensure_table(self) -> None:
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "  version TEXT PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  checksum TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL,"
            "  execution_ms INTEGER NOT NULL DEFAULT 0)"
        )

    def applied(self) -> list[Applied]:
        rows = self._conn.execute(
            f"SELECT version, name, checksum, applied_at, execution_ms FROM {self.table} "
            "ORDER BY length(version), version"
        ).fetchall()
        return [Applied(*row) for row in rows]

    def record(self, migration: Migration, execution_ms: int) -> None:
        self._conn.execute(
            f"INSERT INTO {self.table} (version, name, checksum, applied_at, execution_ms) "
            "VALUES (?, ?, ?, datetime('now'), ?)",
            (migration.version, migration.name, migration.checksum, execution_ms),
        )

    def forget(self, version: str) -> None:
        self._conn.execute(f"DELETE FROM {self.table} WHERE version = ?", (version,))

    def run_sql(self, sql: str) -> None:
        for statement in split(sql):
            self._conn.execute(statement)

    def update_checksum(self, version: str, checksum: str) -> None:
        self._conn.execute(
            f"UPDATE {self.table} SET checksum = ? WHERE version = ?", (checksum, version)
        )

    @contextmanager
    def atomic(self, enabled: bool = True) -> Iterator[None]:
        """One immediate transaction, which is also SQLite's mutual exclusion.

        ``BEGIN IMMEDIATE`` takes the write lock straight away rather than on
        first write, so a second migrator blocks here instead of discovering the
        conflict halfway through. Combined with the applied-check the runner does
        inside this block, that is what stops two processes applying the same
        migration.
        """
        if not enabled:
            yield
            return
        deadline = time.monotonic() + 30.0
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() > deadline:
                    raise LockUnavailable(f"could not lock {self.path}: {error}") from error
                time.sleep(0.05)
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    @contextmanager
    def lock(self, timeout: float = 30.0) -> Iterator[None]:
        """No separate lock on SQLite.

        SQLite permits one writer, and each migration's ``BEGIN IMMEDIATE`` takes
        that writer lock for as long as the migration and its bookkeeping row
        take. A second lock around the whole run would have to be held across
        those transactions, which SQLite has no way to express.
        """
        yield


class PostgresDriver(Driver):
    name = "postgres"
    supports_transactional_ddl = True

    def __init__(self, url: str, table: str = DEFAULT_TABLE) -> None:
        super().__init__(url, table)
        self.connection: Any = None

    def connect(self) -> None:
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - depends on the install
            raise MissingDriver(
                "PostgreSQL needs psycopg. Install the extra: pip install 'sqlstep[postgres]'"
            ) from error
        self.connection = psycopg.connect(self.url, autocommit=True)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def ensure_table(self) -> None:
        """Create the bookkeeping table, tolerating another process doing it too.

        ``CREATE TABLE IF NOT EXISTS`` is not race-safe in PostgreSQL: two
        sessions running it at the same moment can still collide on the system
        catalogue and raise a unique violation. Since two deploys landing
        together is the exact case the advisory lock exists for, and the table
        has to exist before the lock can be reasoned about, the collision is
        caught and the table re-checked instead.
        """
        statement = (
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "  version TEXT PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  checksum TEXT NOT NULL,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
            "  execution_ms INTEGER NOT NULL DEFAULT 0)"
        )
        try:
            self.connection.execute(statement)
        except Exception as error:
            if not _is_duplicate_object(error):
                raise
            # Someone else won the race. Confirm the table is really there
            # rather than assuming.
            self.connection.execute(f"SELECT 1 FROM {self.table} WHERE false")

    def applied(self) -> list[Applied]:
        rows = self.connection.execute(
            f"SELECT version, name, checksum, applied_at, execution_ms FROM {self.table} "
            "ORDER BY length(version), version"
        ).fetchall()
        return [Applied(r[0], r[1], r[2], str(r[3]), r[4]) for r in rows]

    def record(self, migration: Migration, execution_ms: int) -> None:
        self.connection.execute(
            f"INSERT INTO {self.table} (version, name, checksum, execution_ms) "
            "VALUES (%s, %s, %s, %s)",
            (migration.version, migration.name, migration.checksum, execution_ms),
        )

    def forget(self, version: str) -> None:
        self.connection.execute(f"DELETE FROM {self.table} WHERE version = %s", (version,))

    def run_sql(self, sql: str) -> None:
        # One statement per execute. Sending several in a single message uses
        # the simple query protocol, which wraps them in one implicit
        # transaction, and that is exactly what a no-transaction migration is
        # asking not to happen.
        for statement in split(sql):
            self.connection.execute(statement)

    def update_checksum(self, version: str, checksum: str) -> None:
        self.connection.execute(
            f"UPDATE {self.table} SET checksum = %s WHERE version = %s", (checksum, version)
        )

    @contextmanager
    def atomic(self, enabled: bool = True) -> Iterator[None]:
        if not enabled:
            yield
            return
        with self.connection.transaction():
            yield

    @contextmanager
    def lock(self, timeout: float = 30.0) -> Iterator[None]:
        """A session advisory lock, which Postgres drops if the connection dies."""
        deadline = time.monotonic() + timeout
        while True:
            got = self.connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)
            ).fetchone()[0]
            if got:
                break
            if time.monotonic() > deadline:
                raise LockUnavailable(
                    f"another sqlstep is migrating this database and did not finish "
                    f"within {timeout:g}s"
                )
            time.sleep(0.2)
        try:
            yield
        finally:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


#: PostgreSQL SQLSTATEs for "this object already exists".
_DUPLICATE_OBJECT = frozenset({"42P07", "23505", "42710"})


def _is_duplicate_object(error: BaseException) -> bool:
    return str(getattr(error, "sqlstate", "") or "") in _DUPLICATE_OBJECT


def _sqlite_path(url: str) -> str:
    if url.startswith("sqlite://"):
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc != "":
            return ":memory:" if parsed.netloc == ":memory:" else parsed.netloc + path
        return path.lstrip("/") if path.startswith("///") else path or ":memory:"
    return url


def open_driver(url: str, table: str = DEFAULT_TABLE) -> Driver:
    """Pick a driver from the URL."""
    lowered = url.lower()
    if lowered.startswith(("postgres://", "postgresql://")):
        return PostgresDriver(url, table)
    if lowered.startswith("sqlite://") or lowered.endswith((".db", ".sqlite", ".sqlite3")):
        return SQLiteDriver(url, table)
    if lowered == ":memory:":
        return SQLiteDriver(":memory:", table)
    raise UnsupportedDatabase(
        f"{url!r} does not name a database sqlstep can talk to. Supported: "
        "postgres://..., postgresql://..., sqlite:///path.db, or a path ending in "
        ".db, .sqlite or .sqlite3."
    )
