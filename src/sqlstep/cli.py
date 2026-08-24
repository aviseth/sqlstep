"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlstep import __version__

EPILOG = """\
examples:
  sqlstep new add_widgets
  sqlstep status
  sqlstep up --dry-run
  sqlstep up
  sqlstep down --steps 1
  sqlstep verify
  sqlstep baseline 20260101000000
"""


def _positive(value: str) -> int:
    """argparse type for a count that has to be at least one."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlstep",
        description="Plain SQL migrations, without an ORM.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sqlstep {__version__}")
    parser.add_argument("--url", help="database URL, overriding the environment")
    parser.add_argument("--dir", type=Path, dest="directory", help="migrations directory")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="write an empty migration file")
    new.add_argument("name")

    sub.add_parser("status", help="show what is applied and what is pending")

    up = sub.add_parser("up", help="apply pending migrations")
    up.add_argument("--to", dest="target", help="stop after this version")
    up.add_argument("--dry-run", action="store_true", help="show what would run")
    up.add_argument("--allow-out-of-order", action="store_true")
    up.add_argument("--lock-timeout", type=float, default=30.0)

    down = sub.add_parser("down", help="roll back the most recent migrations")
    down.add_argument("--steps", type=_positive, default=1)
    down.add_argument("--to", dest="target", help="roll back until this version is current")
    down.add_argument("--dry-run", action="store_true")
    down.add_argument("--lock-timeout", type=float, default=30.0)

    verify = sub.add_parser("verify", help="check applied migrations against the files")
    verify.add_argument("--accept", action="store_true", help="rewrite recorded checksums")

    baseline = sub.add_parser("baseline", help="mark migrations as applied without running them")
    baseline.add_argument("version")
    return parser


def main(argv: list[str] | None = None) -> int:
    from sqlstep.errors import SqlstepError

    args = build_parser().parse_args(argv)
    try:
        if args.command == "new":
            return _new(args)
        return _with_database(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except SqlstepError as error:
        print(f"sqlstep: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"sqlstep: {error}", file=sys.stderr)
        return 2
    return 2


def _config(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from sqlstep.config import load

    config = load(args.config)
    if args.directory:
        config.directory = args.directory
    return config


def _new(args: argparse.Namespace) -> int:
    from sqlstep.migrations import create

    path = create(_config(args).directory, args.name)
    if args.json:
        print(json.dumps({"created": str(path)}, indent=2))
        return 0
    print(f"wrote {path}")
    return 0


def _with_database(args: argparse.Namespace) -> int:
    from sqlstep.drivers import open_driver
    from sqlstep.migrations import discover

    config = _config(args)
    migrations = discover(config.directory)
    with open_driver(config.url(args.url), config.table) as driver:
        if args.command == "status":
            return _status(args, driver, migrations)
        if args.command == "up":
            return _up(args, driver, migrations)
        if args.command == "down":
            return _down(args, driver, migrations)
        if args.command == "verify":
            return _verify(args, driver, migrations)
        if args.command == "baseline":
            return _baseline(args, driver, migrations)
    return 2


def _status(args: argparse.Namespace, driver, migrations) -> int:  # type: ignore[no-untyped-def]
    from sqlstep.report import style, table
    from sqlstep.runner import status

    state = status(driver, migrations)
    if args.json:
        print(
            json.dumps(
                {
                    "current": state.current,
                    "applied": [r.version for r in state.applied],
                    "pending": [m.version for m in state.pending],
                    "mismatched": [m.label for m, _ in state.mismatched],
                    "orphaned": [f"{r.version}_{r.name}" for r in state.orphaned],
                    "out_of_order": [m.label for m in state.out_of_order],
                    "clean": state.clean,
                },
                indent=2,
            )
        )
        return 0 if state.clean else 1

    rows = [("applied", r.version, r.name) for r in state.applied]
    rows += [("pending", m.version, m.name) for m in state.pending]
    if rows:
        print(table(["state", "version", "name"], rows))
    else:
        print(f"no migrations in {(migrations and migrations[0].path.parent) or 'the directory'}")

    if state.mismatched:
        print()
        print(style("edited after being applied:", "red"))
        for migration, _row in state.mismatched:
            print(f"  {migration.label}  {migration.path}")
    if state.out_of_order:
        print()
        print(style("older than a migration already applied:", "yellow"))
        for migration in state.out_of_order:
            print(f"  {migration.label}")
    if state.orphaned:
        print()
        print(style("applied but not in this directory:", "yellow"))
        for row in state.orphaned:
            print(f"  {row.version}_{row.name}")
    return 0 if state.clean else 1


def _up(args: argparse.Namespace, driver, migrations) -> int:  # type: ignore[no-untyped-def]
    from sqlstep.report import style
    from sqlstep.runner import up

    def announce(migration, elapsed: int) -> None:  # type: ignore[no-untyped-def]
        print(f"{style('applied', 'green')} {migration.label} ({elapsed} ms)")

    done = up(
        driver,
        migrations,
        target=args.target,
        dry_run=args.dry_run,
        allow_out_of_order=args.allow_out_of_order,
        lock_timeout=args.lock_timeout,
        on_step=None if args.json else announce,
    )
    if args.json:
        print(json.dumps({"applied": [m.label for m in done], "dry_run": args.dry_run}, indent=2))
        return 0
    if not done:
        print("nothing to do, the database is up to date")
        return 0
    if args.dry_run:
        print(f"would apply {len(done)} migration(s):")
        for migration in done:
            print(f"  {migration.label}")
            for line in migration.up.splitlines():
                print(f"      {line}")
    return 0


def _down(args: argparse.Namespace, driver, migrations) -> int:  # type: ignore[no-untyped-def]
    from sqlstep.report import style
    from sqlstep.runner import down

    def announce(migration, elapsed: int) -> None:  # type: ignore[no-untyped-def]
        print(f"{style('rolled back', 'yellow')} {migration.label} ({elapsed} ms)")

    done = down(
        driver,
        migrations,
        steps=args.steps,
        target=args.target,
        dry_run=args.dry_run,
        lock_timeout=args.lock_timeout,
        on_step=None if args.json else announce,
    )
    if args.json:
        print(
            json.dumps({"rolled_back": [m.label for m in done], "dry_run": args.dry_run}, indent=2)
        )
        return 0
    if not done:
        print("nothing to roll back")
        return 0
    if args.dry_run:
        print(f"would roll back {len(done)} migration(s):")
        for migration in done:
            print(f"  {migration.label}")
            for line in (migration.down or "").splitlines():
                print(f"      {line}")
    return 0


def _verify(args: argparse.Namespace, driver, migrations) -> int:  # type: ignore[no-untyped-def]
    from sqlstep.report import style, table
    from sqlstep.runner import accept_checksums, status

    if args.accept:
        changed = accept_checksums(driver, migrations)
        print(f"accepted {len(changed)} changed checksum(s)")
        return 0

    state = status(driver, migrations)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not state.mismatched,
                    "mismatched": [
                        {"migration": m.label, "recorded": r.checksum, "on_disk": m.checksum}
                        for m, r in state.mismatched
                    ],
                },
                indent=2,
            )
        )
        return 0 if not state.mismatched else 1

    if not state.mismatched:
        print(style(f"ok   {len(state.applied)} applied migration(s) match their files", "green"))
        return 0
    print(style(f"fail {len(state.mismatched)} migration(s) changed after being applied", "red"))
    print()
    print(
        table(
            ["migration", "recorded", "on disk"],
            [(m.label, r.checksum[:19], m.checksum[:19]) for m, r in state.mismatched],
        )
    )
    print()
    print("The database was built by the old text, so it no longer matches these files.")
    print("Write a new migration, or 'sqlstep verify --accept' if the change was cosmetic.")
    return 1


def _baseline(args: argparse.Namespace, driver, migrations) -> int:  # type: ignore[no-untyped-def]
    from sqlstep.runner import baseline

    marked = baseline(driver, migrations, args.version)
    if args.json:
        print(json.dumps({"marked": [m.label for m in marked]}, indent=2))
        return 0
    print(f"marked {len(marked)} migration(s) as applied without running them")
    for migration in marked:
        print(f"  {migration.label}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
