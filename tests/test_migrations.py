import pytest

from conftest import write
from sqlstep.errors import MigrationError
from sqlstep.migrations import checksum, create, discover, load


def test_a_file_splits_into_up_and_down(migrations_dir):
    path = write(migrations_dir, "0001_widgets.sql", "create table a (i int);", "drop table a;")
    migration = load(path)
    assert migration.version == "0001"
    assert migration.name == "widgets"
    assert migration.up == "create table a (i int);"
    assert migration.down == "drop table a;"
    assert migration.reversible


def test_a_migration_without_a_down_section_is_not_reversible(migrations_dir):
    path = write(migrations_dir, "0001_widgets.sql", "create table a (i int);")
    assert load(path).reversible is False


def test_an_empty_down_section_is_not_reversible(migrations_dir):
    path = write(migrations_dir, "0001_widgets.sql", "create table a (i int);", "   ")
    assert load(path).reversible is False


def test_a_file_with_no_up_marker_is_rejected(migrations_dir):
    path = migrations_dir / "0001_widgets.sql"
    path.write_text("create table a (i int);\n")
    with pytest.raises(MigrationError, match="no '-- migrate:up' line"):
        load(path)


def test_an_empty_up_section_is_rejected(migrations_dir):
    path = write(migrations_dir, "0001_widgets.sql", "   ")
    with pytest.raises(MigrationError, match="empty"):
        load(path)


def test_a_filename_without_a_version_is_rejected(migrations_dir):
    path = write(migrations_dir, "widgets.sql", "select 1;")
    with pytest.raises(MigrationError, match="not a migration filename"):
        load(path)


def test_the_no_transaction_directive_is_read(migrations_dir):
    path = write(
        migrations_dir,
        "0001_index.sql",
        "create index concurrently i on a (b);",
        header="-- sqlstep:no-transaction\n",
    )
    assert load(path).no_transaction is True


def test_a_migration_is_transactional_by_default(migrations_dir):
    path = write(migrations_dir, "0001_a.sql", "select 1;")
    assert load(path).no_transaction is False


def test_discover_orders_numerically_not_alphabetically(migrations_dir):
    for version in ("0001", "0002", "0010", "0009"):
        write(migrations_dir, f"{version}_m.sql", "select 1;")
    assert [m.version for m in discover(migrations_dir)] == ["0001", "0002", "0009", "0010"]


def test_two_migrations_with_the_same_version_are_rejected(migrations_dir):
    write(migrations_dir, "0001_one.sql", "select 1;")
    write(migrations_dir, "0001_two.sql", "select 2;")
    with pytest.raises(MigrationError, match="share version"):
        discover(migrations_dir)


def test_a_missing_directory_is_an_error(tmp_path):
    with pytest.raises(MigrationError, match="not a directory"):
        discover(tmp_path / "nope")


def test_the_checksum_ignores_trailing_whitespace_only():
    assert checksum("select 1;\n") == checksum("select 1;   \n\n")
    assert checksum("select 1;") != checksum("select 2;")


def test_the_checksum_notices_a_comment_change():
    """A checksum that forgives comments cannot tell them from a changed WHERE clause."""
    assert checksum("select 1; -- a") != checksum("select 1; -- b")


def test_create_writes_a_usable_template(migrations_dir):
    path = create(migrations_dir, "Add Widgets!")
    assert path.name.endswith("_add_widgets.sql")
    assert "-- migrate:up" in path.read_text()
    assert "-- migrate:down" in path.read_text()


def test_create_rejects_a_name_with_nothing_usable_in_it(migrations_dir):
    with pytest.raises(MigrationError, match="usable filename"):
        create(migrations_dir, "!!!")


def test_parse_is_case_insensitive_about_the_markers(migrations_dir):
    path = migrations_dir / "0001_a.sql"
    path.write_text("-- MIGRATE:UP\nselect 1;\n-- Migrate:Down\nselect 2;\n")
    migration = load(path)
    assert migration.up == "select 1;"
    assert migration.down == "select 2;"


def test_text_before_the_first_marker_is_ignored(migrations_dir):
    path = migrations_dir / "0001_a.sql"
    path.write_text("-- a note about this migration\n-- migrate:up\nselect 1;\n")
    assert load(path).up == "select 1;"
