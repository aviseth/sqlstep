import pytest

from sqlstep.drivers import PostgresDriver, SQLiteDriver, open_driver
from sqlstep.errors import UnsupportedDatabase


@pytest.mark.parametrize(
    "url",
    ["sqlite:///tmp/x.db", "app.db", "data.sqlite", "data.sqlite3", ":memory:"],
)
def test_sqlite_urls(url):
    assert isinstance(open_driver(url), SQLiteDriver)


@pytest.mark.parametrize("url", ["postgres://u@h/d", "postgresql://u@h/d", "POSTGRES://u@h/d"])
def test_postgres_urls(url):
    assert isinstance(open_driver(url), PostgresDriver)


@pytest.mark.parametrize("url", ["mysql://u@h/d", "redis://h", "nonsense", ""])
def test_anything_else_is_refused_with_a_list_of_what_works(url):
    with pytest.raises(UnsupportedDatabase, match="Supported"):
        open_driver(url)


def test_the_table_name_is_carried_through():
    assert open_driver("app.db", "my_migrations").table == "my_migrations"


def test_sqlite_creates_the_parent_directory(tmp_path):
    url = f"sqlite:///{tmp_path / 'nested' / 'deep' / 'app.db'}"
    with open_driver(url) as driver:
        driver.ensure_table()
    assert (tmp_path / "nested" / "deep" / "app.db").is_file()


def test_a_second_connection_sees_what_the_first_recorded(tmp_path):
    from conftest import write as write_migration
    from sqlstep.migrations import discover
    from sqlstep.runner import up

    directory = tmp_path / "migrations"
    directory.mkdir()
    write_migration(directory, "0001_a.sql", "create table a (i integer);")
    url = f"sqlite:///{tmp_path / 'app.db'}"

    with open_driver(url) as driver:
        up(driver, discover(directory))
    with open_driver(url) as driver:
        driver.ensure_table()
        assert [r.version for r in driver.applied()] == ["0001"]
