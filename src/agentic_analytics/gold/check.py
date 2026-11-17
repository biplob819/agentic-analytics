"""The gold check (Q2.2: GLD-01..GLD-06, Tech Spec §5 steps 5-7, US9, H1, H5).

`run_gold_check(pack, questions, executors, as_of=..., snapshot_sha256=...)`:

1. **Lint (GLD-04).** Q2.1's writing-rule lint (`lint_questions`: exact functions, enum rules,
   uppercase literals, the as-of placeholder, top-N shape ...) over the whole bank. Any issue
   fails its question.
2. **Run (GLD-01).** Every answer-expected question's gold SQL, with `'{as_of}'` bound, runs on
   each configured engine through N1.2's read-only executors with a 200-row cap. An error, more
   than 200 rows, no rows (unless `expects_empty`) or rows when `expects_empty` fails it.
3. **Determinism (GLD-04).** The ClickHouse query runs a second time (the first configured
   engine when ClickHouse is not configured); the two results must be identical (same columns;
   same rows, compared as multisets unless the question is `ordered`).
4. **Cross-engine (GLD-02).** With both engines, the Snowflake result must match the ClickHouse
   result with S2.1's matcher (`scoring.result.match_result`: column mapping by name, else by
   value, the question's tolerance on `Tolerance.columns` and exact elsewhere, the question's
   `ordered`) and have the same number of columns. Single-engine mode skips this step and is
   stamped `single_engine`.
5. **Top-N ties (GLD-03).** For a question with `top_n = n`, the outermost `LIMIT n` is rewritten
   to `LIMIT n+1` from sqlglot's token stream (the rest of the text is kept byte for byte) and run
   on the determinism engine; the ranking value is the output column named by the first ORDER BY
   key (by alias or name, else by identical expression). If n+1 rows come back, the n-th and
   (n+1)-th ranking values must differ beyond the question's tolerance when `Tolerance.columns`
   covers the ranking column (exactly otherwise). For an `ordered` question every adjacent pair
   inside the top n must be separated the same way, or the gold order is ambiguous.
6. **Selection (GLD-05).** From the passing questions, `quota` per category (pack.yaml: 17 x 4,
   16 x 2) is drawn with `random.Random(f"{seed}:{category}")` over the ids in sorted order, so
   the selection depends only on the seed and the passing set. Too few passing questions in a
   category is a failure.
6b. **Probes.** Every probe with gold SQL runs on each configured engine its backends are scored
   on (MCP agents: ClickHouse; Cortex: Snowflake); it must return 1-200 rows, and with both
   engines the results must match exactly (as probes are scored). Its gold is stored as
   `gold/<engine>/<probe id>.json` (`run.gold.ProbeGold`), listed in the record's `probes`, and
   the runner passes it to scoring.
7. **Stamp (GLD-06).** `gold/<engine>/<qid>.json` (`GoldResult`, `sql_hash` = sha256 of the
   executed, as-of-bound SQL) for every gold query that ran cleanly, then the record in R3.1's
   format (`run.gold.GoldCheck`, documented in `run/gold.py`) twice: `gold/check-<ts>.json`
   (history, never overwritten) and `gold/gold_check.json` (the one R3.1's runner reads), with
   the as-of date and `gold_check_id = run.gold.compute_gold_check_id(selected questions, their
   gold, snapshot sha256, as_of=, probes=)`: only the SELECTED questions count. A run whose as-of
   date differs from the record's is refused (exit 1).

Any failure gives `status: "fail"` and exit 1 (§5 step 5.5): a failing question is fixed,
reworded, moved or dropped by an owner ruling and the check is rerun; it is never silently
skipped. `only=<id>` checks one question (QST-07, US8) without selecting or writing anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.tokenizer_core import TokenType

from agentic_analytics.packs import Pack, PackError
from agentic_analytics.questions import (
    BankError,
    bind_as_of,
    context_for,
    lint_probes,
    lint_questions,
    load_probes,
)
from agentic_analytics.run.gold import (
    GOLD_CHECK_FILE,
    AnyGold,
    GoldCheck,
    ProbeGold,
    compute_gold_check_id,
    probe_engines,
    write_gold_check,
)
from agentic_analytics.schemas import (
    ENGINES,
    Engine,
    Execution,
    GoldResult,
    Probe,
    Question,
    ResultSet,
    Tolerance,
)
from agentic_analytics.schemas.pack import CATEGORIES
from agentic_analytics.scoring.result import match_result

GOLD_ROW_CAP = 200  # Tech Spec §5 step 2: at most 200 gold rows
SELECTION_SEED = 20261115  # §5 step 6: a fixed seed


class GoldConfigError(RuntimeError):
    """The gold check cannot run: no engine, a bad pack directory, an unknown --only (exit 2)."""


# -- top-N rewrite and ranking column -------------------------------------------------------


def rewrite_limit(sql: str, engine: Engine, n: int) -> str:
    """`sql` with its outermost `LIMIT n` replaced by `LIMIT n+1`; every other byte is kept.

    The outermost LIMIT is the last LIMIT token at parenthesis depth 0 in sqlglot's token
    stream; its literal must be `n`. Raises ValueError otherwise.
    """
    tokens = sqlglot.tokenize(sql, read=engine)
    depth = 0
    found: tuple[int, int, str] | None = None
    for i, t in enumerate(tokens):
        if t.token_type == TokenType.L_PAREN:
            depth += 1
        elif t.token_type == TokenType.R_PAREN:
            depth -= 1
        elif t.token_type == TokenType.LIMIT and depth == 0:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is None or nxt.token_type != TokenType.NUMBER:
                raise ValueError("the outermost LIMIT is not a number literal")
            found = (nxt.start, nxt.end, nxt.text)
    if found is None:
        raise ValueError("no outermost LIMIT to rewrite")
    start, end, text = found
    if text != str(n):
        raise ValueError(f"the outermost LIMIT is {text}, not top_n {n}")
    return sql[:start] + str(n + 1) + sql[end + 1 :]


def ranking_index(sql: str, engine: Engine) -> int:
    """The position of the output column named by the outermost query's first ORDER BY key: an
    output alias or column of that name, else a projection with the same expression."""
    root = sqlglot.parse_one(sql, read=engine)
    if not isinstance(root, exp.Query):
        raise ValueError("not a query")
    order = root.args.get("order")
    if not isinstance(order, exp.Order) or not order.expressions:
        raise ValueError("no ORDER BY on the outermost query")
    key = order.expressions[0]
    key = key.this if isinstance(key, exp.Ordered) else key
    names = [n.casefold() for n in root.named_selects]
    if isinstance(key, exp.Column) and not key.table and key.name.casefold() in names:
        return names.index(key.name.casefold())
    if isinstance(root, exp.Select):
        wanted = key.sql(dialect=engine)
        for idx, proj in enumerate(root.expressions):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if inner.sql(dialect=engine) == wanted:
                return idx
    raise ValueError(f"the ranking key {key.sql(dialect=engine)!r} is not an output column")


# -- comparisons -----------------------------------------------------------------------------


def _row_key(row: Sequence[Any]) -> str:
    return json.dumps(list(row), sort_keys=True, default=str)


def identical(a: ResultSet, b: ResultSet, *, ordered: bool) -> bool:
    """Same columns and the same rows (a multiset unless `ordered`), compared exactly."""
    if tuple(a.columns) != tuple(b.columns):
        return False
    ka, kb = [_row_key(r) for r in a.rows], [_row_key(r) for r in b.rows]
    return ka == kb if ordered else Counter(ka) == Counter(kb)


def _as_execution(engine: Engine, result: ResultSet) -> Execution:
    return Execution(
        engine=engine,
        sql="<gold>",
        result=result,
        rows_total=len(result.rows),
        truncated=False,
        query_id=None,
        elapsed_ms=0,
        error=None,
    )


def cross_engine_mismatch(q: Question, ch: ResultSet, sf: ResultSet) -> str | None:
    """Why the Snowflake gold does not match the ClickHouse gold, or None (GLD-02)."""
    if len(ch.columns) != len(sf.columns):
        return f"column_count (clickhouse={len(ch.columns)}, snowflake={len(sf.columns)})"
    ok, reason = match_result(
        _as_execution("snowflake", sf), ch, tolerance=q.tolerance, ordered=q.ordered
    )
    return None if ok else (reason or "mismatch")


# -- one question ----------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionOutcome:
    question: Question
    failures: tuple[str, ...]
    gold: Mapping[Engine, GoldResult] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(executor: Any, sql: str) -> Execution:
    return executor.execute(sql, row_cap=GOLD_ROW_CAP)


def _problem(ex: Execution, q: Question) -> str | None:
    if ex.error is not None or ex.result is None:
        return f"execution failed ({ex.error})"
    if ex.truncated:
        return f"more than {GOLD_ROW_CAP} gold rows ({ex.rows_total})"
    if not ex.result.rows and not q.expects_empty:
        return "no rows (and expects_empty is false)"
    if ex.result.rows and q.expects_empty:
        return f"{len(ex.result.rows)} rows but expects_empty is true"
    return None


def determinism_engine(executors: Mapping[Engine, Any]) -> Engine:
    return "clickhouse" if "clickhouse" in executors else next(iter(_engines(executors)))


def _engines(executors: Mapping[Engine, Any]) -> tuple[Engine, ...]:
    return tuple(e for e in ENGINES if executors.get(e) is not None)


def check_question(
    q: Question,
    executors: Mapping[Engine, Any],
    *,
    as_of: date,
    lint: Sequence[str] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> QuestionOutcome:
    """GLD-01..04 for one question; clarify-expected questions are linted only."""
    failures = [f"{q.id}: lint {msg}" for msg in lint]
    if q.expected != "answer" or q.gold_sql is None:
        return QuestionOutcome(q, tuple(failures))
    engines = _engines(executors)
    det = determinism_engine(executors)
    results: dict[Engine, ResultSet] = {}
    gold: dict[Engine, GoldResult] = {}
    bound: dict[Engine, str] = {}
    for engine in engines:
        try:
            bound[engine] = bind_as_of(q.gold_sql[engine], as_of)
        except (KeyError, ValueError) as exc:
            failures.append(f"{q.id} [{engine}]: cannot bind the as-of date ({exc})")
            continue
        ex = _run(executors[engine], bound[engine])
        problem = _problem(ex, q)
        if problem:
            failures.append(f"{q.id} [{engine}]: {problem}")
            continue
        assert ex.result is not None
        result = ResultSet(columns=ex.result.columns, rows=ex.result.rows, ordered=q.ordered)
        results[engine] = result
        gold[engine] = GoldResult(
            schema_version="1.0",
            question_id=q.id,
            engine=engine,
            sql_hash=_sha(bound[engine]),
            result=result,
            elapsed_ms=ex.elapsed_ms,
            executed_at=now(),
        )
    if det in results:
        again = _run(executors[det], bound[det])
        if again.error is not None or again.result is None:
            failures.append(f"{q.id} [{det}]: second run failed ({again.error})")
        elif again.truncated or not identical(results[det], again.result, ordered=q.ordered):
            failures.append(f"{q.id} [{det}]: not deterministic (two runs differ)")
    if "clickhouse" in results and "snowflake" in results:
        why = cross_engine_mismatch(q, results["clickhouse"], results["snowflake"])
        if why:
            failures.append(f"{q.id}: cross-engine mismatch: {why}")
    if q.top_n is not None and det in results:
        failures += _tie_failures(q, executors[det], det, bound[det], q.top_n)
    return QuestionOutcome(q, tuple(failures), gold)


def _covered(column: str, tol: Tolerance) -> bool:
    return tol.columns is None or column.casefold() in {c.casefold() for c in tol.columns}


def separated(a: Any, b: Any, tol: Tolerance | None) -> bool:
    """True when two ranking values differ beyond the tolerance (exactly when `tol` is None or
    either value is not a number)."""
    nums = all(isinstance(v, int | float) and not isinstance(v, bool) for v in (a, b))
    if tol is None or not nums:
        return a != b
    d = abs(float(a) - float(b))
    return d > tol.abs and d > tol.rel * max(abs(float(a)), abs(float(b)))


def _tie_failures(q: Question, executor: Any, engine: Engine, sql: str, n: int) -> list[str]:
    """GLD-03: rows n and n+1 must differ beyond the question's tolerance for the ranking column
    (exactly when the column is not covered); for an ordered question every adjacent pair inside
    the top n must too, or the gold order is ambiguous."""
    try:
        wider = rewrite_limit(sql, engine, n)
        j = ranking_index(sql, engine)
    except Exception as exc:
        return [f"{q.id} [{engine}]: tie check impossible ({exc})"]
    ex = _run(executor, wider)
    if ex.error is not None or ex.result is None:
        return [f"{q.id} [{engine}]: tie check query failed ({ex.error})"]
    if j >= len(ex.result.columns):
        return [f"{q.id} [{engine}]: the ranking column is missing from the result"]
    rank = ex.result.columns[j]
    tol = q.tolerance if _covered(rank, q.tolerance) else None
    within = "equal" if tol is None else "within tolerance"
    rows = ex.result.rows
    out: list[str] = []
    if len(rows) > n and not separated(rows[n - 1][j], rows[n][j], tol):
        out.append(
            f"{q.id} [{engine}]: tie at the LIMIT cut ({rank} {rows[n - 1][j]!r} and "
            f"{rows[n][j]!r} at rows {n} and {n + 1} are {within})"
        )
    if q.ordered:
        top = rows[:n]
        for i in range(len(top) - 1):
            if not separated(top[i][j], top[i + 1][j], tol):
                out.append(
                    f"{q.id} [{engine}]: ambiguous order inside the top {n} ({rank} "
                    f"{top[i][j]!r} and {top[i + 1][j]!r} at rows {i + 1} and {i + 2} are "
                    f"{within})"
                )
    return out


# -- probes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeOutcome:
    probe: Probe
    failures: tuple[str, ...]
    gold: Mapping[Engine, ProbeGold] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures


def check_probe(
    p: Probe,
    executors: Mapping[Engine, Any],
    *,
    as_of: date,
    lint: Sequence[str] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ProbeOutcome:
    """A probe's gold runs on each configured engine its backends are scored on; it must return
    rows, and with both engines the results must match exactly (as probes are scored)."""
    failures = [f"{p.id}: lint {msg}" for msg in lint]
    if p.gold_sql is None:
        return ProbeOutcome(p, tuple(failures))
    results: dict[Engine, ResultSet] = {}
    gold: dict[Engine, ProbeGold] = {}
    for engine in probe_engines(p, _engines(executors)):
        try:
            sql = bind_as_of(p.gold_sql[engine], as_of)
        except (KeyError, ValueError) as exc:
            failures.append(f"{p.id} [{engine}]: cannot bind the as-of date ({exc})")
            continue
        ex = _run(executors[engine], sql)
        if ex.error is not None or ex.result is None:
            failures.append(f"{p.id} [{engine}]: execution failed ({ex.error})")
            continue
        if ex.truncated:
            failures.append(f"{p.id} [{engine}]: more than {GOLD_ROW_CAP} gold rows")
            continue
        if not ex.result.rows:
            failures.append(f"{p.id} [{engine}]: no rows")
            continue
        results[engine] = ex.result
        gold[engine] = ProbeGold(
            schema_version="1.0",
            probe_id=p.id,
            engine=engine,
            sql_hash=_sha(sql),
            result=ex.result,
            elapsed_ms=ex.elapsed_ms,
            executed_at=now(),
        )
    if "clickhouse" in results and "snowflake" in results:
        ch, sf = results["clickhouse"], results["snowflake"]
        ok = (
            len(ch.columns) == len(sf.columns)
            and match_result(
                _as_execution("snowflake", sf), ch, tolerance=Tolerance(), ordered=False
            )[0]
        )
        if not ok:
            failures.append(f"{p.id}: cross-engine mismatch")
    return ProbeOutcome(p, tuple(failures), gold)


# -- selection -------------------------------------------------------------------------------


def select_questions(
    passing: Sequence[Question], quotas: Mapping[str, int], seed: int = SELECTION_SEED
) -> tuple[tuple[str, ...], list[str]]:
    """GLD-05: `quota` ids per category, seeded; problems when a category is short."""
    selected: list[str] = []
    problems: list[str] = []
    for cat in CATEGORIES:
        quota = quotas.get(cat)
        if quota is None:
            continue
        pool = sorted(q.id for q in passing if q.category == cat)
        if len(pool) < quota:
            problems.append(f"{cat}: {len(pool)} passing questions, below the quota of {quota}")
            selected += pool
            continue
        selected += random.Random(f"{seed}:{cat}").sample(pool, quota)
    return tuple(sorted(selected)), problems


# -- the whole check -------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldCheckOutcome:
    check: GoldCheck | None  # None for an `only` check
    outcomes: tuple[QuestionOutcome, ...]
    failures: tuple[str, ...]
    written: tuple[Path, ...] = ()
    probes: tuple[ProbeOutcome, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def _lint_by_question(pack: Pack, questions: Sequence[Question]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for issue in lint_questions(questions, context_for(pack)):
        eng = f" [{issue.engine}]" if issue.engine else ""
        out.setdefault(issue.item_id, []).append(f"{issue.rule}{eng}: {issue.message}")
    return out


def _lint_probes(pack: Pack, probes: Sequence[Probe]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for issue in lint_probes(probes, context_for(pack)):
        eng = f" [{issue.engine}]" if issue.engine else ""
        out.setdefault(issue.item_id, []).append(f"{issue.rule}{eng}: {issue.message}")
    return out


def _timestamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _pack_probes(pack: Pack) -> tuple[Probe, ...]:
    try:
        return load_probes(pack)
    except PackError:
        return ()
    except BankError as exc:
        raise GoldConfigError(str(exc)) from exc


def run_gold_check(
    pack: Pack,
    questions: Sequence[Question],
    executors: Mapping[Engine, Any],
    *,
    as_of: date,
    snapshot_sha256: str,
    probes: Sequence[Probe] | None = None,
    seed: int = SELECTION_SEED,
    only: str | None = None,
    write: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    say: Callable[[str], None] = lambda _text: None,
) -> GoldCheckOutcome:
    """Run the gold check over the questions and the probes with gold (`probes` defaults to the
    pack's); write its files unless `only` is set or `write` is False."""
    engines = _engines(executors)
    if not engines:
        raise GoldConfigError("no engine is configured: set the variables in .env.example")
    all_probes = list(_pack_probes(pack) if probes is None else probes)
    lint = _lint_by_question(pack, questions)
    probe_lint = _lint_probes(pack, all_probes)
    targets = list(questions)
    probe_targets = [p for p in all_probes if probe_engines(p, engines)]
    if only is not None:
        targets = [q for q in questions if q.id == only]
        probe_targets = [p for p in all_probes if p.id == only]
        if not targets and not probe_targets:
            raise GoldConfigError(f"no question {only!r} in {pack.ref}")
    outcomes: list[QuestionOutcome] = []
    for q in targets:
        outcome = check_question(q, executors, as_of=as_of, lint=lint.get(q.id, ()), now=now)
        say(f"{q.id}: {'ok' if outcome.passed else '; '.join(outcome.failures)}")
        outcomes.append(outcome)
    probe_outcomes: list[ProbeOutcome] = []
    for pr in probe_targets:
        po = check_probe(pr, executors, as_of=as_of, lint=probe_lint.get(pr.id, ()), now=now)
        say(f"{pr.id}: {'ok' if po.passed else '; '.join(po.failures)}")
        probe_outcomes.append(po)
    failures = [f for o in outcomes for f in o.failures]
    failures += [f for o in probe_outcomes for f in o.failures]
    if only is not None:
        return GoldCheckOutcome(
            None, tuple(outcomes), tuple(failures), probes=tuple(probe_outcomes)
        )

    passing = [o.question for o in outcomes if o.passed]
    selected_ids, short = select_questions(passing, pack.manifest.quotas, seed)
    failures += [f"selection: {p}" for p in short]
    by_id = {q.id: q for q in questions}
    selected = [by_id[i] for i in selected_ids]
    gold: dict[tuple[str, str], AnyGold] = {}
    for o in outcomes:
        for engine, g in o.gold.items():
            gold[(o.question.id, engine)] = g
    for po in probe_outcomes:
        for engine, pg in po.gold.items():
            gold[(po.probe.id, engine)] = pg
    listed = [po.probe for po in probe_outcomes]
    sel = set(selected_ids) | {pr.id for pr in listed}
    checked_at = now()
    check = GoldCheck(
        schema_version="1.0",
        kind="gold_check",
        pack=pack.ref,
        snapshot_sha256=snapshot_sha256,
        as_of=as_of,
        engines=engines,
        mode="cross_engine" if len(engines) == 2 else "single_engine",
        status="fail" if failures else "pass",
        questions=selected_ids,
        probes=tuple(sorted(pr.id for pr in listed)),
        failures=tuple(failures),
        checked_at=checked_at,
        gold_check_id=compute_gold_check_id(
            selected,
            {k: g for k, g in gold.items() if k[0] in sel},
            snapshot_sha256,
            as_of=as_of,
            probes=listed,
        ),
    )
    written: list[Path] = []
    if write:
        root = pack_dir(pack)
        for (item, engine), g in sorted(gold.items()):
            written.append(_write_json(root / "gold" / engine / f"{item}.json", _dump(g)))
        history = root / "gold" / f"check-{_timestamp(checked_at)}.json"
        n = 2
        while history.exists():  # history copies are never overwritten, even within a second
            history = root / "gold" / f"check-{_timestamp(checked_at)}-{n}.json"
            n += 1
        written.append(_write_json(history, check.model_dump(mode="json")))
        written.append(write_gold_check(root, check))
    return GoldCheckOutcome(
        check, tuple(outcomes), tuple(failures), tuple(written), tuple(probe_outcomes)
    )


def _dump(g: AnyGold) -> dict[str, Any]:
    return g.model_dump(mode="json")


def pack_dir(pack: Pack) -> Path:
    if not isinstance(pack.root, Path):
        raise GoldConfigError(f"{pack.ref} is not a directory on disk; copy it and use --pack")
    return pack.root


def configured_executors(environ: Mapping[str, str] | None = None) -> dict[Engine, Any]:
    """N1.2's read-only executors for the engines whose variables are set (nothing connects)."""
    from agentic_analytics.warehouse.doctor import executors_from_env

    ch, sf = executors_from_env(os.environ if environ is None else environ)
    out: dict[Engine, Any] = {}
    if ch is not None:
        out["clickhouse"] = ch
    if sf is not None:
        out["snowflake"] = sf
    return out


__all__ = [
    "GOLD_CHECK_FILE",
    "GOLD_ROW_CAP",
    "SELECTION_SEED",
    "GoldCheckOutcome",
    "GoldConfigError",
    "ProbeOutcome",
    "QuestionOutcome",
    "check_probe",
    "check_question",
    "configured_executors",
    "cross_engine_mismatch",
    "determinism_engine",
    "identical",
    "pack_dir",
    "ranking_index",
    "rewrite_limit",
    "run_gold_check",
    "select_questions",
    "separated",
]
