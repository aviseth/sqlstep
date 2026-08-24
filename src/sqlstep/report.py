"""Plain terminal output. No rendering library, because this runs inside CI far more
often than it runs in front of a person."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence

_RESET = "\033[0m"
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def colour_enabled(stream: object | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream or sys.stdout, "isatty", lambda: False)())


def style(text: str, *names: str, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = colour_enabled()
    if not enabled or not names:
        return text
    return "".join(_STYLES.get(n, "") for n in names) + text + _RESET


def plain(text: str) -> str:
    out, escaping = [], False
    for char in text:
        if escaping:
            escaping = char != "m"
            continue
        if char == "\033":
            escaping = True
            continue
        out.append(char)
    return "".join(out)


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], *, aligns: str = "") -> str:
    body = [list(map(str, row)) for row in rows]
    if not body:
        return ""
    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(plain(cell)))
    aligns = (aligns + "l" * len(headers))[: len(headers)]

    def line(cells: Sequence[str], header: bool = False) -> str:
        parts = []
        for cell, width, align in zip(cells, widths, aligns, strict=False):
            pad = width - len(plain(cell))
            parts.append(" " * pad + cell if align == "r" else cell + " " * pad)
        text = "  ".join(parts).rstrip()
        return style(text, "bold") if header else text

    return "\n".join(
        [line(headers, header=True), "  ".join("-" * w for w in widths), *(line(r) for r in body)]
    )


def percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def seconds(value: float) -> str:
    """Readable at both ends: microseconds for a fixture, minutes for a suite."""
    if value >= 60:
        return f"{int(value // 60)}m{value % 60:04.1f}s"
    if value >= 1:
        return f"{value:.2f}s"
    return f"{value * 1000:.0f}ms"
