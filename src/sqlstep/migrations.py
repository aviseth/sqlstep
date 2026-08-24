"""Reading migration files off disk.

A migration is one ``.sql`` file with two sections::

    -- migrate:up
    create table widget (id integer primary key);

    -- migrate:down
    drop table widget;

One file rather than two, because an up and its down belong together and a pair
of files is a pair that can drift. The down section is optional; a migration
without one simply cannot be rolled back, and ``sqlstep down`` says so rather
than doing something creative.

Two directives are understood:

``-- sqlstep:no-transaction``
    Run this migration outside a transaction. Needed for statements Postgres
    refuses to run inside one, of which ``CREATE INDEX CONCURRENTLY`` is the one
    everybody hits.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlstep.errors import MigrationError

UP = re.compile(r"^\s*--\s*migrate:up\b(.*)$", re.IGNORECASE)
DOWN = re.compile(r"^\s*--\s*migrate:down\b(.*)$", re.IGNORECASE)
#: Anchored to its own comment line. Matching anywhere would let a SQL string
#: literal containing the text turn off the transaction for the migration.
NO_TRANSACTION = re.compile(
    r"^[ \t]*--[ \t]*sqlstep:no-transaction[ \t]*$", re.IGNORECASE | re.MULTILINE
)
FILENAME = re.compile(r"^(?P<version>\d+)[_-](?P<name>.+)\.sql$")


@dataclass
class Migration:
    """One migration file."""

    version: str
    name: str
    path: Path
    up: str
    down: str | None
    no_transaction: bool = False
    checksum: str = ""

    @property
    def label(self) -> str:
        return f"{self.version}_{self.name}"

    @property
    def reversible(self) -> bool:
        return bool(self.down and self.down.strip())


def checksum(text: str) -> str:
    """Hash of a migration's SQL, insensitive to trailing whitespace only.

    Deliberately not insensitive to comments or formatting. A checksum that
    forgives reformatting cannot tell reformatting from a changed WHERE clause.
    """
    normalised = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return "sha256:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def parse(text: str, path: Path) -> tuple[str, str | None, bool]:
    """Split a migration file into its up and down sections."""
    up_lines: list[str] = []
    down_lines: list[str] = []
    section: str | None = None
    for line in text.splitlines():
        if UP.match(line):
            section = "up"
            continue
        if DOWN.match(line):
            section = "down"
            continue
        if section == "up":
            up_lines.append(line)
        elif section == "down":
            down_lines.append(line)

    if section is None:
        raise MigrationError(
            f"{path} has no '-- migrate:up' line, so there is nothing to run. "
            "Every migration needs one, even if the down section is left out."
        )
    up = "\n".join(up_lines).strip()
    if not up:
        raise MigrationError(
            f"{path} has an empty '-- migrate:up' section. If this is a migration "
            "that was just created, write the SQL into it. An empty migration would "
            "be recorded as applied while doing nothing."
        )
    down = "\n".join(down_lines).strip() or None
    return up, down, bool(NO_TRANSACTION.search(text))


def load(path: Path) -> Migration:
    """Read one migration file."""
    match = FILENAME.match(path.name)
    if match is None:
        raise MigrationError(
            f"{path.name} is not a migration filename. Expected something like "
            "'0001_create_widgets.sql', a number then an underscore then a name."
        )
    text = path.read_text(encoding="utf-8")
    up, down, no_transaction = parse(text, path)
    return Migration(
        version=match.group("version"),
        name=match.group("name"),
        path=path,
        up=up,
        down=down,
        no_transaction=no_transaction,
        checksum=checksum(text),
    )


def discover(directory: Path) -> list[Migration]:
    """Every migration in ``directory``, in version order."""
    if not directory.is_dir():
        raise MigrationError(f"{directory} is not a directory")
    found = []
    for path in sorted(directory.glob("*.sql")):
        found.append(load(path))

    # Keyed by numeric value, so 0002 and 2 are recognised as the same version
    # rather than silently becoming two migrations that sort unpredictably.
    seen: dict[int, Path] = {}
    for migration in found:
        number = version_order(migration.version)[0]
        if number in seen:
            raise MigrationError(
                f"two migrations share version {number}: "
                f"{seen[number].name} and {migration.path.name}. "
                "Applying them in a stable order across machines would be luck."
            )
        seen[number] = migration.path
    found.sort(key=lambda m: version_order(m.version))
    return found


def version_order(version: str) -> tuple[int, str]:
    """Sort key by numeric value, so 10 comes after 9 and 0002 equals 2.

    Sorting by digit count would put ``10_a.sql`` before ``0002_b.sql`` on a
    fresh database, which is a migration order nobody chose.
    """
    try:
        return (int(version), version)
    except ValueError:  # pragma: no cover - FILENAME only matches digits
        return (0, version)


def next_version() -> str:
    """A UTC timestamp, which does not collide when two people write one on the same day."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")


TEMPLATE = """\
-- migrate:up


-- migrate:down

"""


def create(directory: Path, name: str) -> Path:
    """Write a new empty migration and return its path.

    Created exclusively, and the version is bumped on collision. The timestamp
    has one-second resolution, so two ``sqlstep new`` calls in the same second
    would otherwise pick the same path and the second would overwrite the first.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not slug:
        raise MigrationError(f"{name!r} does not give a usable filename")
    directory.mkdir(parents=True, exist_ok=True)

    taken = {version_order(m.version)[0] for m in discover_safe(directory)}
    number = int(next_version())
    while True:
        if number not in taken:
            path = directory / f"{number}_{slug}.sql"
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(TEMPLATE)
            except FileExistsError:
                number += 1
                continue
            return path
        number += 1


def discover_safe(directory: Path) -> list[Migration]:
    try:
        return discover(directory)
    except MigrationError:
        return []
