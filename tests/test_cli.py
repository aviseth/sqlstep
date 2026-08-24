import json

import pytest

from conftest import write
from sqlstep.cli import main


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n\n[tool.sqlstep]\ndirectory = "migrations"\n'
    )
    directory = tmp_path / "migrations"
    directory.mkdir()
    write(
        directory,
        "0001_widgets.sql",
        "create table widget (id integer primary key);",
        "drop table widget;",
    )
    write(
        directory,
        "0002_price.sql",
        "alter table widget add column price integer;",
        "alter table widget drop column price;",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return tmp_path


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "sqlstep" in capsys.readouterr().out


def test_new_writes_a_template(project, capsys):
    assert main(["new", "add widgets"]) == 0
    out = capsys.readouterr().out
    assert "add_widgets.sql" in out
    created = list((project / "migrations").glob("*add_widgets.sql"))
    assert "-- migrate:up" in created[0].read_text()


def test_status_lists_pending(project, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending" in out
    assert "widgets" in out


def test_dry_run_prints_the_sql_and_changes_nothing(project, capsys):
    assert main(["up", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would apply 2 migration(s)" in out
    assert "create table widget" in out
    assert main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == []


def test_up_then_status(project, capsys):
    assert main(["up"]) == 0
    assert "applied 0001_widgets" in capsys.readouterr().out
    assert main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == ["0001", "0002"]


def test_up_twice_does_nothing_the_second_time(project, capsys):
    assert main(["up"]) == 0
    capsys.readouterr()
    assert main(["up"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_down_rolls_one_back(project, capsys):
    assert main(["up"]) == 0
    capsys.readouterr()
    assert main(["down", "--steps", "1"]) == 0
    assert "rolled back 0002_price" in capsys.readouterr().out


def test_verify_passes_then_fails_after_an_edit(project, capsys):
    assert main(["up"]) == 0
    capsys.readouterr()
    assert main(["verify"]) == 0
    assert "match their files" in capsys.readouterr().out

    path = project / "migrations" / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- changed\n")
    assert main(["verify"]) == 1
    assert "changed after being applied" in capsys.readouterr().out


def test_verify_accept_clears_it(project, capsys):
    assert main(["up"]) == 0
    path = project / "migrations" / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- changed\n")
    capsys.readouterr()
    assert main(["verify", "--accept"]) == 0
    assert main(["verify"]) == 0


def test_baseline_marks_without_running(project, capsys):
    assert main(["baseline", "0002"]) == 0
    assert "without running them" in capsys.readouterr().out
    assert main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == []


def test_an_unsupported_url_is_reported_not_raised(project, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://u@h/d")
    assert main(["status"]) == 1
    assert "does not name a database" in capsys.readouterr().err


def test_a_missing_url_is_reported(project, capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL")
    assert main(["status"]) == 1
    assert "no database URL" in capsys.readouterr().err


def test_a_failing_migration_names_itself(project, capsys):
    write(project / "migrations", "0003_bad.sql", "this is not sql;")
    assert main(["up"]) == 1
    assert "0003_bad failed" in capsys.readouterr().err


def test_status_exits_non_zero_when_something_is_wrong(project, capsys):
    assert main(["up"]) == 0
    path = project / "migrations" / "0001_widgets.sql"
    path.write_text(path.read_text() + "\n-- changed\n")
    capsys.readouterr()
    assert main(["status"]) == 1
    assert "edited after being applied" in capsys.readouterr().out


def test_up_refuses_an_out_of_order_migration(project, capsys):
    assert main(["up"]) == 0
    write(project / "migrations", "0000_earlier.sql", "create table earlier (i integer);")
    capsys.readouterr()
    assert main(["up"]) == 1
    assert "older than one already applied" in capsys.readouterr().err


def test_out_of_order_can_be_allowed(project, capsys):
    assert main(["up"]) == 0
    write(project / "migrations", "0000_earlier.sql", "create table earlier (i integer);")
    capsys.readouterr()
    assert main(["up", "--allow-out-of-order"]) == 0
    assert "applied 0000_earlier" in capsys.readouterr().out
