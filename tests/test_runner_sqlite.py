"""The runner, against a real SQLite database."""

import pytest

from conftest import write
from sqlstep.drivers import open_driver
from sqlstep.errors import ChecksumMismatch, MigrationFailed, SqlstepError
from sqlstep.migrations import discover
from sqlstep.runner import accept_checksums, baseline, down, status, up


@pytest.fixture
def project(migrations_dir, sqlite_url):
    write(
        migrations_dir,
        "0001_widgets.sql",
        "create table widget (id integer primary key);",
        "drop table widget;",
    )
    write(
        migrations_dir,
        "0002_price.sql",
        "alter table widget add column price integer;",
        "alter table widget drop column price;",
    )
    return migrations_dir, sqlite_url


def tables(url):
    with open_driver(url) as driver:
        rows = driver._conn.execute(
            "select name from sqlite_master where type='table' order by name"
        ).fetchall()
    return [r[0] for r in rows]


def test_everything_starts_pending(project):
    directory, url = project
    with open_driver(url) as driver:
        state = status(driver, discover(directory))
    assert [m.version for m in state.pending] == ["0001", "0002"]
    assert state.applied == []
    assert state.clean


def test_up_applies_in_order(project):
    directory, url = project
    with open_driver(url) as driver:
        done = up(driver, discover(directory))
    assert [m.version for m in done] == ["0001", "0002"]
    assert "widget" in tables(url)


def test_up_is_idempotent(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    with open_driver(url) as driver:
        assert up(driver, discover(directory)) == []


def test_a_dry_run_changes_nothing(project):
    directory, url = project
    with open_driver(url) as driver:
        planned = up(driver, discover(directory), dry_run=True)
    assert [m.version for m in planned] == ["0001", "0002"]
    assert "widget" not in tables(url)


def test_up_to_a_target_stops_there(project):
    directory, url = project
    with open_driver(url) as driver:
        done = up(driver, discover(directory), target="0001")
    assert [m.version for m in done] == ["0001"]


def test_down_rolls_back_the_most_recent(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    with open_driver(url) as driver:
        done = down(driver, discover(directory), steps=1)
    assert [m.version for m in done] == ["0002"]
    with open_driver(url) as driver:
        assert [m.version for m in status(driver, discover(directory)).pending] == ["0002"]


def test_down_removes_the_table_when_it_goes_all_the_way(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    with open_driver(url) as driver:
        down(driver, discover(directory), steps=2)
    assert "widget" not in tables(url)


def test_down_refuses_a_migration_with_no_down_section(migrations_dir, sqlite_url):
    write(migrations_dir, "0001_a.sql", "create table a (i integer);")
    with open_driver(sqlite_url) as driver:
        up(driver, discover(migrations_dir))
    with (
        open_driver(sqlite_url) as driver,
        pytest.raises(SqlstepError, match="no '-- migrate:down' section"),
    ):
        down(driver, discover(migrations_dir))


def test_a_migration_edited_after_it_ran_is_refused(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    path = directory / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- a change\n")

    with (
        open_driver(url) as driver,
        pytest.raises(ChecksumMismatch, match="has changed since it was applied"),
    ):
        up(driver, discover(directory))


def test_accepting_a_checksum_lets_it_run_again(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory), target="0001")
    path = directory / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- cosmetic\n")

    with open_driver(url) as driver:
        assert len(accept_checksums(driver, discover(directory))) == 1
    with open_driver(url) as driver:
        assert status(driver, discover(directory)).mismatched == []


def test_a_migration_older_than_one_already_applied_is_refused(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    write(migrations_dir_of(directory), "0000_earlier.sql", "create table earlier (i integer);")

    with (
        open_driver(url) as driver,
        pytest.raises(SqlstepError, match="older than one already applied"),
    ):
        up(driver, discover(directory))


def test_an_out_of_order_migration_can_be_allowed_on_purpose(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    write(migrations_dir_of(directory), "0000_earlier.sql", "create table earlier (i integer);")

    with open_driver(url) as driver:
        done = up(driver, discover(directory), allow_out_of_order=True)
    assert [m.version for m in done] == ["0000"]


def migrations_dir_of(directory):
    return directory


def test_a_failing_migration_says_which_one(migrations_dir, sqlite_url):
    write(migrations_dir, "0001_bad.sql", "this is not sql;")
    with open_driver(sqlite_url) as driver, pytest.raises(MigrationFailed, match="0001_bad failed"):
        up(driver, discover(migrations_dir))


def test_a_failing_migration_is_rolled_back_whole(migrations_dir, sqlite_url):
    """The first statement must not survive the second one failing."""
    write(
        migrations_dir,
        "0001_partial.sql",
        "create table good (i integer);\nthis is not sql;",
    )
    with open_driver(sqlite_url) as driver, pytest.raises(MigrationFailed):
        up(driver, discover(migrations_dir))
    assert "good" not in tables(sqlite_url)


def test_a_failed_migration_is_not_recorded_as_applied(migrations_dir, sqlite_url):
    write(migrations_dir, "0001_bad.sql", "this is not sql;")
    with open_driver(sqlite_url) as driver, pytest.raises(MigrationFailed):
        up(driver, discover(migrations_dir))
    with open_driver(sqlite_url) as driver:
        assert status(driver, discover(migrations_dir)).applied == []


def test_baseline_marks_without_running(project):
    directory, url = project
    with open_driver(url) as driver:
        marked = baseline(driver, discover(directory), "0002")
    assert [m.version for m in marked] == ["0001", "0002"]
    assert "widget" not in tables(url), "baseline must not run any SQL"
    with open_driver(url) as driver:
        assert status(driver, discover(directory)).pending == []


def test_a_row_with_no_file_is_reported_as_orphaned(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    (directory / "0002_price.sql").unlink()

    with open_driver(url) as driver:
        state = status(driver, discover(directory))
    assert [r.version for r in state.orphaned] == ["0002"]
    assert not state.clean


def test_down_refuses_while_a_row_has_no_file(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory))
    (directory / "0002_price.sql").unlink()

    with open_driver(url) as driver, pytest.raises(SqlstepError, match="no file here"):
        down(driver, discover(directory))


def test_the_recorded_time_is_the_time_the_runner_measured(project):
    """Asserting >= 0 would pass even if record always stored zero."""
    directory, url = project
    seen = {}
    with open_driver(url) as driver:
        up(driver, discover(directory), on_step=lambda m, ms: seen.__setitem__(m.version, ms))
    with open_driver(url) as driver:
        recorded = {r.version: r.execution_ms for r in driver.applied()}
    assert recorded == seen


def test_the_bookkeeping_row_lands_with_the_migration_not_after_it(project):
    """A durable schema change with no row makes the next run re-apply and fail."""
    directory, url = project
    migrations = discover(directory)
    with open_driver(url) as driver:
        driver.ensure_table()
        with pytest.raises(MigrationFailed):
            # The second statement fails, so neither the table nor its row
            # should survive.
            broken = migrations[0]
            broken.up = "create table widget (id integer primary key);\nthis is not sql;"
            up(driver, [broken])
    assert "widget" not in tables(url)
    with open_driver(url) as driver:
        assert driver.applied() == []


def test_two_processes_migrating_the_same_sqlite_file_apply_it_once(project, tmp_path):
    import subprocess
    import sys
    import textwrap

    directory, url = project
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from sqlstep.drivers import open_driver
        from sqlstep.migrations import discover
        from sqlstep.runner import up

        with open_driver({url!r}) as driver:
            print(len(up(driver, discover(Path({str(directory)!r})))))
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    counts = sorted(int(p.communicate()[0].strip() or -1) for p in procs)
    assert all(p.returncode == 0 for p in procs)
    assert sum(counts) == 2, "each migration should be applied exactly once in total"
    with open_driver(url) as driver:
        assert len(driver.applied()) == 2


def test_accepting_a_checksum_keeps_when_it_was_applied(project):
    directory, url = project
    with open_driver(url) as driver:
        up(driver, discover(directory), target="0001")
    with open_driver(url) as driver:
        before = {r.version: (r.applied_at, r.execution_ms) for r in driver.applied()}

    path = directory / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- cosmetic\n")
    with open_driver(url) as driver:
        accept_checksums(driver, discover(directory))
    with open_driver(url) as driver:
        after = {r.version: (r.applied_at, r.execution_ms) for r in driver.applied()}
    assert before == after
