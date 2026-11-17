"""Tolerant result match (OWNER D-81, Tech Spec §6.1).

The headline score accepts four shapes of a right answer that the strict, pre-registered match
(`result.py`) rejects. Each is a pre-normalisation of the predicted rows, tried only after the
strict match failed, and the normalised rows must then pass the strict match unchanged: same
column mapping, same per-column tolerance, same row count. Every backend gets the same rules.
Only answer-expected questions are eligible; clarify-expected questions and probes never are.

1. **percent_scale.** The question asks for a percentage (its text says "percent",
   "percentage", "per cent" or "%"), and a gold column is a percent-unit column: covered by
   the question's tolerance with D-51's percent-unit `abs` (at least 0.01) and named as a
   percentage (``pct``, ``percent`` or ``points`` in its name). A predicted numeric column is
   multiplied by 100 when, so scaled, its values fit such a gold column within that tolerance
   and, unscaled, they do not (0.0817 for 8.17).
2. **pivot.** The gold is long: two columns, one all-text label column and one all-numeric
   value column, at least one row, distinct labels. The prediction is one wide row whose
   column names are the gold's labels (case-insensitive; spaces, ``-`` and ``_`` are the same
   separator). Every gold label must be a predicted column; any other predicted column must
   hold a numeric zero (a label the gold's GROUP BY omitted, e.g. ``unknown = 0``). The row is
   unpivoted into the gold's two columns.
3. **empty_check.** On an `expects_empty` question with an empty gold, a single predicted row
   counts as no rows when it has at least one numeric cell, every numeric cell (numbers, and
   text that is a plain number) is zero, and every other cell is text or null (the dates of a
   no-data check: ``earliest_sale_date, latest_sale_date, sales_in_1994 = 0``).
4. **period_number.** A predicted column of dates that are all month starts
   (``YYYY-MM-01``, optionally with a midnight time) in one calendar year becomes the month
   number (1-12) when the question names months and the gold has a column of month numbers,
   or the quarter number (1-4) when every date is a quarter start, the question names quarters
   and the gold has a column of quarter numbers. Dates over two or more years never convert:
   their month numbers would merge different periods.

percent_scale and period_number act on columns and may apply together (a monthly percentage
change returned as month dates and fractions); pivot and empty_check reshape the whole result
and apply alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from agentic_analytics.schemas import Equivalence, Execution, Question, ResultSet, Tolerance
from agentic_analytics.scoring.result import _Column, _is_num, match_rows

PERCENT_ABS = 0.01  # D-51: a percent-unit column's absolute tolerance
PERCENT_SCALE = 100
_ASKS_PERCENT = re.compile(r"\bper\s?cent(?:age)?s?\b|%", re.IGNORECASE)
_PERCENT_COLUMN = re.compile(r"pct|percent|points", re.IGNORECASE)
_NAMES_MONTHS = re.compile(r"\bmonths?\b", re.IGNORECASE)
_NAMES_QUARTERS = re.compile(r"\bquarters?\b", re.IGNORECASE)
_PERIOD_START = re.compile(
    r"(\d{4})-(\d{2})-01(?:[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?)?", re.ASCII
)
_PLAIN_NUMBER = re.compile(r"\s*[-+]?\d+(?:\.\d+)?\s*", re.ASCII)
_SEPARATORS = re.compile(r"[\s_-]+")
QUARTER_STARTS = frozenset({1, 4, 7, 10})

Variant = tuple[tuple[Equivalence, ...], ResultSet]


def tolerant_match(
    execution: Execution | None, gold: ResultSet | None, question: Question
) -> tuple[Equivalence, ...]:
    """The equivalences under which the executed rows match the gold, or () when none does.

    Callers use it only after the strict match failed; it does not re-check the strict match.
    """
    if question.expected != "answer" or gold is None or execution is None:
        return ()
    if execution.error is not None or execution.result is None or execution.truncated:
        return ()
    for rules, predicted in variants(execution.result, gold, question):
        ok, _ = match_rows(predicted, gold, tolerance=question.tolerance, ordered=question.ordered)
        if ok:
            return rules
    return ()


def variants(predicted: ResultSet, gold: ResultSet, question: Question) -> Iterator[Variant]:
    """Each normalised form of `predicted` with the rules that made it, in the order tried."""
    if (empty := empty_check(predicted, gold, question)) is not None:
        yield ("empty_check",), empty
    if (long := pivot(predicted, gold)) is not None:
        yield ("pivot",), long
    scaled = percent_columns(predicted, gold, question)
    periods = period_columns(predicted, gold, question)
    if scaled:
        yield ("percent_scale",), _replace(predicted, scaled)
    for period in periods:
        yield ("period_number",), _replace(predicted, period)
    if scaled:
        for period in periods:
            yield ("percent_scale", "period_number"), _replace(predicted, {**scaled, **period})


def _replace(rs: ResultSet, columns: dict[int, list[Any]]) -> ResultSet:
    rows = tuple(
        tuple(columns[i][k] if i in columns else v for i, v in enumerate(row))
        for k, row in enumerate(rs.rows)
    )
    return ResultSet(columns=rs.columns, rows=rows)


def _column(rs: ResultSet, i: int) -> list[Any]:
    return [row[i] for row in rs.rows]


# 1. percent_scale


def _gold_percent_columns(gold: ResultSet, question: Question) -> list[int]:
    tol = question.tolerance
    if tol.abs < PERCENT_ABS or not _ASKS_PERCENT.search(question.text):
        return []
    covered = None if tol.columns is None else {c.casefold() for c in tol.columns}
    return [
        j
        for j, name in enumerate(gold.columns)
        if (covered is None or name.casefold() in covered) and _PERCENT_COLUMN.search(name)
    ]


def percent_columns(
    predicted: ResultSet, gold: ResultSet, question: Question
) -> dict[int, list[Any]]:
    """{predicted column: its values ×100} for each fraction column that fits a gold percent
    column only once scaled; {} when the rule does not apply."""
    targets = [_Column(_column(gold, j)) for j in _gold_percent_columns(gold, question)]
    if not targets:
        return {}
    tol: Tolerance = question.tolerance
    out: dict[int, list[Any]] = {}
    for i in range(len(predicted.columns)):
        values = _column(predicted, i)
        if not any(_is_num(v) for v in values):
            continue
        scaled = [v * PERCENT_SCALE if _is_num(v) else v for v in values]
        before, after = _Column(values), _Column(scaled)
        if any(after.fits(g, tol) and not before.fits(g, tol) for g in targets):
            out[i] = scaled
    return out


# 2. pivot


def _label_key(text: str) -> str:
    return _SEPARATORS.sub("_", text.strip().casefold())


def pivot(predicted: ResultSet, gold: ResultSet) -> ResultSet | None:
    """The one wide predicted row unpivoted into the gold's long shape, or None."""
    if len(gold.columns) != 2 or not gold.rows or len(predicted.rows) != 1:
        return None
    text = [all(isinstance(r[j], str) for r in gold.rows) for j in (0, 1)]
    nums = [all(_is_num(r[j]) for r in gold.rows) for j in (0, 1)]
    if text[0] and nums[1]:
        label_col = 0
    elif text[1] and nums[0]:
        label_col = 1
    else:
        return None
    labels: dict[str, str] = {}
    for r in gold.rows:
        labels.setdefault(_label_key(r[label_col]), r[label_col])
    if len(labels) != len(gold.rows):
        return None
    keys = [_label_key(c) for c in predicted.columns]
    if len(set(keys)) != len(keys) or not set(labels) <= set(keys):
        return None
    (row,) = predicted.rows
    long: list[tuple[Any, Any]] = []
    for key, cell in zip(keys, row, strict=True):
        if key in labels:
            pair = (labels[key], cell) if label_col == 0 else (cell, labels[key])
            long.append(pair)
        elif not (_is_num(cell) and cell == 0):
            return None  # an extra label must be one the gold omitted, so zero
    return ResultSet(columns=gold.columns, rows=tuple(long))


# 3. empty_check


def _numeric(cell: Any) -> float | None:
    if _is_num(cell):
        return float(cell)
    if isinstance(cell, str) and _PLAIN_NUMBER.fullmatch(cell):
        return float(cell)
    return None


def empty_check(predicted: ResultSet, gold: ResultSet, question: Question) -> ResultSet | None:
    """No rows, in the gold's columns, when the one predicted row is a no-data check."""
    if not question.expects_empty or gold.rows or len(predicted.rows) != 1:
        return None
    (row,) = predicted.rows
    counts = [n for n in (_numeric(c) for c in row) if n is not None]
    if not counts or any(n != 0 for n in counts):
        return None
    if any(_numeric(c) is None and not (c is None or isinstance(c, str)) for c in row):
        return None  # booleans and nested values are not a no-data check
    return ResultSet(columns=gold.columns, rows=())


# 4. period_number


def _period_starts(values: Sequence[Any]) -> list[tuple[int, int] | None] | None:
    """(year, month) per cell when every non-null cell is a month start, else None."""
    out: list[tuple[int, int] | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        m = _PERIOD_START.fullmatch(v) if isinstance(v, str) else None
        if m is None or not 1 <= int(m.group(2)) <= 12:
            return None
        out.append((int(m.group(1)), int(m.group(2))))
    return out if any(p is not None for p in out) else None


def _has_numbers(gold: ResultSet, top: int) -> bool:
    """A gold column whose every cell is an integer in 1..top."""
    for j in range(len(gold.columns)):
        cells = _column(gold, j)
        if cells and all(_is_num(v) and float(v).is_integer() and 1 <= v <= top for v in cells):
            return True
    return False


def period_columns(
    predicted: ResultSet, gold: ResultSet, question: Question
) -> list[dict[int, list[Any]]]:
    """The month-number and quarter-number readings of the predicted date columns that apply."""
    if not gold.rows:
        return []
    dates: dict[int, list[tuple[int, int] | None]] = {}
    for i in range(len(predicted.columns)):
        starts = _period_starts(_column(predicted, i))
        if starts is not None and len({p[0] for p in starts if p is not None}) == 1:
            dates[i] = starts
    if not dates:
        return []
    out: list[dict[int, list[Any]]] = []
    if _NAMES_MONTHS.search(question.text) and _has_numbers(gold, 12):
        out.append({i: [None if p is None else p[1] for p in s] for i, s in dates.items()})
    quarter_starts = all(p is None or p[1] in QUARTER_STARTS for s in dates.values() for p in s)
    if quarter_starts and _NAMES_QUARTERS.search(question.text) and _has_numbers(gold, 4):
        quarters = {
            i: [None if p is None else (p[1] - 1) // 3 + 1 for p in s] for i, s in dates.items()
        }
        out.append(quarters)
    return out
