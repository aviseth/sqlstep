"""Shared fixtures. Postgres tests are skipped unless SQLSTEP_TEST_DSN is set."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

POSTGRES_DSN = os.environ.get("SQLSTEP_TEST_DSN")

needs_postgres = pytest.mark.skipif(
    not POSTGRES_DSN, reason="set SQLSTEP_TEST_DSN to run the PostgreSQL tests"
)


def write(directory: Path, name: str, up: str, down: str | None = None, header: str = "") -> Path:
    body = f"{header}-- migrate:up\n{up}\n"
    if down is not None:
        body += f"\n-- migrate:down\n{down}\n"
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'app.db'}"


@pytest.fixture
def postgres_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """A private schema per test, dropped afterwards.

    Deliberately not `DROP SCHEMA public CASCADE`: somebody will eventually point
    SQLSTEP_TEST_DSN at a database that matters, and a test suite is not entitled
    to delete it. A per-test schema also means these can run in parallel.
    """
    if not POSTGRES_DSN:
        pytest.skip("no SQLSTEP_TEST_DSN")
    import psycopg

    name = "sqlstep_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    try:
        yield f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{name}"
    finally:
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
