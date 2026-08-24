"""Split a script into statements, without getting fooled by the SQL in it.

Splitting on semicolons is the obvious approach and it is wrong. A semicolon
inside a string literal, inside a comment, or inside a trigger body is not a
statement boundary, and a migration that creates a trigger is exactly the sort
of migration people write once and never look at again.

So this walks the text and tracks what it is inside. Only Postgres and SQLite
syntax is modelled, because those are the two databases sqlstep supports.
"""

from __future__ import annotations

import re

#: A trigger or a PL/pgSQL block opens a body that contains its own semicolons.
_BLOCK_OPEN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_BLOCK_CLOSE = re.compile(r"\bEND\b", re.IGNORECASE)
#: Inside a body, these also open something that an END closes. Without them a
#: CASE inside a trigger closes the trigger early and the statement is cut in half.
_NESTED_OPEN = re.compile(r"\b(CASE|IF|LOOP)\b", re.IGNORECASE)
#: Statements after which a bare BEGIN starts a body rather than a transaction.
_BODY_INTRO = re.compile(
    r"\bCREATE\s+(OR\s+REPLACE\s+)?(TRIGGER|FUNCTION|PROCEDURE)\b", re.IGNORECASE
)


def split(script: str) -> list[str]:
    """Statements in ``script``, with comments and blank statements dropped."""
    statements: list[str] = []
    current: list[str] = []
    index = 0
    depth = 0
    length = len(script)

    while index < length:
        char = script[index]
        rest = script[index:]

        if char == "-" and rest.startswith("--"):
            end = script.find("\n", index)
            index = length if end == -1 else end
            continue
        if char == "/" and rest.startswith("/*"):
            end = script.find("*/", index + 2)
            index = length if end == -1 else end + 2
            # A space in place of the comment, so `select/* c */1` does not
            # become `select1`.
            current.append(" ")
            continue
        if char in "'\"":
            closing = _string_end(script, index, char)
            current.append(script[index:closing])
            index = closing
            continue
        if char == "$":
            tag = _dollar_tag(script, index)
            if tag is not None:
                closing = script.find(tag, index + len(tag))
                closing = length if closing == -1 else closing + len(tag)
                current.append(script[index:closing])
                index = closing
                continue

        word = _word_at(script, index)
        if word:
            opens = _opens_body(script, index, word) or (depth and _NESTED_OPEN.fullmatch(word))
            if opens:
                depth += 1
            elif depth and _BLOCK_CLOSE.fullmatch(word):
                depth -= 1
            current.append(script[index : index + len(word)])
            index += len(word)
            continue

        if char == ";" and depth == 0:
            statements.append("".join(current))
            current = []
            index += 1
            continue

        current.append(char)
        index += 1

    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


def _string_end(script: str, start: int, quote: str) -> int:
    """Index just past the closing quote, handling doubled quotes as escapes."""
    index = start + 1
    while index < len(script):
        if script[index] == quote:
            if index + 1 < len(script) and script[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(script)


def _dollar_tag(script: str, start: int) -> str | None:
    """The `$$` or `$tag$` opening a dollar-quoted string, if there is one."""
    match = re.match(r"\$[A-Za-z_]\w*\$|\$\$", script[start:])
    return match.group(0) if match else None


def _word_at(script: str, index: int) -> str:
    match = re.match(r"[A-Za-z_]\w*", script[index:])
    return match.group(0) if match else ""


def _opens_body(script: str, index: int, word: str) -> bool:
    """Is this BEGIN opening a trigger or function body rather than a transaction?

    Only counted when a CREATE TRIGGER, FUNCTION or PROCEDURE came before it in
    the same statement. A bare BEGIN at the top of a migration is somebody
    starting a transaction by hand, and treating that as a block would swallow
    the whole file.
    """
    if not _BLOCK_OPEN.fullmatch(word):
        return False
    preceding = script[:index]
    boundary = preceding.rfind(";")
    return bool(_BODY_INTRO.search(preceding[boundary + 1 :]))
