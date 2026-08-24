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
    def run(self, sql: str, *, in_transaction: bool) -> None: ...

    @contextmanager
    @abstractmethod
    def lock(self, timeout: float = 30.0) -> Iterator[None]: ...

    def apply(self, migration: Migration, sql: str) -> int:
        """Run one migration's SQL and return how long it took, in milliseconds."""
        started = time.perf_counter()
        self.run(sql, in_transaction=not migration.no_transaction)
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

    def run(self, sql: str, *, in_transaction: bool) -> None:
        statements = split(sql)
        if in_transaction:
            self._conn.execute("BEGIN")
        try:
            for statement in statements:
                self._conn.execute(statement)
        except Exception:
            if in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        if in_transaction:
            self._conn.execute("COMMIT")

    @contextmanager
    def lock(self, timeout: float = 30.0) -> Iterator[None]:
        """SQLite allows one writer, so an immediate transaction is the lock."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() > deadline:
                    raise LockUnavailable(
                        f"could not lock {self.path} within {timeout:g}s: {error}"
                    ) from error
                time.sleep(0.1)
        try:
            # Released immediately: each migration manages its own transaction,
            # and holding this one would nest them.
            self._conn.execute("COMMIT")
            yield
        finally:
            pass


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

    def run(self, sql: str, *, in_transaction: bool) -> None:
        if not in_transaction:
            self.connection.execute(sql)
            return
        with self.connection.transaction():
            self.connection.execute(sql)

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
