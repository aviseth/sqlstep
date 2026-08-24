import pytest

from sqlstep.config import ConfigError, load


def write(tmp_path, body):
    path = tmp_path / "pyproject.toml"
    path.write_text(body)
    return path


def test_no_section_gives_defaults(tmp_path):
    config = load(write(tmp_path, '[project]\nname = "x"\n'))
    assert config.directory.name == "migrations"
    assert config.url_env == "DATABASE_URL"
    assert config.table == "schema_migrations"


def test_a_full_section(tmp_path):
    path = write(
        tmp_path,
        '[tool.sqlstep]\ndirectory = "db/migrate"\nurl_env = "PG_URL"\ntable = "migrations"\n',
    )
    config = load(path)
    assert config.directory == tmp_path / "db" / "migrate"
    assert config.url_env == "PG_URL"
    assert config.table == "migrations"


def test_the_url_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    assert load(write(tmp_path, '[project]\nname = "x"\n')).url() == "sqlite:///x.db"


def test_an_explicit_url_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    assert (
        load(write(tmp_path, '[project]\nname = "x"\n')).url("sqlite:///y.db") == "sqlite:///y.db"
    )


def test_a_missing_url_says_where_to_put_one(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="Set DATABASE_URL"):
        load(write(tmp_path, '[project]\nname = "x"\n')).url()


@pytest.mark.parametrize(
    "table", ["x; drop table users", "9migrations", "has-a-dash", "has space", ""]
)
def test_a_table_name_that_is_not_an_identifier_is_rejected(tmp_path, table):
    """The table name goes into SQL unparameterised, since identifiers cannot be bound."""
    with pytest.raises(ConfigError):
        load(write(tmp_path, f'[tool.sqlstep]\ntable = "{table}"\n'))


def test_a_leading_underscore_is_a_fine_table_name(tmp_path):
    assert load(write(tmp_path, '[tool.sqlstep]\ntable = "_migrations"\n')).table == "_migrations"


def test_an_empty_directory_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="non-empty string"):
        load(write(tmp_path, '[tool.sqlstep]\ndirectory = ""\n'))
