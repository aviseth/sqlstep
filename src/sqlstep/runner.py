"""Deciding what to run, and running it.

Three hazards get explicit handling here, because each one produces a database
that does not match anybody's expectation and none of them announce themselves.

*A migration edited after it ran.* The database was built by the old text, so the
schema on disk and the schema in the file have quietly diverged. Every command
checks, and refuses rather than carrying on.

*A migration that arrives out of order.* Two branches merge, and the one written
first has the later version number. Applying it after the newer one means your
database ran the migrations in an order nobody tested. Refused by default.

*A row with no file.* The database says it ran a migration that is not in the
directory, which usually means a rollback that deleted the file or a checkout of
an older branch.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlstep.drivers import Applied, Driver
from sqlstep.errors import ChecksumMismatch, MigrationFailed, SqlstepError
from sqlstep.migrations import Migration, version_order


@dataclass
class Status:
    """What the database and the directory say, and where they disagree."""

    applied: list[Applied] = field(default_factory=list)
    pending: list[Migration] = field(default_factory=list)
    mismatched: list[tuple[Migration, Applied]] = field(default_factory=list)
    orphaned: list[Applied] = field(default_factory=list)
    out_of_order: list[Migration] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.mismatched or self.orphaned or self.out_of_order)

    @property
    def current(self) -> str | None:
        return self.applied[-1].version if self.applied else None


def status(driver: Driver, migrations: Sequence[Migration]) -> Status:
    """Compare the migrations table against the directory."""
    driver.ensure_table()
    rows = driver.applied()
    by_version = {row.version: row for row in rows}
    on_disk = {m.version: m for m in migrations}

    result = Status(applied=rows)
    highest = max((r.version for r in rows), key=_order, default=None)

    for migration in migrations:
        row = by_version.get(migration.version)
        if row is None:
            result.pending.append(migration)
            if highest is not None and _order(migration.version) < _order(highest):
                result.out_of_order.append(migration)
        elif row.checksum != migration.checksum:
            result.mismatched.append((migration, row))

    result.orphaned = [row for row in rows if row.version not in on_disk]
    return result


#: Ordering lives in migrations.py so the runner and discovery cannot disagree.
_order = version_order


def up(
    driver: Driver,
    migrations: Sequence[Migration],
    *,
    target: str | None = None,
    dry_run: bool = False,
    allow_out_of_order: bool = False,
    lock_timeout: float = 30.0,
    on_step: Callable[[Migration, int], None] | None = None,
) -> list[Migration]:
    """Apply pending migrations up to ``target``, or all of them."""
    state = status(driver, migrations)
    _refuse_on_mismatch(state)
    if state.out_of_order and not allow_out_of_order:
        names = ", ".join(m.label for m in state.out_of_order)
        raise SqlstepError(
            f"these migrations are older than one already applied: {names}. "
            "Running them now means your database has seen an order nobody tested. "
            "Renumber them, or pass --allow-out-of-order if you know it is safe."
        )

    chosen = list(state.pending)
    if target is not None:
        chosen = [m for m in chosen if _order(m.version) <= _order(target)]
    if dry_run or not chosen:
        return chosen

    done: list[Migration] = []
    with driver.lock(lock_timeout):
        for migration in chosen:
            applied = _apply_one(driver, migration, migration.up, forward=True)
            if applied is None:
                continue
            done.append(migration)
            if on_step is not None:
                on_step(migration, applied)
    return done


def _apply_one(driver: Driver, migration: Migration, sql: str, *, forward: bool) -> int | None:
    """Run one migration and its bookkeeping in a single transaction.

    Both halves together, because a schema change that is durable while its row
    is missing means the next run tries to apply it again and fails on a table
    that already exists. Doing them in one transaction also lets the
    already-applied check happen inside it, which is what stops two concurrent
    migrators from both deciding they should run the same migration.

    A migration marked no-transaction cannot have this. Its bookkeeping is
    written straight after instead, and the gap between them is the price of
    running statements the database will not put in a transaction.
    """
    with driver.atomic(not migration.no_transaction):
        if forward == driver.is_applied(migration.version):
            return None
        try:
            elapsed = driver.apply(migration, sql)
        except Exception as error:
            raise MigrationFailed(migration.version, migration.name, error) from error
        if forward:
            driver.record(migration, elapsed)
        else:
            driver.forget(migration.version)
        return elapsed


def down(
    driver: Driver,
    migrations: Sequence[Migration],
    *,
    steps: int = 1,
    target: str | None = None,
    dry_run: bool = False,
    lock_timeout: float = 30.0,
    on_step: Callable[[Migration, int], None] | None = None,
) -> list[Migration]:
    """Roll back the most recent migrations, newest first."""
    state = status(driver, migrations)
    _refuse_on_mismatch(state)
    if state.orphaned:
        names = ", ".join(f"{r.version}_{r.name}" for r in state.orphaned)
        raise SqlstepError(
            f"the database has applied migrations with no file here: {names}. "
            "Rolling back without them would leave the schema in a state this "
            "directory cannot describe."
        )

    by_version = {m.version: m for m in migrations}
    applied = sorted(state.applied, key=lambda r: _order(r.version), reverse=True)
    chosen: list[Migration] = []
    for row in applied:
        if target is not None and _order(row.version) <= _order(target):
            break
        migration = by_version.get(row.version)
        if migration is None:  # pragma: no cover - guarded above
            break
        chosen.append(migration)
        if target is None and len(chosen) >= steps:
            break

    irreversible = [m for m in chosen if not m.reversible]
    if irreversible:
        names = ", ".join(m.label for m in irreversible)
        raise SqlstepError(
            f"these migrations have no '-- migrate:down' section: {names}. "
            "There is nothing to run, so the rollback would silently do less than "
            "you asked for."
        )
    if dry_run or not chosen:
        return chosen

    done: list[Migration] = []
    with driver.lock(lock_timeout):
        # Re-read inside the lock. Another process may have rolled some of these
        # back between the status call above and the lock being granted, and
        # running a down section twice is not something to find out about later.
        current = {row.version for row in driver.applied()}
        for migration in chosen:
            if migration.version not in current:
                continue
            assert migration.down is not None
            elapsed = _apply_one(driver, migration, migration.down, forward=False)
            if elapsed is None:
                continue
            done.append(migration)
            if on_step is not None:
                on_step(migration, elapsed)
    return done


def baseline(driver: Driver, migrations: Sequence[Migration], version: str) -> list[Migration]:
    """Mark everything up to ``version`` as applied, without running any of it.

    For a database that already has the schema, typically because it predates
    sqlstep or was restored from a dump.
    """
    driver.ensure_table()
    existing = {row.version for row in driver.applied()}
    marked = []
    for migration in migrations:
        if _order(migration.version) <= _order(version) and migration.version not in existing:
            driver.record(migration, 0)
            marked.append(migration)
    return marked


def accept_checksums(driver: Driver, migrations: Sequence[Migration]) -> list[Migration]:
    """Rewrite recorded checksums to match the files. Only for cosmetic edits.

    Updated in place rather than deleted and re-inserted, so when the migration
    actually ran and how long it took survive. This command is for a cosmetic
    edit; rewriting the history alongside it would not be.
    """
    state = status(driver, migrations)
    for migration, _row in state.mismatched:
        driver.update_checksum(migration.version, migration.checksum)
    return [migration for migration, _ in state.mismatched]


def _refuse_on_mismatch(state: Status) -> None:
    if state.mismatched:
        migration, row = state.mismatched[0]
        raise ChecksumMismatch(migration.version, migration.name, row.checksum, migration.checksum)
