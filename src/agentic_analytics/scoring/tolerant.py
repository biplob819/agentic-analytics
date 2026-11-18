"""Tolerant result match through EvalKit's shape equivalences (OWNER D-81; V5.1 MON-08, D-120).

The headline score accepts four shapes of a right answer that the strict, pre-registered match
(`result.py`) rejects: `percent_scale`, `pivot`, `empty_check` and `period_number`. The rules
themselves are EvalKit's (`evalkit.scorers.sql_match(..., question=, equivalences=)`, B5.15,
lifted from this module and made stricter, D-91/D-92); this module is the thin adapter that
keeps this project's pre-registered conditions around them:

- **Eligibility.** Only answer-expected questions, with gold, whose answer executed without an
  error or truncation, and only after the strict match failed (`score.py`). Clarify-expected
  questions and probes are never rescued.
- **Which rules a question may use.** `empty_check` only on an `expects_empty` question (what
  EvalKit's "naming empty_check" stands for); `percent_scale` only when the question's
  tolerance is a percent-unit one (D-51: `abs` at least 0.01); `pivot` and `period_number`
  always. EvalKit reads the question text for the rest (a percentage asked for, months or
  quarters named, the years named, fiscal periods).
- **Per-column exactness.** `Tolerance.columns` names the gold columns the tolerance covers;
  every other number must match exactly, as in the strict match. EvalKit takes one tolerance
  per call, so an uncovered column is handed over as exact-match text (`result.canonical`) on
  both sides, except a column of small integers (|v| < 1/rel), which tolerance cannot tell
  from an exact match and which EvalKit's period rule must read as numbers (month 1-12,
  quarter 1-4); a predicted number there that is not such an integer becomes text too, so it
  can only fail. pivot's gold has one value column: it gets the question's tolerance when
  covered, else an exact one (`rel_tol=0`, `abs_tol=0`).
- **Column mapping.** The strict match maps predicted onto gold columns by name, else by
  values (`result.py`). The column rules convert values before they can match, so the mapping
  is tried over the columns that *could* match once converted (same text cells and as many
  numbers, or no numbers at all for a period column; the same name first), each assignment
  handed to EvalKit as a predicted table in the gold's column names and order, so EvalKit
  compares it by name. pivot and empty_check read the predicted column names, so they get the
  predicted table as it is.

The first rescue wins, in EvalKit's order (empty_check, pivot, percent_scale, period_number,
percent_scale with period_number). Rescoring every stored run through this adapter changes one
verdict, accepted by OWNER D-118 (the fix runs' GPT + descriptions q-020 trial 1: EvalKit's
empty_check takes a no-data zero only in a count-named column), and no other
(`docs/measurements/evalkit-0.4-parity-2026-11-18.json`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterator
from typing import Any

from evalkit.scorers import ScoreResult, sql_match

from agentic_analytics.schemas import Equivalence, Execution, Question, ResultSet, Tolerance
from agentic_analytics.scoring.result import (
    MAX_ASSIGNMENTS,
    _by_name,
    _Column,
    _is_num,
    canonical,
)

PERCENT_ABS = 0.01  # D-51: a percent-unit column's absolute tolerance
WHOLE_TABLE: tuple[Equivalence, ...] = ("empty_check", "pivot")
PER_COLUMN: tuple[Equivalence, ...] = ("percent_scale", "period_number")


def equivalences_for(question: Question) -> tuple[Equivalence, ...]:
    """The EvalKit equivalences this question may be rescued by (its pre-registered terms)."""
    names: list[Equivalence] = []
    if question.expects_empty:
        names.append("empty_check")
    names.append("pivot")
    if question.tolerance.abs >= PERCENT_ABS:
        names.append("percent_scale")
    names.append("period_number")
    return tuple(names)


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
    names = equivalences_for(question)
    predicted, tol = execution.result, question.tolerance
    whole: list[Equivalence] = [n for n in WHOLE_TABLE if n in names]
    rel, abs_ = _pivot_tolerance(gold, tol)
    target = ResultSet(columns=gold.columns, rows=gold.rows, ordered=question.ordered)
    rules = _rescue(predicted, target, rel, abs_, question.text, whole)
    if rules:
        return rules
    per_column: list[Equivalence] = [n for n in PER_COLUMN if n in names]
    for mapped, encoded in _mapped_tables(predicted, gold, tol, question.ordered):
        rules = _rescue(mapped, encoded, tol.rel, tol.abs, question.text, per_column)
        if rules:
            return rules
    return ()


def _rescue(
    predicted: ResultSet,
    gold: ResultSet,
    rel: float,
    abs_: float,
    text: str,
    names: Collection[Equivalence],
) -> tuple[Equivalence, ...]:
    """EvalKit's rules that rescue `predicted`, or () (also when EvalKit's own strict match
    passes: the strict verdict is `result.py`'s, so that is not a rescue)."""
    res: ScoreResult = sql_match(
        predicted,
        gold,
        rel_tol=rel,
        abs_tol=abs_,
        allow_extra_columns=True,
        question=text,
        equivalences=names,
    )
    if res.value != 1.0 or res.details.get("correct_strict") is not False:
        return ()
    return tuple(res.details["equivalences"])


def _covered(gold: ResultSet, tol: Tolerance) -> list[bool]:
    names = None if tol.columns is None else {c.casefold() for c in tol.columns}
    return [names is None or c.casefold() in names for c in gold.columns]


def _pivot_tolerance(gold: ResultSet, tol: Tolerance) -> tuple[float, float]:
    """pivot compares one value column: the question's tolerance when it covers every numeric
    gold column, else an exact match (rel 0, abs 0)."""
    covered = _covered(gold, tol)
    numeric = [j for j in range(len(gold.columns)) if any(_is_num(r[j]) for r in gold.rows)]
    if all(covered[j] for j in numeric):
        return tol.rel, tol.abs
    return 0.0, 0.0


def _small(v: Any, bound: float) -> bool:
    return _is_num(v) and math.isfinite(float(v)) and float(v).is_integer() and abs(v) < bound


def _small_integers(values: list[Any], bound: float) -> bool:
    return bool(values) and all(_small(v, bound) for v in values)


def _keep(v: Any) -> Any:
    return v


def _encoders(
    gold: ResultSet, tol: Tolerance
) -> tuple[list[Callable[[Any], Any]], list[Callable[[Any], Any]]]:
    """(gold cell encoder, predicted cell encoder) per gold column (the module docstring)."""
    bound = math.inf if tol.rel == 0 else 1 / tol.rel
    covered = _covered(gold, tol)

    def as_small(v: Any) -> Any:
        return canonical(v) if _is_num(v) and not _small(v, bound) else v

    g_enc: list[Callable[[Any], Any]] = []
    p_enc: list[Callable[[Any], Any]] = []
    for j in range(len(gold.columns)):
        if covered[j]:
            g_enc.append(_keep)
            p_enc.append(_keep)
        elif tol.abs < 1 and _small_integers([r[j] for r in gold.rows], bound):
            g_enc.append(_keep)
            p_enc.append(as_small)
        else:
            g_enc.append(canonical)
            p_enc.append(canonical)
    return g_enc, p_enc


def _scaled(p: _Column, g: _Column) -> bool:
    """The same text cells and as many numbers: a fraction that may fit once scaled."""
    return p.others == g.others and len(p.sorted_nums) == len(g.sorted_nums)


def _periods(p: _Column, g: _Column) -> bool:
    """No numbers beside an all-numeric gold column: period starts that may become month or
    quarter numbers."""
    return not p.sorted_nums and bool(g.sorted_nums) and not g.others


def _assignments(predicted: ResultSet, gold: ResultSet, tol: Tolerance) -> list[list[int]]:
    """Column assignments to try: by name when every gold name is predicted; else each gold
    column takes a predicted column that fits it as the strict match would (its tolerance when
    covered, exactly when not), or that a column rule could convert into it (a scaled
    fraction for a covered column, period starts for a covered or small-integer column).
    Strict fits come first, then the same name."""
    named = _by_name(predicted, gold)
    if named is not None:
        return [named]
    pcols = [c.casefold() for c in predicted.columns]
    gcols = [c.casefold() for c in gold.columns]
    pprof = [_Column([r[i] for r in predicted.rows]) for i in range(len(pcols))]
    gprof = [_Column([r[j] for r in gold.rows]) for j in range(len(gcols))]
    covered = _covered(gold, tol)
    bound = math.inf if tol.rel == 0 else 1 / tol.rel
    cands: list[list[int]] = []
    for j, g in enumerate(gprof):
        small = _small_integers([r[j] for r in gold.rows], bound)
        fit: list[tuple[bool, bool, int]] = []
        for i, p in enumerate(pprof):
            strict = p.fits(g, tol if covered[j] else None)
            convert = (covered[j] and (_scaled(p, g) or _periods(p, g))) or (
                small and _periods(p, g)
            )
            if strict or convert:
                fit.append((not strict, pcols[i] != gcols[j], i))
        cands.append([i for *_, i in sorted(fit)])
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


def _mapped_tables(
    predicted: ResultSet, gold: ResultSet, tol: Tolerance, ordered: bool
) -> Iterator[tuple[ResultSet, ResultSet]]:
    """(predicted in the gold's column names and order, the encoded gold) per column
    assignment, each cell encoded for per-column exactness."""
    if len(predicted.rows) != len(gold.rows) or not gold.rows:
        return
    g_enc, p_enc = _encoders(gold, tol)
    target = ResultSet(
        columns=gold.columns,
        rows=tuple(tuple(g_enc[j](v) for j, v in enumerate(r)) for r in gold.rows),
        ordered=ordered,
    )
    for order in _assignments(predicted, gold, tol):
        rows = tuple(tuple(p_enc[j](r[i]) for j, i in enumerate(order)) for r in predicted.rows)
        yield ResultSet(columns=gold.columns, rows=rows), target
