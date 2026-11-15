"""EXE-02/EXE-03: driver values become JSON-safe values deterministically."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import math
import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentic_analytics.warehouse import to_json_value
from agentic_analytics.warehouse._common import count_sql, make_result

CASES: list[tuple[Any, Any]] = [
    (None, None),
    (True, True),
    (0, 0),
    (2**70, 2**70),  # UInt128 / Int256 stay exact
    (1.5, 1.5),
    (float("nan"), None),
    (float("inf"), None),
    (float("-inf"), None),
    ("Ünïcödé", "Ünïcödé"),
    (Decimal("100.00"), 100),
    (Decimal("-7"), -7),
    (Decimal("1.50"), 1.5),
    (Decimal("0.1"), 0.1),
    (Decimal("NaN"), None),
    (Decimal("Infinity"), None),
    (dt.date(2024, 2, 29), "2024-02-29"),
    (dt.datetime(2024, 2, 29, 13, 5, 9), "2024-02-29T13:05:09"),
    (dt.datetime(2024, 2, 29, 13, 5, 9, 1500), "2024-02-29T13:05:09.001500"),
    (
        dt.datetime(2024, 2, 29, 14, 5, 9, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        "2024-02-29T13:05:09+00:00",
    ),
    (dt.time(7, 30), "07:30:00"),
    (dt.timedelta(seconds=90), 90),
    (dt.timedelta(milliseconds=1500), 1.5),
    (uuid.UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
    (ipaddress.IPv4Address("10.1.2.3"), "10.1.2.3"),
    (ipaddress.IPv6Address("::1"), "::1"),
    (b"\x00\xffab", "AP9hYg=="),
    ([1, Decimal("2.5"), None], [1, 2.5, None]),
    ((1, "a"), [1, "a"]),
    ({"k": dt.date(2024, 1, 1), 3: 4}, [["k", "2024-01-01"], [3, 4]]),
    ({1: "int key", "1": "str key"}, [["1", "str key"], [1, "int key"]]),  # no collision
    ({3, 1, 2}, [1, 2, 3]),
]


@pytest.mark.parametrize(("value", "want"), CASES)
def test_each_type_converts_as_documented(value: Any, want: Any) -> None:
    got = to_json_value(value)
    assert got == want
    assert type(got) is type(want)


def test_bool_is_not_turned_into_int() -> None:
    assert to_json_value(True) is True


def test_unknown_objects_become_their_string() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd"

    assert to_json_value(Odd()) == "odd"


def test_result_set_from_driver_rows_is_json_round_trippable() -> None:
    rs = make_result(
        ("d", "p", "u"),
        [(dt.date(2024, 1, 1), Decimal("250000.00"), uuid.UUID(int=1)), (None, float("nan"), None)],
    )
    again = type(rs).model_validate_json(rs.model_dump_json())
    assert again == rs
    assert rs.rows[1] == (None, None, None)


_driver_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | st.decimals()
    | st.dates()
    | st.datetimes()
    | st.datetimes(timezones=st.timezones())
    | st.uuids()
    | st.binary(max_size=16)
    | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=3), inner),
    max_leaves=8,
)


@given(_driver_values)
def test_property_output_is_strict_json_and_deterministic(value: Any) -> None:
    out = to_json_value(value)
    text = json.dumps(out, allow_nan=False, sort_keys=True)
    assert text == json.dumps(to_json_value(value), allow_nan=False, sort_keys=True)
    assert not (isinstance(out, float) and not math.isfinite(out))


def test_count_sql_wraps_safely() -> None:
    assert count_sql("SELECT 1 ;; ", "clickhouse") == (
        "SELECT count(*) AS n FROM (\nSELECT 1\n) AS aa_counted"
    )
    # a trailing line comment cannot swallow the closing parenthesis
    assert count_sql("SELECT 1 -- note", "clickhouse").endswith("SELECT 1\n) AS aa_counted")


@pytest.mark.parametrize("engine", ["clickhouse", "snowflake"])
@pytest.mark.parametrize(
    "sql",
    ["SELECT 1; -- c", "SELECT 1;\n-- note", "SELECT 1 /* x */ ;", "SELECT 1 -- a\n;\n-- b"],
)
def test_count_sql_drops_trailing_semicolons_and_comments(sql: str, engine: str) -> None:
    """Round 2: the count for `SELECT 1; -- c` must still be one valid guarded statement."""
    from agentic_analytics.warehouse import check_sql

    wrapped = count_sql(sql, engine)  # type: ignore[arg-type]
    assert wrapped == "SELECT count(*) AS n FROM (\nSELECT 1\n) AS aa_counted"
    assert check_sql(wrapped, engine).ok  # type: ignore[arg-type]
