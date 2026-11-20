"""integrity.py: what a rewrite would lose, and the app's own checks."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from appkit import integrity, selfcheck


def _faithful(raw: bytes) -> Any:
    return json.loads(raw)


def _dump(doc: Any) -> str:
    return json.dumps(doc, indent=2)


def _raw(doc: Any) -> bytes:
    return json.dumps(doc).encode("utf-8")


def test_a_faithful_model_has_no_problems_whatever_the_formatting():
    doc = {"sets": [{"reps": 5, "weight": 62.5}], "active": False, "count": 0}
    assert integrity.roundtrip(_raw(doc), _faithful, _dump) == []


def test_a_new_defaulted_field_is_fine():
    def with_default(raw: bytes) -> Any:
        return {**json.loads(raw), "schema_version": 1}

    assert integrity.roundtrip(_raw({"name": "x"}), with_default, _dump) == []


def test_a_renamed_field_is_reported_as_dropped_by_path():
    """The bug this exists for: a parser that defaults a key it no longer knows."""

    def renamed(raw: bytes) -> Any:
        doc = json.loads(raw)
        return {"sets": [{"count": s.get("count", 0), "weight": s["weight"]} for s in doc["sets"]]}

    raw = _raw({"sets": [{"reps": 5, "weight": 60}, {"reps": 3, "weight": 70}]})
    assert integrity.roundtrip(raw, renamed, _dump) == [
        "$.sets[0].reps: dropped",
        "$.sets[1].reps: dropped",
    ]


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_dropping_an_empty_value_loses_nothing(empty):
    def omit_empty(raw: bytes) -> Any:
        return {k: v for k, v in json.loads(raw).items() if v not in (None, "", [], {})}

    assert integrity.roundtrip(_raw({"name": "x", "note": empty}), omit_empty, _dump) == []


@pytest.mark.parametrize("value", [False, 0])
def test_dropping_false_or_zero_is_a_loss(value):
    """`active: false` read back as a default True is real data lost."""

    def drop(raw: bytes) -> Any:
        return {}

    assert integrity.roundtrip(_raw({"active": value}), drop, _dump) == ["$.active: dropped"]


def test_a_changed_value_names_the_path_but_never_the_value():
    def zeroed(raw: bytes) -> Any:
        return {"amount_pence": 0, "name": "Holidays"}

    found = integrity.roundtrip(_raw({"amount_pence": 271234, "name": "Holidays"}), zeroed, _dump)
    assert found == ["$.amount_pence: changed"]
    assert "271234" not in found[0]


def test_a_flag_that_became_a_number_is_a_change():
    # True == 1 in Python; in the document they are different values.
    assert integrity.roundtrip(_raw({"x": True}), lambda raw: {"x": 1}, _dump) == ["$.x: changed"]


def test_an_int_read_back_as_the_same_float_is_not_a_change():
    assert integrity.roundtrip(_raw({"weight": 60}), lambda raw: {"weight": 60.0}, _dump) == []


def test_a_list_that_lost_an_item_reports_its_length():
    found = integrity.roundtrip(_raw({"log": [1, 2, 3]}), lambda raw: {"log": [1, 2]}, _dump)
    assert found == ["$.log: length changed from 3 to 2"]


@pytest.mark.parametrize(
    ("stored", "rewritten", "problem"),
    [
        ({"x": {"a": 1}}, {"x": [1]}, "$.x: changed from an object to a list"),
        ({"x": [1]}, {"x": "1"}, "$.x: changed from a list to a scalar"),
        ({"x": [1]}, {"x": {"a": 1}}, "$.x: changed from a list to an object"),
    ],
)
def test_a_changed_shape_is_reported(stored, rewritten, problem):
    assert integrity.roundtrip(_raw(stored), lambda raw: rewritten, _dump) == [problem]


def test_dump_may_return_bytes():
    assert integrity.roundtrip(_raw({"a": 1}), _faithful, lambda d: _dump(d).encode()) == []


def test_problems_adds_the_apps_own_check_after_the_roundtrip():
    def check(doc: Any) -> list[str]:
        pots = {p["id"] for p in doc["pots"]}
        return [
            f"pot_entries[{i}]: points at no pot"
            for i, e in enumerate(doc["pot_entries"])
            if e["pot_id"] not in pots
        ]

    raw = _raw({"pots": [{"id": "a"}], "pot_entries": [{"pot_id": "a"}, {"pot_id": "b"}]})
    assert integrity.problems(raw, _faithful, _dump, check) == ["pot_entries[1]: points at no pot"]


def test_the_document_is_parsed_once_and_check_sees_that_one():
    parsed: list[Any] = []

    def parse(raw: bytes) -> Any:
        parsed.append(json.loads(raw))
        return parsed[-1]

    seen: list[Any] = []

    def check(doc: Any) -> list[str]:
        seen.append(doc)
        return []

    integrity.problems(_raw({"a": 1}), parse, _dump, check)
    assert len(parsed) == 1
    assert seen[0] is parsed[0]


def test_a_key_that_may_be_data_shows_as_its_position_not_its_name():
    """Objects keyed by exercise or pot name must not put the name in the log."""
    stored = {"exercise_notes": {"Bench press": "seat 4", "row": "blue handle"}}
    found = integrity.roundtrip(_raw(stored), lambda raw: {"exercise_notes": {}}, _dump)
    assert found == ["$.exercise_notes.{key 0}: dropped", "$.exercise_notes.row: dropped"]
    assert not any("Bench" in line or "seat" in line for line in found)


@pytest.mark.parametrize("key", ["Holidays", "a.b", "x[0]", "café", "a" * 65])
def test_a_key_that_is_not_snake_case_is_hidden(key):
    found = integrity.roundtrip(_raw({key: 1}), lambda raw: {}, _dump)
    assert found == ["$.{key 0}: dropped"]


def test_a_parse_failure_names_its_type_never_its_message():
    """A validation message quotes the input, which is the document's data."""

    def parse(raw: bytes) -> Any:
        raise ValueError("amount_pence: input_value='271234.5' is not an integer")

    found = integrity.roundtrip(_raw({"amount_pence": 271234.5}), parse, _dump)
    assert found == ["$: the stored document does not parse (ValueError)"]


def test_bytes_that_are_not_json_are_a_parse_failure():
    found = integrity.roundtrip(b"{not json", _faithful, _dump)
    assert found == ["$: the stored document does not parse (JSONDecodeError)"]


def test_a_dump_failure_names_its_type_never_its_message():
    def dump(doc: Any) -> str:
        raise TypeError("Object of type Decimal('12.34') is not JSON serializable")

    found = integrity.roundtrip(_raw({"a": 1}), _faithful, dump)
    assert found == ["$: the document does not serialise (TypeError)"]


@pytest.mark.parametrize(
    ("stored", "rewritten"),
    [(None, []), ([], None), ("", None), ({}, []), (None, {})],
)
def test_one_empty_value_written_as_another_loses_nothing(stored, rewritten):
    assert integrity.roundtrip(_raw({"tags": stored}), lambda raw: {"tags": rewritten}, _dump) == []


def test_an_empty_value_that_became_data_is_a_change():
    found = integrity.roundtrip(_raw({"tags": None}), lambda raw: {"tags": ["x"]}, _dump)
    assert found == ["$.tags: changed"]


def test_a_stored_nan_kept_as_nan_is_not_a_change():
    raw = b'{"weight": NaN}'
    assert integrity.roundtrip(raw, _faithful, _dump) == []
    assert integrity.roundtrip(raw, lambda raw: {"weight": 0.0}, _dump) == ["$.weight: changed"]


def test_require_passes_quietly_and_raises_with_every_problem():
    integrity.require(_raw({"a": 1}), _faithful, _dump)

    many = [f"problem {i}" for i in range(7)]
    with pytest.raises(integrity.IntegrityError) as raised:
        integrity.require(_raw({}), _faithful, _dump, lambda doc: many)
    assert raised.value.problems == many
    assert str(raised.value) == (
        "7 problem(s): problem 0; problem 1; problem 2; problem 3; problem 4; and 2 more"
    )


def test_an_integrity_error_is_a_value_error():
    # So an app's backup parse function raising it fails the run like a parse error.
    assert issubclass(integrity.IntegrityError, ValueError)


def test_the_selfcheck_passes_a_good_document_and_a_never_written_one(caplog):
    good = selfcheck.integrity_checks("doc.json", lambda: _raw({"a": 1}), _faithful, _dump)
    never = selfcheck.integrity_checks("new.json", lambda: None, _faithful, _dump)
    assert [label for label, _ in good] == ["integrity doc.json"]
    with caplog.at_level(logging.INFO):
        assert selfcheck.run([*good, *never]) == 0
    assert "selfcheck: ok integrity doc.json" in caplog.text


def test_the_selfcheck_fails_a_lossy_model_or_a_failed_check(caplog):
    lossy = selfcheck.integrity_checks("doc.json", lambda: _raw({"a": 1}), lambda raw: {}, _dump)
    broken = selfcheck.integrity_checks(
        "other.json", lambda: _raw({}), _faithful, _dump, lambda doc: ["duplicate id x"]
    )
    with caplog.at_level(logging.INFO):
        assert selfcheck.run([*lossy, *broken]) == 1
    assert "FAIL integrity doc.json: IntegrityError: 1 problem(s): $.a: dropped" in caplog.text
    assert "FAIL integrity other.json: IntegrityError: 1 problem(s): duplicate id x" in caplog.text
