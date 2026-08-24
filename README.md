# sqlstep

Plain SQL database migrations, without an ORM.

[![PyPI](https://img.shields.io/pypi/v/sqlstep.svg)](https://pypi.org/project/sqlstep/)
[![Python](https://img.shields.io/pypi/pyversions/sqlstep.svg)](https://pypi.org/project/sqlstep/)
[![CI](https://github.com/aviseth/sqlstep/actions/workflows/ci.yml/badge.svg)](https://github.com/aviseth/sqlstep/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

If you are on raw psycopg, asyncpg or sqlite3, Alembic means adopting SQLAlchemy to get a
migration runner. The alternatives that did not are unmaintained: `yoyo-migrations` last released
in 2024, `migra` in 2022, `sqlbag` in 2021, and `dbmate` is a Go binary that is not on PyPI.

sqlstep runs `.sql` files, records what it ran, and refuses to do the things that quietly give
you a database nobody expected.

## Installation

```shell
pip install sqlstep                # SQLite
pip install 'sqlstep[postgres]'    # and PostgreSQL
```

Requires Python 3.10+.

## Quick start

```shell
export DATABASE_URL=postgres://user:pass@localhost/app
sqlstep new create_widgets
```

```sql
-- migrations/20260824093000_create_widgets.sql

-- migrate:up
create table widget (
  id bigserial primary key,
  name text not null
);

-- migrate:down
drop table widget;
```

```text
$ sqlstep status
state    version         name
-------  --------------  --------------
pending  20260824093000  create_widgets

$ sqlstep up
applied 20260824093000_create_widgets (4 ms)
```

One file per migration rather than two, because an up and its down belong together and a pair of
files is a pair that can drift. The down section is optional; a migration without one cannot be
rolled back, and `sqlstep down` says so rather than doing something creative.

## What it refuses to do

Three situations produce a database that does not match anybody's expectation, and none of them
announce themselves. sqlstep checks for all three before it runs anything.

**A migration edited after it ran.** The database was built by the old text, so the schema on disk
and the schema in the file have diverged. Every command checks the recorded checksum.

```text
$ sqlstep up
sqlstep: 0001_create_widgets has changed since it was applied.
  recorded: sha256:4b5c37bb9786404b34c23e1424228961
  on disk:  sha256:1d471d4327f8af7c75a13adf4f2c9492
The database was built by the old version, so re-running this one would not produce the
schema anyone else has. Write a new migration instead, or use 'sqlstep verify --accept'
if you are certain the change is cosmetic.
```

**A migration that arrives out of order.** Two branches merge and the one written first has the
lower version number. Applying it now means your database ran the migrations in an order nobody
tested. Refused unless you pass `--allow-out-of-order`.

**A row with no file.** The database says it ran something that is not in the directory, usually
an older checkout or a deleted migration. Reported by `status`, and `down` refuses while it is
true, because rolling back without those files leaves a schema this directory cannot describe.

## Locking

Two deploys landing together must not run the same migration twice.

On PostgreSQL, sqlstep takes a session advisory lock. It is released when the connection drops,
so a migrator that is killed halfway does not leave the lock held forever. The pending list is
re-read after the lock is granted, since the other process may have applied some of it while this
one was waiting.

On SQLite there is one writer at a time by design, and an immediate transaction is the lock.

There is a test that starts two migrators simultaneously against the same Postgres database and
asserts exactly one applies the migration and neither errors. It found a real bug: `CREATE TABLE
IF NOT EXISTS` is not race-safe in PostgreSQL, and two processes creating the bookkeeping table at
the same moment can still collide on the system catalogue.

## Transactions

PostgreSQL and SQLite both allow DDL inside a transaction, so a migration that fails halfway is
rolled back whole rather than leaving you with the first two statements applied. That is not true
of MySQL, which is why it is not supported yet rather than half-supported.

Some statements cannot run inside a transaction at all:

```sql
-- sqlstep:no-transaction
-- migrate:up
create index concurrently widget_name on widget (name);
```

Without that directive the statement fails, which is Postgres telling you the truth. With it,
the migration runs outside a transaction and a failure halfway is yours to clean up.

## Statement splitting

Splitting a script on semicolons is the obvious approach and it is wrong. sqlstep tracks what it
is inside, so semicolons in these places are not treated as boundaries:

| Construct | Example |
| --- | --- |
| String literals | `insert into a values ('x; y')` |
| Doubled quotes | `'it''s; fine'` |
| Quoted identifiers | `select "weird;name"` |
| Line and block comments | `-- drop table a;` |
| Trigger and function bodies | `create trigger t ... begin ...; end;` |
| Dollar quoting | `$$ ... ; ... $$`, `$tag$ ... $tag$` |

A bare `BEGIN` at the top of a migration is treated as somebody starting a transaction by hand,
not as opening a block, because treating it as a block would swallow the rest of the file.

## Adopting it on an existing database

```shell
sqlstep baseline 20260101000000
```

Marks everything up to that version as applied without running any of it, for a database that
already has the schema because it predates sqlstep or came from a dump.

## Configuration

Keys under `[tool.sqlstep]` in `pyproject.toml`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `directory` | string | `"migrations"` | Where the `.sql` files live |
| `url_env` | string | `"DATABASE_URL"` | Environment variable holding the connection string |
| `table` | string | `"schema_migrations"` | Bookkeeping table name |

The database URL is read from the environment, not from the file, because a connection string
with a password in it does not belong in a repository. `--url` overrides it.

The table name is validated as letters, digits and underscores. It goes into SQL unparameterised,
because identifiers cannot be bound, and restricting it is what makes that safe.

## Command reference

| Command | What it does |
| --- | --- |
| `sqlstep new <name>` | Write an empty migration with a timestamp version |
| `sqlstep status` | Applied, pending, and anything wrong. Non-zero if something is wrong |
| `sqlstep up` | Apply pending migrations, `--to` to stop at a version |
| `sqlstep up --dry-run` | Print the exact SQL that would run |
| `sqlstep down --steps N` | Roll back, `--to` to roll back to a version |
| `sqlstep verify` | Check applied migrations against their files |
| `sqlstep verify --accept` | Rewrite recorded checksums, for cosmetic edits |
| `sqlstep baseline <version>` | Mark as applied without running |

Every command takes `--json`, `--url` and `--dir`.

## Migration file format

| Marker | Meaning |
| --- | --- |
| `-- migrate:up` | Required. Everything until the next marker is the migration |
| `-- migrate:down` | Optional. Without it the migration cannot be rolled back |
| `-- sqlstep:no-transaction` | Run this migration outside a transaction |

Filenames are a number, an underscore, then a name: `0001_create_widgets.sql`. `sqlstep new` uses
a UTC timestamp, which does not collide when two people write a migration on the same day.
Versions sort numerically, so 10 comes after 9 rather than after 1.

## How it compares

| Tool | No ORM required | Checksums | Locking | Out-of-order guard | Dry run | Maintained |
| --- | --- | --- | --- | --- | --- | --- |
| Alembic | no | no | no | yes | yes | yes |
| `yoyo-migrations` | yes | no | yes | no | no | last release 2024 |
| `migra` | yes | n/a | n/a | n/a | n/a | last release 2022 |
| sqlstep | yes | yes | yes | yes | yes | yes |

Alembic is a good tool and autogenerate is genuinely useful. This is for people who are not using
SQLAlchemy and do not want to adopt it to get a migration runner.

## Notes

PostgreSQL and SQLite only. MySQL has no transactional DDL, so a failed migration leaves the
database half-changed, and supporting it properly means a different set of promises rather than
the same ones with a footnote.

Checksums are insensitive to trailing whitespace and nothing else. A checksum that forgives
comments or reformatting cannot tell reformatting from a changed `WHERE` clause.

`sqlstep down` runs the down sections in reverse order and does not attempt to be clever about
data. A down section that drops a column drops the data in it.

There is no autogenerate. Working out the difference between two schemas is a different and much
larger tool, and getting it subtly wrong is worse than not having it.

## Contributing

Bug reports and pull requests are welcome. `uv sync` then `uv run pytest`. The PostgreSQL tests
need a server: `SQLSTEP_TEST_DSN=postgres://... uv run pytest`.

## License

MIT.
