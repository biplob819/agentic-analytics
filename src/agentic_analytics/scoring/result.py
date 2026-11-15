"""Result match against the stored gold (SCR-02, Tech Spec §6.1, EK L-38).

Extra helpful columns pass, extra or missing rows do not. The column mapping is ours, because
`sql_match` falls back to position when the column counts are equal but the names differ:

1. every gold name present among the predicted names (case-insensitive) -> map by name;
2. else, with at most `MAX_PREDICTED_COLUMNS` predicted and `MAX_GOLD_COLUMNS` gold columns,
   try assignments of distinct predicted columns to the gold columns. A predicted column is a
   candidate for a gold column only when its non-numeric values are the same multiset and its
   numbers could match (sorted pairing within tolerance, or exact for exact columns). At most
   `MAX_ASSIGNMENTS` are tried; any one that matches passes;
3. beyond those limits, `sql_match(..., allow_extra_columns=True)` decides on its own (per-column
   exactness is then not enforced).

Each tried assignment builds a predicted table with the gold's column names in the gold's order
and calls `sql_match` by name. Tolerance comes from the question: numbers in gold columns that
`Tolerance.columns` does not cover are compared exactly, by turning them into canonical strings
on both sides (an integer, or an integral float, as ``str(int(v))``; any other float as its
``repr``), which `sql_match` compares as text. `ordered` is set on the gold from the question.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from evalkit.scorers import sql_match

from agentic_analytics.schemas import Execution, Probe, Question, ResultSet, Tolerance
from agentic_analytics.scoring.execute import ScoringError

MAX_PREDICTED_COLUMNS = 8
MAX_GOLD_COLUMNS = 6
MAX_ASSIGNMENTS = 200


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def canonical(v: Any) -> Any:
    """A number as exact-match text; anything else unchanged."""
    if not _is_num(v):
        return v
    if isinstance(v, int):
        return str(v)
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _close(a: float, b: float, tol: Tolerance) -> bool:
    d = abs(a - b)
    return d <= tol.abs or d <= tol.rel * max(abs(a), abs(b))


class _Column:
    """What a column's values must agree on for any row matching to exist."""

    def __init__(self, values: Sequence[Any]) -> None:
        nums = [v for v in values if _is_num(v)]
        self.others = Counter(
            v.rstrip() if isinstance(v, str) else v for v in values if not _is_num(v)
        )
        self.sorted_nums = sorted(float(v) for v in nums)
        self.exact = Counter(canonical(v) for v in nums)

    def fits(self, gold: _Column, tol: Tolerance | None) -> bool:
        if self.others != gold.others or len(self.sorted_nums) != len(gold.sorted_nums):
            return False
        if tol is None:
            return self.exact == gold.exact
        pairs = zip(self.sorted_nums, gold.sorted_nums, strict=True)
        return all(_close(a, b, tol) for a, b in pairs)


def _by_name(predicted: ResultSet, gold: ResultSet) -> list[int] | None:
    pcols = [c.casefold() for c in predicted.columns]
    gcols = [c.casefold() for c in gold.columns]
    if set(gcols) <= set(pcols) and len(set(pcols)) == len(pcols):
        return [pcols.index(c) for c in gcols]
    return None


def _assignments(
    predicted: ResultSet, gold: ResultSet, covered: Callable[[int], bool], tol: Tolerance
) -> list[list[int]]:
    named = _by_name(predicted, gold)
    if named is not None:
        return [named]
    pcols = [c.casefold() for c in predicted.columns]
    gcols = [c.casefold() for c in gold.columns]
    pprof = [_Column([r[i] for r in predicted.rows]) for i in range(len(pcols))]
    gprof = [_Column([r[j] for r in gold.rows]) for j in range(len(gcols))]
    cands: list[list[int]] = []
    for j, g in enumerate(gprof):
        fit = [i for i, p in enumerate(pprof) if p.fits(g, tol if covered(j) else None)]
        fit.sort(key=lambda i: pcols[i] != gcols[j])  # same name first
        cands.append(fit)
    out: list[list[int]] = []

    def walk(j: int, used: list[int]) -> None:
        if len(out) >= MAX_ASSIGNMENTS:
            return
        if j == len(gcols):
            out.append(list(used))
            return
        for i in cands[j]:
            if i not in used:
                used.append(i)
                walk(j + 1, used)
                used.pop()

    walk(0, [])
    return out


def _reason(details: dict[str, Any]) -> str:
    reason = str(details.pop("reason", "mismatch"))
    extra = ", ".join(f"{k}={details[k]}" for k in sorted(details))
    return f"{reason} ({extra})" if extra else reason


def match_result(
    execution: Execution | None,
    gold: ResultSet,
    *,
    tolerance: Tolerance,
    ordered: bool,
) -> tuple[bool, str | None]:
    """Compare an execution with a gold result: (matched, reason when not)."""
    if execution is None:
        return False, "no SQL to execute"
    if execution.error is not None or execution.result is None:
        return False, f"execution failed: {execution.error}"
    if execution.truncated:
        return False, f"result truncated: {len(execution.result.rows)} of {execution.rows_total}"
    predicted = execution.result
    n_gold, n_pred = len(gold.columns), len(predicted.columns)
    if len(predicted.rows) != len(gold.rows):
        return False, f"row_count (gold={len(gold.rows)}, predicted={len(predicted.rows)})"
    if n_pred < n_gold:
        return False, f"column_count (gold={n_gold}, predicted={n_pred})"
    rel, abs_ = tolerance.rel, tolerance.abs
    if _by_name(predicted, gold) is None and (
        n_pred > MAX_PREDICTED_COLUMNS or n_gold > MAX_GOLD_COLUMNS
    ):
        target = ResultSet(columns=gold.columns, rows=gold.rows, ordered=ordered)
        res = sql_match(predicted, target, rel_tol=rel, abs_tol=abs_, allow_extra_columns=True)
        return (True, None) if res.value == 1.0 else (False, _reason(dict(res.details)))

    names = None if tolerance.columns is None else {c.casefold() for c in tolerance.columns}

    def covered(j: int) -> bool:
        return names is None or gold.columns[j].casefold() in names

    conv: list[Callable[[Any], Any]] = [
        (lambda v: v) if covered(j) else canonical for j in range(n_gold)
    ]
    target = ResultSet(
        columns=gold.columns,
        rows=tuple(tuple(conv[j](v) for j, v in enumerate(r)) for r in gold.rows),
        ordered=ordered,
    )
    first: str | None = None
    for order in _assignments(predicted, gold, covered, tolerance):
        mapped = ResultSet(
            columns=gold.columns,
            rows=tuple(tuple(conv[j](r[i]) for j, i in enumerate(order)) for r in predicted.rows),
        )
        res = sql_match(mapped, target, rel_tol=rel, abs_tol=abs_)
        if res.value == 1.0:
            return True, None
        if first is None:
            first = _reason(dict(res.details))
    if first is None:
        return False, f"no_matching_columns (gold={n_gold}, predicted={n_pred})"
    return False, first


def result_match(
    execution: Execution | None,
    gold: ResultSet | None,
    question: Question | Probe,
) -> tuple[bool | None, str | None]:
    """SCR-02: None for clarify-expected questions and probes without gold; else a match."""
    if isinstance(question, Question) and question.expected == "clarify":
        return None, None
    if isinstance(question, Probe):
        if gold is None:
            return None, None
        return match_result(execution, gold, tolerance=Tolerance(), ordered=False)
    if gold is None:
        raise ScoringError(f"no gold result for {question.id}")
    return match_result(execution, gold, tolerance=question.tolerance, ordered=question.ordered)
