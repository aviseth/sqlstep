"""Exceptions, few enough to catch individually."""

from __future__ import annotations


class SqlstepError(Exception):
    """Base class for everything raised here."""


class MigrationError(SqlstepError):
    """A migration file is malformed, or the directory does not make sense."""


class ChecksumMismatch(SqlstepError):
    """A migration was edited after it had already been applied."""

    def __init__(self, version: str, name: str, recorded: str, current: str) -> None:
        super().__init__(
            f"{version}_{name} has changed since it was applied.\n"
            f"  recorded: {recorded}\n"
            f"  on disk:  {current}\n"
            "The database was built by the old version, so re-running this one would not "
            "produce the schema anyone else has. Write a new migration instead, or use "
            "'sqlstep verify --accept' if you are certain the change is cosmetic."
        )
        self.version = version
        self.name = name
        self.recorded = recorded
        self.current = current


class MigrationFailed(SqlstepError):
    """A migration raised while running."""

    def __init__(self, version: str, name: str, cause: BaseException) -> None:
        super().__init__(f"{version}_{name} failed: {cause}")
        self.version = version
        self.name = name
        self.cause = cause


class LockUnavailable(SqlstepError):
    """Another process is holding the migration lock."""


class UnsupportedDatabase(SqlstepError):
    """The URL does not name a database we can talk to."""


class MissingDriver(SqlstepError):
    """The driver for this database is not installed."""
