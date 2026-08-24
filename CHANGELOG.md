# Changelog

## 0.1.0

First release.

- Plain `.sql` migrations with `-- migrate:up` and `-- migrate:down` in one file.
- PostgreSQL and SQLite, each with real locking: a session advisory lock on PostgreSQL, released
  when the connection drops, and SQLite's single-writer transaction.
- Checksums recorded at apply time, so a migration edited afterwards is refused rather than
  quietly producing a schema nobody else has.
- Out-of-order migrations are refused by default, since two merged branches can otherwise apply in
  an order nobody tested.
- Applied rows with no matching file are reported, and `down` refuses while any exist.
- `-- sqlstep:no-transaction` for statements PostgreSQL will not run inside one, such as
  `CREATE INDEX CONCURRENTLY`.
- A statement splitter that is not fooled by semicolons in strings, comments, quoted identifiers,
  trigger bodies or dollar quoting.
- `--dry-run` prints the exact SQL, and `baseline` adopts a database that already has the schema.
