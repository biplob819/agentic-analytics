"""One deterministic chart builder for every backend (ANS-01, D-6).

Models never write chart specs: the chart is built from the executed result set, the same way for
Claude, GPT and Cortex. The output is a Vega-Lite v5 spec with the rows inline, holding only JSON
scalars, or `None` when the table should be shown instead.

Rules, applied to the rows exactly as executed (no row is invented; values are the executed
values, only reordered):

- one row, one column, a non-null, non-boolean value: a KPI (`text` mark);
- two columns, one a period and the other a numeric measure: a `line`, sorted by period. A period
  is one of
  1. valid ISO strings, all `YYYY-MM-DD` or all `YYYY-MM`: a temporal axis on a UTC scale with a
     UTC time unit, so viewers west of Greenwich do not see every month shifted back one;
  2. integers in a column whose name has the token `year` or `yr` (`year`, `sale_year`,
     `toYear(date)`; not `yearly`, not `years`): ordinal;
  3. integers all in 1990–2100 that are distinct and strictly consecutive once sorted (one row per
     year, no gaps), in a column whose name has no id/code/amount-like token (`store_id`, `price`
     never qualify): ordinal.
  A lower-numbered kind wins; between equals, the left-most column;
- two columns, one text and the other a numeric measure: a horizontal `bar` chart sorted by value
  descending (ties by label), at most `MAX_BARS` bars, the title naming the cut against the true
  total;
- two columns need at least two rows: one row of two or more values would draw a one-point line
  or a single bar (A2.3 live: GPT answered a one-year count as `year, count`), so it has none;
- anything else gives `None`: an empty result, a truncated result (the executor's cap cut rows, so
  a line would stop early and "top N" would not be the top), a null in either column, booleans or
  nested values, integers beyond 2^53 (a browser cannot show them exactly), a repeated period or
  label (unaggregated rows would draw a zigzag or overlapping bars), two or more measures, or more
  than two columns.

Fields are renamed to fixed keys (`value`, `period`, `label`) so a column name holding a dot or a
bracket never becomes a Vega-Lite field path; the original names are the axis titles.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from evalkit.schemas import ResultSet

VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
MAX_BARS = 25
YEAR_MIN, YEAR_MAX = 1990, 2100
MAX_SAFE_INT = 2**53  # JavaScript numbers are exact up to here
TITLE_CHARS = 200

ChartKind = Literal["kpi", "line", "bar"]
_ISO_FORMATS = (
    (re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"), "%Y-%m-%d", "utcyearmonthdate"),
    (re.compile(r"\A[0-9]{4}-[0-9]{2}\Z"), "%Y-%m", "utcyearmonth"),
)
_NAME_TOKENS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_YEAR_TOKENS = frozenset({"year", "yr"})
# a column named with any of these is a measure or an identifier, never an unnamed year
_NOT_PERIOD_TOKENS = frozenset(
    {"id", "code", "key", "no", "num", "number", "price", "amount", "value", "count", "total",
     "sum", "avg", "mean", "median", "min", "max", "cost", "sales", "qty", "quantity"}
)  # fmt: skip


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -MAX_SAFE_INT <= v <= MAX_SAFE_INT
    return isinstance(v, float) and math.isfinite(v)


def _iso_time_unit(values: list[Any]) -> str | None:
    for pattern, fmt, unit in _ISO_FORMATS:
        if all(isinstance(v, str) and pattern.match(v) for v in values):
            try:
                for v in values:
                    datetime.strptime(v, fmt)
            except ValueError:
                return None
            return unit
    return None


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in _NAME_TOKENS.findall(name)}


def _consecutive_years(values: list[int]) -> bool:
    ordered = sorted(values)
    return (
        len(ordered) >= 2
        and ordered[0] >= YEAR_MIN
        and ordered[-1] <= YEAR_MAX
        and all(b - a == 1 for a, b in pairwise(ordered))
    )


def _period(name: str, values: list[Any]) -> tuple[int, dict[str, Any]] | None:
    """How strongly a column reads as a period (lower is stronger) and its x encoding, or None."""
    unit = _iso_time_unit(values)
    if unit is not None:
        return 0, {"type": "temporal", "timeUnit": unit, "scale": {"type": "utc"}}
    if not all(_is_int(v) and _is_number(v) for v in values):
        return None
    tokens = _tokens(name)
    if tokens & _YEAR_TOKENS:
        return 1, {"type": "ordinal"}
    if not tokens & _NOT_PERIOD_TOKENS and _consecutive_years(values):
        return 2, {"type": "ordinal"}
    return None


def _title(question_text: str | None, fallback: str, note: str = "") -> str:
    text = question_text.strip() if question_text else ""
    base = (text or fallback)[:TITLE_CHARS]
    return f"{base} {note}" if note else base


def _spec(
    kind: ChartKind,
    title: str,
    values: list[dict[str, Any]],
    total: int,
    mark: dict[str, Any],
    encoding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "title": title,
        "data": {"values": values},
        "mark": mark,
        "encoding": encoding,
        "usermeta": {"chart": kind, "rows_shown": len(values), "rows_total": total},
    }


def _kpi(column: str, value: Any, question_text: str | None) -> dict[str, Any] | None:
    if not (isinstance(value, str) or _is_number(value)):
        return None  # null, boolean, nested, or an integer too large to show exactly
    text: dict[str, Any] = {
        "field": "value",
        "type": "quantitative" if _is_number(value) else "nominal",
        "title": column,
    }
    if _is_int(value):
        text["format"] = ","
    return _spec(
        "kpi",
        _title(question_text, column),
        [{"value": value}],
        1,
        {"type": "text", "fontSize": 48, "fontWeight": "bold"},
        {"text": text},
    )


def _line(
    period: str,
    measure: str,
    pairs: list[tuple[Any, Any]],
    x: dict[str, Any],
    question_text: str | None,
) -> dict[str, Any]:
    pairs = sorted(pairs, key=lambda p: p[0])
    return _spec(
        "line",
        _title(question_text, f"{measure} by {period}"),
        [{"period": p, "value": m} for p, m in pairs],
        len(pairs),
        {"type": "line", "point": True},
        {
            "x": {"field": "period", **x, "title": period},
            "y": {"field": "value", "type": "quantitative", "title": measure},
        },
    )


def _bar(
    label: str, measure: str, pairs: list[tuple[Any, Any]], question_text: str | None
) -> dict[str, Any]:
    total = len(pairs)
    pairs = sorted(pairs, key=lambda p: (-p[1], p[0]))[:MAX_BARS]
    note = f"(top {MAX_BARS} of {total})" if total > MAX_BARS else ""
    return _spec(
        "bar",
        _title(question_text, f"{measure} by {label}", note),
        [{"label": lab, "value": m} for lab, m in pairs],
        total,
        {"type": "bar"},
        {
            "y": {"field": "label", "type": "nominal", "sort": None, "title": label},
            "x": {"field": "value", "type": "quantitative", "title": measure},
        },
    )


def chart_for(
    result: ResultSet,
    question_text: str | None = None,
    *,
    rows_total: int | None = None,
) -> dict[str, Any] | None:
    """The Vega-Lite v5 spec for an executed result set, or None to show the table.

    `rows_total` is the execution's row count before the executor's cap; when it exceeds the rows
    held, the result is truncated and there is no chart. A count below the rows held is an error.
    """
    columns = list(result.columns)
    rows = [list(r) for r in result.rows]
    if rows_total is not None:
        if isinstance(rows_total, bool) or not isinstance(rows_total, int):
            raise ValueError("rows_total must be an integer")
        if rows_total < len(rows):
            raise ValueError("rows_total is smaller than the rows held")
        if rows_total > len(rows):
            return None  # truncated: a partial line or a false "top N"
    if not rows or len(columns) > 2:
        return None
    if len(columns) == 1:
        return _kpi(columns[0], rows[0][0], question_text) if len(rows) == 1 else None
    if len(rows) == 1:
        return None  # one row of several values: a one-point line or a one-bar chart says less
    cols = [[r[0] for r in rows], [r[1] for r in rows]]
    if any(v is None for col in cols for v in col):
        return None
    numeric = [all(_is_number(v) for v in col) for col in cols]

    # line: a period column and the other column numeric
    periods = [_period(columns[i], cols[i]) for i in (0, 1)]
    options = [(p[0], i, p[1]) for i, p in enumerate(periods) if p is not None and numeric[1 - i]]
    if options:
        _, i, x = min(options, key=lambda o: (o[0], o[1]))
        if len(set(cols[i])) != len(cols[i]):
            return None  # a repeated period: rows are not aggregated per period
        pairs = list(zip(cols[i], cols[1 - i], strict=True))
        return _line(columns[i], columns[1 - i], pairs, x, question_text)

    # bar: a text column and a numeric column
    text = [all(isinstance(v, str) for v in col) for col in cols]
    for i in (0, 1):
        if text[i] and numeric[1 - i]:
            if len(set(cols[i])) != len(cols[i]):
                return None  # a repeated label: bars would overlap
            pairs = list(zip(cols[i], cols[1 - i], strict=True))
            return _bar(columns[i], columns[1 - i], pairs, question_text)
    return None
