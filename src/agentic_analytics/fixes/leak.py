"""Leak check for the `examples` fix (FIX-03, Tech Spec §7, OWNER D-13, C5).

Three rules keep the examples from paraphrasing, mirroring or answering the benchmark:

1. **Text.** Each question is normalised (casefolded; every run of characters other than
   letters and digits becomes one space; so "semi-detached" is "semi detached" and "£1,000,000"
   is "1 000 000") and cut into word 3-grams. The overlap of an example with a benchmark
   question is the number of shared distinct 3-grams divided by the smaller of the two 3-gram
   sets, so a short benchmark question copied inside a longer example counts as fully shared
   (stricter than dividing by the example's 3-grams alone, which §7 names). An overlap above
   `MAX_OVERLAP` (0.5) with any benchmark question, ambiguous ones included, is a leak.
2. **Structure (D-52).** Each SQL is reduced to a skeleton from sqlglot's token stream: string
   and number literals become `?`, identifiers (columns, tables, CTE names) become `c`,
   `AS <alias>` is dropped, function names and keywords stay. An example leaks when its skeleton
   equals, or shares more than `MAX_OVERLAP` of its distinct 6-grams (over the smaller set)
   with, the skeleton of any benchmark gold SQL, any ambiguous question's interpretation or any
   probe gold (ClickHouse versions). This catches an example that demonstrates a scored output
   formula (counts per year with a percentage change, a top-N with a tie-break and LIMIT) even
   about other places and years. Calibrated on the first draft, whose five examples scored
   0.74-1.00 against the bank; the replacements score at most 0.50 (ex-1 against
   p-id03/04/05, generic `FROM ... WHERE town` boilerplate; allowed, as the rule is a strict `>`).
3. **Result.** No example's result set may equal any benchmark gold result: S2.1's result
   matcher at an exact tolerance, unordered, in both directions with equal column counts (so a
   renamed column does not hide a copy). This needs the examples executed on ClickHouse, so it
   runs only live (`result_leaks_live`); `result_leaks` is the pure comparison.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot.tokenizer_core import TokenType

from agentic_analytics.fixes.examples import Example
from agentic_analytics.schemas import (
    Execution,
    GoldResult,
    Probe,
    Question,
    ResultSet,
    Tolerance,
)
from agentic_analytics.scoring.result import match_result

MAX_OVERLAP = 0.5
SKELETON_N = 6
_LITERALS = frozenset({TokenType.STRING, TokenType.NUMBER})
_NAMES = frozenset({TokenType.VAR, TokenType.IDENTIFIER})
EXAMPLE_ROW_CAP = 200
_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalise(text: str) -> list[str]:
    return [w for w in _NON_WORD.sub(" ", text.casefold()).split() if w]


def trigrams(text: str) -> frozenset[tuple[str, str, str]]:
    words = normalise(text)
    return frozenset(zip(words, words[1:], words[2:], strict=False))


def overlap(a: str, b: str) -> float:
    """Shared distinct word 3-grams over the smaller set (0.0 when either has none)."""
    ga, gb = trigrams(a), trigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


@dataclass(frozen=True)
class Leak:
    example_id: str
    question_id: str
    rule: str  # "text", "structure" or "result"
    detail: str

    def __str__(self) -> str:
        return f"{self.example_id} leaks {self.question_id} ({self.rule}): {self.detail}"


def text_leaks(
    examples: Iterable[Example], questions: Iterable[Question], *, limit: float = MAX_OVERLAP
) -> list[Leak]:
    qs = list(questions)
    out: list[Leak] = []
    for ex in examples:
        for q in qs:
            share = overlap(ex.question, q.text)
            if share > limit:
                out.append(Leak(ex.id, q.id, "text", f"3-gram overlap {share:.2f} > {limit}"))
    return out


def skeleton(sql: str, engine: str = "clickhouse") -> tuple[str, ...]:
    """The SQL's token skeleton: literals `?`, identifiers `c`, aliases dropped, lowercase."""
    tokens = sqlglot.tokenize(sql.replace("{as_of}", "2000-01-01"), read=engine)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t.token_type == TokenType.ALIAS:
            i += 2  # AS and the alias
            continue
        if t.token_type in _LITERALS:
            out.append("?")
        elif t.token_type in _NAMES and not (nxt and nxt.token_type == TokenType.L_PAREN):
            out.append("c")
        else:
            out.append(t.text.casefold())
        i += 1
    return tuple(out)


def skeleton_overlap(a: Sequence[str], b: Sequence[str], n: int = SKELETON_N) -> float:
    """1.0 for identical skeletons; else shared distinct n-grams over the smaller set."""
    if tuple(a) == tuple(b):
        return 1.0
    ga = {tuple(a[i : i + n]) for i in range(len(a) - n + 1)}
    gb = {tuple(b[i : i + n]) for i in range(len(b) - n + 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def gold_sqls(
    questions: Iterable[Question], probes: Iterable[Probe] = (), engine: str = "clickhouse"
) -> list[tuple[str, str]]:
    """(id, SQL) for every gold query, interpretation and probe gold on one engine."""
    out: list[tuple[str, str]] = []
    for q in questions:
        if q.gold_sql is not None and engine in q.gold_sql:
            out.append((q.id, q.gold_sql[engine]))  # type: ignore[index]
        for n, interp in enumerate(q.interpretations):
            if engine in interp:
                out.append((f"{q.id}/interpretations[{n}]", interp[engine]))  # type: ignore[index]
    for p in probes:
        if p.gold_sql is not None and engine in p.gold_sql:
            out.append((p.id, p.gold_sql[engine]))  # type: ignore[index]
    return out


def structural_leaks(
    examples: Iterable[Example],
    questions: Iterable[Question],
    probes: Iterable[Probe] = (),
    *,
    limit: float = MAX_OVERLAP,
) -> list[Leak]:
    targets = [(gid, skeleton(sql)) for gid, sql in gold_sqls(questions, probes)]
    out: list[Leak] = []
    for ex in examples:
        sk = skeleton(ex.sql)
        for gid, gsk in targets:
            share = skeleton_overlap(sk, gsk)
            if share > limit:
                out.append(
                    Leak(ex.id, gid, "structure", f"SQL skeleton overlap {share:.2f} > {limit}")
                )
    return out


def _execution(result: ResultSet) -> Execution:
    return Execution(
        engine="clickhouse",
        sql="<example>",
        result=result,
        rows_total=len(result.rows),
        truncated=False,
        query_id=None,
        elapsed_ms=0,
        error=None,
    )


def same_result(a: ResultSet, b: ResultSet) -> bool:
    """Equal as result sets: same column count, rows matched exactly, order ignored."""
    if len(a.columns) != len(b.columns):
        return False
    exact = Tolerance()
    ok_ab, _ = match_result(_execution(a), b, tolerance=exact, ordered=False)
    ok_ba, _ = match_result(_execution(b), a, tolerance=exact, ordered=False)
    return ok_ab and ok_ba


def result_leaks(
    example_results: Mapping[str, ResultSet], gold: Iterable[GoldResult]
) -> list[Leak]:
    golds = list(gold)
    out: list[Leak] = []
    for ex_id, result in sorted(example_results.items()):
        for g in golds:
            if same_result(result, g.result):
                out.append(
                    Leak(ex_id, g.question_id, "result", f"same result set as {g.engine} gold")
                )
    return out


def execute_examples(
    executor: object, examples: Sequence[Example], *, row_cap: int = EXAMPLE_ROW_CAP
) -> tuple[dict[str, ResultSet], list[str]]:
    """Run each example on a read-only executor: results by id, and problems (failed, empty or
    truncated examples are not verified)."""
    results: dict[str, ResultSet] = {}
    problems: list[str] = []
    for ex in examples:
        execution: Execution = executor.execute(ex.sql, row_cap=row_cap)  # type: ignore[attr-defined]
        if execution.error is not None or execution.result is None:
            problems.append(f"{ex.id}: execution failed ({execution.error})")
            continue
        if execution.truncated:
            problems.append(f"{ex.id}: more than {row_cap} rows")
            continue
        if not execution.result.rows:
            problems.append(f"{ex.id}: no rows")
            continue
        results[ex.id] = execution.result
    return results, problems


def result_leaks_live(
    executor: object, examples: Sequence[Example], gold: Iterable[GoldResult]
) -> list[str]:
    """Live half of FIX-03 (and the FIX-02 verification): every example runs and returns rows,
    and none of its result sets equals a gold result. Returns the problems, empty when clean."""
    results, problems = execute_examples(executor, examples)
    return problems + [str(leak) for leak in result_leaks(results, gold)]


__all__ = [
    "EXAMPLE_ROW_CAP",
    "MAX_OVERLAP",
    "SKELETON_N",
    "Leak",
    "execute_examples",
    "gold_sqls",
    "normalise",
    "overlap",
    "result_leaks",
    "result_leaks_live",
    "same_result",
    "skeleton",
    "skeleton_overlap",
    "structural_leaks",
    "text_leaks",
    "trigrams",
]
