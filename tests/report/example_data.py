"""Generator for the bundled SYNTHETIC example run (B3.1, `agentic-analytics report --example`).

Nothing here touches a network or an account. Outcomes are drawn from fixed per-backend,
per-category rates with string-seeded `random.Random` streams, turned into schema-valid records
the way S2.1 would score them (answers assembled by A2.3's `assemble`, so charts come from the
executed rows), and summarised by `aggregate.build_summary`. Only the summary,
a handful of failure records, the questions they need and their exported traces are bundled.

Regenerate after a deliberate change with:

    uv run python -m tests.report.example_data

`tests/report/test_example.py` regenerates into a temp folder and requires byte equality with
`src/agentic_analytics/report/example/`, so the bundle cannot drift from the code.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from agentic_analytics.aggregate import Rates, build_summary, write_summary
from agentic_analytics.answer.assemble import assemble
from agentic_analytics.schemas import ENGINE_OF, Question, Record, ResultSet
from agentic_analytics.tracing import Redactor, export_trace
from tests.schemas import factories as f

RUN_ID = "example-synthetic"
SEED = 20261115
N_RESAMPLES = 10_000
RATES = Rates(credits_per_message=0.067, usd_per_credit=3.0)
SNAPSHOT = "5f1c0de2a9b84c7e93d1a6f0b2e4c8d7a1f3e5b7c9d2e4f6a8b0c1d3e5f7a9b1"
GOLD_CHECK = "0c7e1d9a3b5f2e8c4a6d0b1f9e7c3a5d2b4f6e8a0c1d3e5f7a9b2c4d6e8f0a1b"
CONFIG_HASH = "9a8b7c6d5e4f30211203f4e5d6c7b8a99a8b7c6d5e4f30211203f4e5d6c7b8a9"
AS_OF = date(2026, 9, 30)
EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "src/agentic_analytics/report/example"

CATS = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")
PER_CATEGORY = 10
TRIALS = 3
TOWNS = ("LEEDS", "MANCHESTER", "BRISTOL", "BIRMINGHAM", "LIVERPOOL", "NOTTINGHAM", "SHEFFIELD",
         "YORK", "CARDIFF", "NORWICH")  # fmt: skip
COUNTIES = ("WEST YORKSHIRE", "GREATER MANCHESTER", "CITY OF BRISTOL", "WEST MIDLANDS",
            "MERSEYSIDE", "NOTTINGHAMSHIRE", "SOUTH YORKSHIRE", "NORTH YORKSHIRE", "CARDIFF",
            "NORFOLK")  # fmt: skip
TYPES = (("detached", "D"), ("semi-detached", "S"), ("terraced", "T"), ("flat", "F"))

# Share of questions answered correctly, per backend and category (synthetic, not measured).
RATE = {
    "claude_mcp": dict(aggregation=0.8, time=0.7, geography=0.62, top_n=0.72, yoy=0.55,
                       ambiguous=0.35),
    "gpt_mcp": dict(aggregation=0.74, time=0.62, geography=0.55, top_n=0.66, yoy=0.46,
                    ambiguous=0.25),
    "cortex_analyst": dict(aggregation=0.86, time=0.78, geography=0.7, top_n=0.6, yoy=0.5,
                           ambiguous=0.6),
}  # fmt: skip
# Per fix: (share of wrong questions the fix turns right, share of right ones it breaks).
FIX_EFFECT = {
    ("claude_mcp", "descriptions"): (0.6, 0.02),
    ("claude_mcp", "examples"): (0.15, 0.1),
    ("claude_mcp", "self_check"): (0.3, 0.05),
    ("gpt_mcp", "descriptions"): (0.5, 0.03),
    ("gpt_mcp", "examples"): (0.12, 0.1),
    ("gpt_mcp", "self_check"): (0.03, 0.35),
}
# Obviously fictional model names, so made-up accuracy never sits next to a real model's name.
SNAPSHOTS = {
    "claude_mcp": "example-agent-a",
    "gpt_mcp": "example-agent-b",
    "cortex_analyst": "example-cortex",
}
Outcome = Literal["right", "wrong", "exec_error", "clarify", "error"]


def _qid(i: int) -> str:
    return f"q-{i:03d}"


def questions() -> dict[str, Question]:
    out: dict[str, Question] = {}
    i = 0
    for cat in CATS:
        for k in range(PER_CATEGORY):
            i += 1
            town, county = TOWNS[k], COUNTIES[k]
            ptype, code = TYPES[k % len(TYPES)]
            year = 2016 + k
            if cat == "aggregation":
                text = f"What was the median price paid for a {ptype} house in {town} in {year}?"
                ch = (
                    f"SELECT medianExact(price) AS median_price FROM uk.uk_price_paid "
                    f"WHERE town = '{town}' AND type = '{code}' AND toYear(date) = {year}"
                )
                sf = (
                    f"SELECT MEDIAN(price) AS median_price FROM ANALYTICS.UK.UK_PRICE_PAID "
                    f"WHERE town = '{town}' AND type = '{code}' AND YEAR(date) = {year}"
                )
            elif cat == "time":
                text = f"How many sales were registered in {town} in each month of {year}?"
                ch = (
                    f"SELECT toStartOfMonth(date) AS month, count() AS sales "
                    f"FROM uk.uk_price_paid WHERE town = '{town}' AND toYear(date) = {year} "
                    f"GROUP BY month ORDER BY month"
                )
                sf = (
                    f"SELECT DATE_TRUNC('month', date) AS month, COUNT(*) AS sales "
                    f"FROM ANALYTICS.UK.UK_PRICE_PAID WHERE town = '{town}' "
                    f"AND YEAR(date) = {year} GROUP BY month ORDER BY month"
                )
            elif cat == "geography":
                text = f"Which district in {county} had the highest average price in {year}?"
                ch = (
                    f"SELECT district, avg(price) AS avg_price FROM uk.uk_price_paid "
                    f"WHERE county = '{county}' AND toYear(date) = {year} "
                    f"GROUP BY district ORDER BY avg_price DESC LIMIT 1"
                )
                sf = (
                    f"SELECT district, AVG(price) AS avg_price FROM ANALYTICS.UK.UK_PRICE_PAID "
                    f"WHERE county = '{county}' AND YEAR(date) = {year} "
                    f"GROUP BY district ORDER BY avg_price DESC LIMIT 1"
                )
            elif cat == "top_n":
                n = 3 + k % 5
                text = f"List the {n} towns with the highest median price in {year}."
                ch = (
                    f"SELECT town, medianExact(price) AS median_price FROM uk.uk_price_paid "
                    f"WHERE toYear(date) = {year} GROUP BY town "
                    f"ORDER BY median_price DESC LIMIT {n}"
                )
                sf = (
                    f"SELECT town, MEDIAN(price) AS median_price FROM ANALYTICS.UK.UK_PRICE_PAID "
                    f"WHERE YEAR(date) = {year} GROUP BY town ORDER BY median_price DESC LIMIT {n}"
                )
            elif cat == "yoy":
                text = (
                    f"By what percentage did the average price in {town} change "
                    f"from {year - 1} to {year}?"
                )
                ch = (
                    f"SELECT 100 * (avgIf(price, toYear(date) = {year}) / "
                    f"avgIf(price, toYear(date) = {year - 1}) - 1) AS pct_change "
                    f"FROM uk.uk_price_paid WHERE town = '{town}'"
                )
                sf = (
                    f"SELECT 100 * (AVG(IFF(YEAR(date) = {year}, price, NULL)) / "
                    f"AVG(IFF(YEAR(date) = {year - 1}, price, NULL)) - 1) AS pct_change "
                    f"FROM ANALYTICS.UK.UK_PRICE_PAID WHERE town = '{town}'"
                )
            else:
                text = f"What is the typical house price in {town} lately?"
                readings = (
                    {
                        "clickhouse": f"SELECT medianExact(price) FROM uk.uk_price_paid "
                        f"WHERE town = '{town}' AND date > '2025-09-30'",
                        "snowflake": f"SELECT MEDIAN(price) FROM ANALYTICS.UK.UK_PRICE_PAID "
                        f"WHERE town = '{town}' AND date > '2025-09-30'",
                    },
                    {
                        "clickhouse": f"SELECT avg(price) FROM uk.uk_price_paid "
                        f"WHERE town = '{town}' AND date > '2026-06-30'",
                        "snowflake": f"SELECT AVG(price) FROM ANALYTICS.UK.UK_PRICE_PAID "
                        f"WHERE town = '{town}' AND date > '2026-06-30'",
                    },
                )
                out[_qid(i)] = f.ambiguous(
                    id=_qid(i), text=text, interpretations=list(readings), owner_reviewed=True
                )
                continue
            out[_qid(i)] = f.question(
                id=_qid(i),
                text=text,
                category=cat,
                gold_sql={"clickhouse": ch, "snowflake": sf},
                ordered=cat in ("time", "top_n"),
                top_n=(3 + k % 5) if cat == "top_n" else None,
                author="claude" if i % 2 else "gpt",
                owner_reviewed=True,
            )
    return out


def _wrong_sql(sql: str) -> str:
    """A plausible wrong query: the mean for a median, or a year filter dropped."""
    for a, b in (("medianExact(", "avg("), ("MEDIAN(", "AVG(")):
        if a in sql:
            return sql.replace(a, b)
    return sql.replace(" DESC", " ASC") if " DESC" in sql else sql + " AND price > 0"


def _record(
    q: Question,
    backend: str,
    fixes: tuple[str, ...],
    trial: int,
    outcome: Outcome,
    rng: random.Random,
) -> Record:
    cortex = backend == "cortex_analyst"
    engine = ENGINE_OF[backend]
    answer_expected = q.expected == "answer"
    gold = (q.gold_sql or q.interpretations[0])[engine]
    value = 150_000 + 5_000 * int(q.id[2:])
    sql: str | None = gold if outcome == "right" else _wrong_sql(gold)
    rows: tuple[tuple[Any, ...], ...] = ((value,),) if outcome == "right" else ((value * 1.07,),)
    columns = ("value",)
    answer_kw: dict[str, Any] = dict(
        backend=backend,
        model_snapshot=SNAPSHOTS[backend],
        answer=f"The figure is {rows[0][0]:,.0f}.",
    )
    execution = None
    reason: str | None = None
    if outcome in ("right", "wrong"):
        execution = f.execution(
            engine=engine,
            sql=sql,
            result=ResultSet(columns=columns, rows=rows),
            rows_total=1,
            query_id=f"{engine[:2]}-{q.id}-{trial}",
            elapsed_ms=30 + rng.randrange(400),
        )
        if outcome == "wrong" and answer_expected:
            reason = "cell values differ: 1 of 1 rows outside tolerance (rel 1e-09)"
    elif outcome == "exec_error":
        sql = gold.replace("price", "prices", 1)
        execution = f.execution(
            engine=engine,
            sql=sql,
            result=None,
            rows_total=None,
            query_id=None,
            error="sql_error",
            elapsed_ms=20 + rng.randrange(60),
        )
        answer_kw["answer"] = "I could not compute this."
    elif outcome == "clarify":
        sql = None
        answer_kw.update(answer="", clarification="Do you mean the median or the mean price?")
        if cortex:
            answer_kw.update(
                clarification=None,
                suggestions=("Median price in the last 12 months", "Mean price this quarter"),
            )
    else:
        sql = None
        answer_kw.update(answer="", error="step_budget")
    answer_kw["sql"] = sql
    if cortex:
        answer_kw["steps"] = ()
        answer_kw["interpretation"] = f"This is our interpretation of your question: {q.text}"
    else:
        n_steps = 2 + rng.randrange(4)
        steps = [
            f.step(
                i,
                tool=("list_tables" if i == 0 else "run_select_query"),
                arguments={"database": "uk"} if i == 0 else {"query": sql or gold},
                rows_returned=None if i == 0 else 1,
                latency_ms=800 + rng.randrange(2400),
                tokens_in=2900 if i == 0 else 0,
                tokens_out=240 if i == 0 else 0,
            )
            for i in range(n_steps)
        ]
        answer_kw["steps"] = steps
    clarified = outcome == "clarify"
    matched = outcome == "right"
    scores = f.scores(
        result_match=(matched if answer_expected else None),
        clarified=clarified,
        false_clarification=clarified and answer_expected,
        correct=(matched if answer_expected else clarified),
        answer_from_rows=True if execution is not None and execution.error is None else None,
        tool_calls=0 if cortex else len(answer_kw["steps"]),
        mismatch_reason=reason,
    )
    tokens = {} if cortex else {"in": 6 * 2900, "out": 260, "cached": 9000}
    return f.record(
        run_id=RUN_ID,
        question_id=q.id,
        backend=backend,
        fixes=list(fixes),
        trial=trial,
        # the chart comes from A2.3's builder over the executed rows, as in a real run
        answer=assemble(f.answer(**answer_kw), execution, question_text=q.text),
        execution=execution,
        scores=scores,
        tokens=tokens,
        cost_usd=0.0 if cortex else round(0.03 + rng.random() * 0.02, 6),
        cortex_messages=1 if cortex else 0,
        warehouse_elapsed_ms=execution.elapsed_ms if execution else 0,
        timestamp=f.T0 + timedelta(seconds=trial),
        seed=SEED,
        gold_check_id=GOLD_CHECK,
        snapshot_sha256=SNAPSHOT,
    )


def _wrong_kind(q: Question, rng: random.Random) -> Outcome:
    if q.expected == "clarify":
        return "wrong"  # answered an ambiguous question without asking back
    kinds: tuple[Outcome, ...] = ("wrong", "exec_error", "clarify", "error")
    return rng.choices(kinds, weights=(6, 2, 1, 1))[0]


def records(qs: dict[str, Question]) -> list[Record]:
    out: list[Record] = []
    for backend, rates in RATE.items():
        for q in qs.values():
            rng = random.Random(f"example:{backend}:{q.id}")
            p = min(0.97, max(0.03, rates[q.category] + rng.uniform(-0.15, 0.15)))
            stable = rng.random() < 0.75
            first = rng.random() < p
            hits = [first if stable else rng.random() < p for _ in range(TRIALS)]
            kind = _wrong_kind(q, rng)
            right: Outcome = "right" if q.expected == "answer" else "clarify"
            base: list[Outcome] = [right if h else kind for h in hits]
            out += [_record(q, backend, (), t + 1, o, rng) for t, o in enumerate(base)]
            if backend == "cortex_analyst":
                continue
            for fix in ("descriptions", "examples", "self_check"):
                up, down = FIX_EFFECT[(backend, fix)]
                frng = random.Random(f"example:{backend}:{fix}:{q.id}")
                roll = frng.random()
                outcomes: list[Outcome] = list(base)
                if any(o != right for o in base) and roll < up:
                    outcomes = [right] * TRIALS
                elif all(o == right for o in base) and roll < down:
                    outcomes = [kind] * TRIALS
                out += [_record(q, backend, (fix,), t + 1, o, frng) for t, o in enumerate(outcomes)]
    return out


def probe_records() -> list[Record]:
    out = []
    for pid, backends in (
        ("p-w01", ("claude_mcp", "gpt_mcp", "cortex_analyst")),
        ("p-w02", ("claude_mcp", "gpt_mcp", "cortex_analyst")),
        ("p-iq01", ("claude_mcp", "gpt_mcp", "cortex_analyst")),
        ("p-id01", ("claude_mcp", "gpt_mcp")),
    ):
        for backend in backends:
            cortex = backend == "cortex_analyst"
            followed = pid == "p-id01" and backend == "gpt_mcp"  # one synthetic failure
            out.append(
                f.record(
                    run_id=RUN_ID,
                    question_id=pid,
                    backend=backend,
                    answer=f.answer(
                        backend=backend,
                        model_snapshot=SNAPSHOTS[backend],
                        sql=None,
                        answer="",
                        clarification="I can only read data; I cannot change it.",
                        steps=() if cortex else [f.step(0)],
                    ),
                    execution=None,
                    scores=f.scores(
                        result_match=None,
                        clarified=True,
                        correct=True,
                        answer_from_rows=None,
                        tool_calls=0 if cortex else 1,
                        injection_followed=followed,
                    ),
                    tokens={} if cortex else {"in": 1200, "out": 40},
                    cost_usd=0.0 if cortex else 0.01,
                    cortex_messages=1 if cortex else 0,
                    seed=SEED,
                    gold_check_id=GOLD_CHECK,
                    snapshot_sha256=SNAPSHOT,
                )
            )
    return out


def _pick_failures(recs: list[Record]) -> list[Record]:
    """One incorrect record per (backend, fix set, failure kind), first in sorted order."""
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    picked: list[Record] = []
    for r in sorted(recs, key=lambda r: (r.backend, r.fixes, r.question_id, r.trial)):
        if r.scores.correct or r.question_id.startswith("p-"):
            continue
        kind = (
            r.answer.error
            or (r.execution.error if r.execution else None)
            or ("false_clarification" if r.scores.false_clarification else None)
            or ("no_clarification" if r.scores.result_match is None else "mismatch")
        )
        key = (r.backend, r.fixes if r.fixes in ((), ("descriptions",)) else ("other",), kind)
        if key not in seen:
            seen.add(key)
            picked.append(r)
    return picked


def build_example(out_dir: Path) -> None:
    """Write summary.json, records.jsonl, questions.jsonl and traces/ into `out_dir`."""
    qs = questions()
    recs = records(qs) + probe_records()
    summary = build_summary(
        recs,
        qs,
        run_id=RUN_ID,
        config_hash=CONFIG_HASH,
        snapshot_sha256=SNAPSHOT,
        as_of=AS_OF,
        gold_check_id=GOLD_CHECK,
        pack="uk_price_paid@1.0",
        gold_mode="cross_engine",
        rates=RATES,
        seed=SEED,
        started_at=f.T0,
        finished_at=f.T0 + timedelta(minutes=47),
        n_resamples=N_RESAMPLES,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_summary(out_dir / "summary.json", summary)
    work = out_dir / ".work"
    results = work / "results"
    bundled: list[Record] = []
    for r in _pick_failures(recs):
        if r.backend == "gpt_mcp" and r.fixes:
            bundled.append(r)  # left without an exported trace, to show that state
            continue
        rel = export_trace(r, results, redactor=Redactor({}), question=qs[r.question_id].text)
        answer = r.answer.model_copy(update={"trace_export": rel})
        bundled.append(r.model_copy(update={"answer": answer}))
    traces = out_dir / "traces"
    traces.mkdir(exist_ok=True)
    src = results / RUN_ID / "traces"
    if src.is_dir():
        for p in sorted(src.iterdir()):
            shutil.copyfile(p, traces / p.name)
    shutil.rmtree(work)
    lines = [Record.model_validate(r.model_dump()).model_dump_json() for r in bundled]
    (out_dir / "records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    needed = sorted({r.question_id for r in bundled})
    qlines = [qs[q].model_dump_json() for q in needed]
    (out_dir / "questions.jsonl").write_text(
        "\n".join(qlines) + "\n", encoding="utf-8", newline="\n"
    )


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.json*"))}


def main(argv: list[str] | None = None) -> int:
    target = Path(argv[0]) if argv else EXAMPLE_DIR
    for old in (target / "traces").glob("*.json"):
        old.unlink()
    build_example(target)
    for p in target.rglob("*.json*"):
        p.chmod(0o644)
    print(json.dumps({"written": sorted(tree(target))}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
