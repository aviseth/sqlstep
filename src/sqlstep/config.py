"""``[tool.sqlstep]`` in pyproject.toml, and the environment.

    [tool.sqlstep]
    directory = "migrations"
    url_env = "DATABASE_URL"
    table = "schema_migrations"

The database URL is read from the environment rather than the file, because a
connection string with a password in it does not belong in a repository.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[import-not-found]

from sqlstep.errors import SqlstepError

DEFAULT_DIRECTORY = "migrations"
DEFAULT_URL_ENV = "DATABASE_URL"
DEFAULT_TABLE = "schema_migrations"


class ConfigError(SqlstepError):
    """pyproject.toml has a ``[tool.sqlstep]`` section we cannot use."""


@dataclass
class Config:
    directory: Path = Path(DEFAULT_DIRECTORY)
    url_env: str = DEFAULT_URL_ENV
    table: str = DEFAULT_TABLE
    source: Path | None = None

    def url(self, override: str | None = None) -> str:
        if override:
            return override
        value = os.environ.get(self.url_env)
        if not value:
            raise ConfigError(
                f"no database URL. Set {self.url_env}, or pass --url. "
                "For example: sqlite:///app.db or postgres://user:pass@host/db"
            )
        return value


def find_pyproject(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None) -> Config:
    pyproject = path or find_pyproject()
    if pyproject is None:
        return Config()
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    section = data.get("tool", {}).get("sqlstep")
    if not isinstance(section, dict):
        return Config(source=pyproject)

    def text(key: str, default: str) -> str:
        value = section.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{pyproject}: {key} must be a non-empty string")
        return value

    table = text("table", DEFAULT_TABLE)
    if not table.replace("_", "").isalnum():
        # The table name goes into SQL unparameterised, because identifiers
        # cannot be bound. Restricting it is what makes that safe.
        raise ConfigError(
            f"{pyproject}: table must be letters, digits and underscores, not {table!r}"
        )

    return Config(
        directory=pyproject.parent / text("directory", DEFAULT_DIRECTORY),
        url_env=text("url_env", DEFAULT_URL_ENV),
        table=table,
        source=pyproject,
    )
