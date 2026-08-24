import pytest

from sqlstep.split import split


def test_plain_statements():
    assert split("create table a (i int); create table b (i int);") == [
        "create table a (i int)",
        "create table b (i int)",
    ]


def test_a_semicolon_inside_a_string_is_not_a_boundary():
    assert split("insert into a values ('x; y'); select 1;") == [
        "insert into a values ('x; y')",
        "select 1",
    ]


def test_a_doubled_quote_is_an_escape_not_a_close():
    assert split("insert into a values ('it''s; fine'); select 2;") == [
        "insert into a values ('it''s; fine')",
        "select 2",
    ]


def test_a_double_quoted_identifier_is_left_alone():
    assert split('select "weird;name" from a; select 1;') == [
        'select "weird;name" from a',
        "select 1",
    ]


def test_a_line_comment_is_dropped():
    assert split("-- drop table a;\ncreate table a (i int);") == ["create table a (i int)"]


def test_a_block_comment_is_dropped():
    assert split("/* a; b */ create table a (i int);") == ["create table a (i int)"]


def test_a_trigger_body_is_one_statement():
    sql = "create trigger t after insert on a begin update b set n = 1; end;\nselect 1;"
    parts = split(sql)
    assert len(parts) == 2
    assert "update b set n = 1" in parts[0]
    assert parts[1] == "select 1"


def test_a_dollar_quoted_function_body_is_one_statement():
    sql = (
        "create function f() returns void as $$ begin raise notice 'a; b'; end; $$ "
        "language plpgsql;\nselect 1;"
    )
    parts = split(sql)
    assert len(parts) == 2
    assert parts[1] == "select 1"


def test_a_tagged_dollar_quote():
    sql = "create function f() returns void as $body$ select 1; $body$ language sql;\nselect 2;"
    assert len(split(sql)) == 2


def test_a_bare_begin_is_a_transaction_not_a_block():
    """Treating it as a block would swallow the rest of the file."""
    assert split("begin; create table a (i int); commit;") == [
        "begin",
        "create table a (i int)",
        "commit",
    ]


def test_a_final_statement_without_a_semicolon():
    assert split("create table a (i int)") == ["create table a (i int)"]


def test_a_file_of_only_comments_has_no_statements():
    assert split("-- nothing to see\n/* nor here */\n") == []


@pytest.mark.parametrize("sql", ["", "   ", "\n\n"])
def test_empty_input(sql):
    assert split(sql) == []


def test_an_unterminated_string_does_not_hang():
    assert split("select 'unterminated") == ["select 'unterminated"]


def test_an_unterminated_block_comment_does_not_hang():
    assert split("/* never closed") == []


def test_removing_a_block_comment_leaves_a_separator():
    """Dropping the bytes outright would turn `select/* c */1` into `select1`."""
    assert split("select/* comment */1;") == ["select 1"]


def test_a_case_inside_a_trigger_does_not_close_the_body_early():
    sql = (
        "create trigger t after insert on a begin "
        "update b set n = case when n > 0 then 1 else 2 end; "
        "end;\nselect 1;"
    )
    parts = split(sql)
    assert len(parts) == 2, parts
    assert "case when" in parts[0]
    assert parts[1] == "select 1"


def test_a_nested_if_inside_a_function_body():
    sql = (
        "create function f() returns void as $$ begin "
        "if true then raise notice 'x'; end if; end; $$ language plpgsql;\nselect 1;"
    )
    assert len(split(sql)) == 2
