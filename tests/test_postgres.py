"""Against a real PostgreSQL server.

Skipped unless SQLSTEP_TEST_DSN is set. These are the tests that matter most,
because locking and transactional DDL are the two things a migration tool has to
get right and neither can be checked against SQLite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from conftest import needs_postgres, write
from sqlstep.drivers import open_driver
from sqlstep.errors import MigrationFailed
from sqlstep.migrations import discover
from sqlstep.runner import status, up

pytestmark = [pytest.mark.postgres, needs_postgres]


def tables(url):
    with open_driver(url) as driver:
        rows = driver.connection.execute(
            "select tablename from pg_tables where schemaname = 'public' order by tablename"
        ).fetchall()
    return [r[0] for r in rows]


def test_a_migration_applies(migrations_dir, postgres_url):
    write(
        migrations_dir,
        "0001_a.sql",
        "create table widget (id serial primary key);",
        "drop table widget;",
    )
    with open_driver(postgres_url) as driver:
        up(driver, discover(migrations_dir))
    assert "widget" in tables(postgres_url)


def test_a_function_body_full_of_semicolons_survives(migrations_dir, postgres_url):
    write(
        migrations_dir,
        "0001_fn.sql",
        "create function bump(n int) returns int as $$\n"
        "begin\n  n := n + 1;\n  return n;\nend;\n$$ language plpgsql;",
        "drop function bump(int);",
    )
    with open_driver(postgres_url) as driver:
        up(driver, discover(migrations_dir))
    with open_driver(postgres_url) as driver:
        assert driver.connection.execute("select bump(1)").fetchone()[0] == 2


def test_a_failing_migration_rolls_back_the_whole_thing(migrations_dir, postgres_url):
    write(
        migrations_dir,
        "0001_partial.sql",
        "create table good (id int);\nthis is not sql;",
    )
    with open_driver(postgres_url) as driver, pytest.raises(MigrationFailed):
        up(driver, discover(migrations_dir))
    assert "good" not in tables(postgres_url), "transactional DDL should have undone it"


def test_create_index_concurrently_needs_no_transaction(migrations_dir, postgres_url):
    """It is an error inside a transaction, which is what the directive is for."""
    write(migrations_dir, "0001_t.sql", "create table widget (id serial primary key, name text);")
    write(
        migrations_dir,
        "0002_i.sql",
        "create index concurrently widget_name on widget (name);",
        header="-- sqlstep:no-transaction\n",
    )
    with open_driver(postgres_url) as driver:
        up(driver, discover(migrations_dir))
    with open_driver(postgres_url) as driver:
        found = driver.connection.execute(
            "select indexname from pg_indexes where tablename = 'widget'"
        ).fetchall()
    assert "widget_name" in {r[0] for r in found}


def test_create_index_concurrently_fails_without_the_directive(migrations_dir, postgres_url):
    write(migrations_dir, "0001_t.sql", "create table widget (id serial primary key, name text);")
    write(migrations_dir, "0002_i.sql", "create index concurrently widget_name on widget (name);")
    with open_driver(postgres_url) as driver, pytest.raises(MigrationFailed):
        up(driver, discover(migrations_dir))


def test_two_migrators_racing_apply_each_migration_once(migrations_dir, postgres_url):
    """The advisory lock exists for exactly this: two deploys landing together."""
    write(
        migrations_dir,
        "0001_slow.sql",
        "create table widget (id serial primary key);\nselect pg_sleep(1);",
        "drop table widget;",
    )
    script = textwrap.dedent(
        f"""
        from pathlib import Path

        from sqlstep.drivers import open_driver
        from sqlstep.migrations import discover
        from sqlstep.runner import up

        with open_driver({postgres_url!r}) as driver:
            done = up(driver, discover(Path({str(migrations_dir)!r})), lock_timeout=60)
        print(len(done))
        """
    )
    first = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    outputs = [int(p.communicate()[0].strip() or -1) for p in (first, second)]

    assert first.returncode == 0 and second.returncode == 0, "neither should have errored"
    assert sorted(outputs) == [0, 1], "exactly one process should have applied it"
    with open_driver(postgres_url) as driver:
        assert len(driver.applied()) == 1


def test_the_applied_table_survives_a_reconnect(migrations_dir, postgres_url):
    write(migrations_dir, "0001_a.sql", "create table widget (id serial primary key);")
    with open_driver(postgres_url) as driver:
        up(driver, discover(migrations_dir))
    with open_driver(postgres_url) as driver:
        assert [r.version for r in status(driver, discover(migrations_dir)).applied] == ["0001"]
