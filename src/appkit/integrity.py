"""Is a stored document still consistent, and will this code write all of it back?

The apps keep each document as one JSON blob, read whole and written whole.
Parsing proves only that the bytes are a document. Two failures parse cleanly:

- **A model that drops data.** Rename a field, or let a parser default a
  missing key, and the old key is silently ignored on read and gone on the next
  write. `roundtrip` catches it: it parses the stored bytes, serialises them the
  way the app would write them, and reports every stored value that would not
  survive.
- **A document that contradicts itself.** An entry pointing at a deleted pot, a
  duplicate id, a spend with a positive sign. The app passes its own `check`,
  which returns one line per problem.

`require` runs both and raises `IntegrityError`. An app calls it from its
backup's parse function, so the nightly job alerts after the copy is safe, and
`selfcheck.integrity_checks` runs it after every deploy, against the live
document, so a model that would drop data is rolled back before it writes. The
`parse` handed to it must be the model's plain parse, never that backup wrapper,
or each would call the other forever.

**Problems name a path, never a value.** These end up in Log Analytics, and the
values are someone's money and training history. So a path shows an object key
only when it reads as a schema field (`amount_pence`, `sets`); a key that may be
data, such as an exercise name in an object keyed by name, shows as its position
(`{key 3}`). An exception from `parse` or `dump` is reported by its type alone,
since a validation message quotes the input. An app's `check` must keep to the
same rule: name an index or an id, never an amount, a name or a note.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any

__all__ = ["IntegrityError", "problems", "require", "roundtrip"]

# How many problems the exception message lists before summarising the rest.
# selfcheck truncates its log line anyway; the full list is on `.problems`.
SHOWN = 5

# Values a writer may leave out rather than write, so their absence from the
# rewritten document loses nothing. Not False or 0: `active: false` read back as
# the default True, or a count read back as its default, is exactly the loss
# this module exists to catch.
_EMPTY: tuple[Any, ...] = (None, "", [], {})

# A key shown by name in a path: lower snake_case, as every schema field is.
_FIELD = re.compile(r"[a-z_][a-z0-9_]{0,63}")


class IntegrityError(ValueError):
    """The document would lose data on rewrite, or breaks one of its own rules."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        shown = "; ".join(problems[:SHOWN])
        more = f"; and {len(problems) - SHOWN} more" if len(problems) > SHOWN else ""
        super().__init__(f"{len(problems)} problem(s): {shown}{more}")


def roundtrip(
    raw: bytes, parse: Callable[[bytes], Any], dump: Callable[[Any], str | bytes]
) -> list[str]:
    """Every stored value that `dump(parse(raw))` would drop or change.

    A subset comparison. A key the rewrite adds (a new field with its default)
    is fine; a stored key it drops is a problem unless its value was empty
    (`None`, `""`, `[]`, `{}`), which writers omit. Lists must keep their
    length and compare item by item, since order is part of a log.
    """
    return _examine(raw, parse, dump, None)


def problems(
    raw: bytes,
    parse: Callable[[bytes], Any],
    dump: Callable[[Any], str | bytes],
    check: Callable[[Any], list[str]] | None = None,
) -> list[str]:
    """`roundtrip`'s problems, then the app's own `check` of the same parsed
    document. `check` returns one line per problem, naming no values."""
    return _examine(raw, parse, dump, check)


def require(
    raw: bytes,
    parse: Callable[[bytes], Any],
    dump: Callable[[Any], str | bytes],
    check: Callable[[Any], list[str]] | None = None,
) -> None:
    """`problems`, raising `IntegrityError` if there are any."""
    found = problems(raw, parse, dump, check)
    if found:
        raise IntegrityError(found)


def _examine(
    raw: bytes,
    parse: Callable[[bytes], Any],
    dump: Callable[[Any], str | bytes],
    check: Callable[[Any], list[str]] | None,
) -> list[str]:
    """Parse once, compare the rewrite, then run `check` on that one document."""
    try:
        stored = json.loads(raw)
        document = parse(raw)
    except Exception as exc:
        return [f"$: the stored document does not parse ({type(exc).__name__})"]
    try:
        rewritten = json.loads(dump(document))
    except Exception as exc:
        return [f"$: the document does not serialise ({type(exc).__name__})"]

    found: list[str] = []
    _compare(stored, rewritten, "$", found)
    if check is not None:
        found += check(document)
    return found


def _compare(stored: Any, rewritten: Any, path: str, found: list[str]) -> None:
    # One empty value written as another (`null` as `[]`) loses nothing.
    if _is_empty(stored) and _is_empty(rewritten):
        return

    if isinstance(stored, dict):
        if not isinstance(rewritten, dict):
            found.append(f"{path}: changed from an object to {_kind(rewritten)}")
            return
        for position, (key, value) in enumerate(stored.items()):
            child = f"{path}.{key}" if _FIELD.fullmatch(key) else f"{path}.{{key {position}}}"
            if key not in rewritten:
                if not _is_empty(value):
                    found.append(f"{child}: dropped")
                continue
            _compare(value, rewritten[key], child, found)
        return

    if isinstance(stored, list):
        if not isinstance(rewritten, list):
            found.append(f"{path}: changed from a list to {_kind(rewritten)}")
            return
        if len(stored) != len(rewritten):
            found.append(f"{path}: length changed from {len(stored)} to {len(rewritten)}")
            return
        for index, (old, new) in enumerate(zip(stored, rewritten, strict=True)):
            _compare(old, new, f"{path}[{index}]", found)
        return

    # A scalar. bool is checked apart from int, since True == 1 in Python but a
    # flag that became a number (or back) is a different value in the document.
    # NaN never equals itself, so two of them would read as a change.
    if _both_nan(stored, rewritten):
        return
    if isinstance(stored, bool) != isinstance(rewritten, bool) or stored != rewritten:
        found.append(f"{path}: changed")


def _both_nan(a: Any, b: Any) -> bool:
    return isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)


def _is_empty(value: Any) -> bool:
    return any(type(value) is type(empty) and value == empty for empty in _EMPTY)


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    return "a scalar"
