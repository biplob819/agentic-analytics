"""QST-04 helper: GPT Sol writes ClickHouse SQL from the question text, DDL and as-of line alone
(D-9); both versions run and S2.1's matcher compares them. Fake model offline; real call live."""

from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from agentic_analytics.backends.prompts import as_of_line
from agentic_analytics.gold import (
    DualAuthorError,
    compare_dual,
    dual_authorship,
    gpt_sol_sql,
    table_ddl,
)
from agentic_analytics.gold.dual import GPT_SOL_MODEL, extract_sql
from tests.backends.fakes import ScriptedModel, turn
from tests.gold_check.fakes import AS_OF, BUNDLED, FakeExecutor, bound, bundled_questions, rs

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")
DDL = table_ddl(BUNDLED)
GPT_SQL = "SELECT count() AS n FROM uk.uk_price_paid WHERE toYear(date) = 2023"


def gpt(text: str, **kw) -> ScriptedModel:
    return ScriptedModel([turn(text=text, **kw)], provider="openai", model=GPT_SOL_MODEL)


def test_ddl_is_the_clickhouse_create_table_with_the_database():
    assert DDL.startswith("CREATE TABLE uk.uk_price_paid")
    assert "{database}" not in DDL and "INSERT" not in DDL and "url(" not in DDL


def test_gpt_sol_sees_only_the_question_ddl_and_as_of_with_no_tools():
    q = bundled_questions("q-001")[0]
    model = gpt(f"```sql\n{GPT_SQL};\n```")
    draft = asyncio.run(gpt_sol_sql(q.text, ddl=DDL, as_of=AS_OF, model=model))
    assert draft.sql == GPT_SQL
    assert model.tools == ()
    assert model.user == f"Table DDL:\n{DDL}\n\n{as_of_line(AS_OF)}\n\nQuestion: {q.text}"
    # the agents' own reading of relative dates (D-10), not only the date
    assert "'last year' means the last full calendar year before that date (2025)" in (
        model.user or ""
    )
    assert q.gold_sql is not None
    for leaked in (q.gold_sql["clickhouse"], q.id, q.category):
        assert leaked not in (model.system or "") + (model.user or "")


def test_sql_extraction():
    assert extract_sql("Here:\n```SQL\nSELECT 1\n```\nthanks") == "SELECT 1"
    assert extract_sql("SELECT 2;") == "SELECT 2"
    assert extract_sql("```sql\n```") is None


def test_refusal_or_empty_reply_is_an_error():
    with pytest.raises(DualAuthorError, match="refused"):
        asyncio.run(gpt_sol_sql("q", ddl=DDL, as_of=AS_OF, model=gpt("", stop="refusal")))
    with pytest.raises(DualAuthorError, match="no SQL"):
        asyncio.run(gpt_sol_sql("q", ddl=DDL, as_of=AS_OF, model=gpt("   ")))


def test_compare_runs_both_on_clickhouse_and_uses_the_question_tolerance():
    q = bundled_questions("q-002")[0]  # median at 0.5%
    ch = FakeExecutor("clickhouse", {bound(q, "clickhouse"): rs(["median_price"], [290_000.0])})
    ch.set("SELECT medianExact(price) AS m FROM uk.uk_price_paid", rs(["m"], [291_000.0]))
    ch.set("SELECT avg(price) AS m FROM uk.uk_price_paid", rs(["m"], [350_000.0]))
    agree = compare_dual(q, "SELECT medianExact(price) AS m FROM uk.uk_price_paid", ch, as_of=AS_OF)
    assert agree.agree and agree.reason is None
    differ = compare_dual(q, "SELECT avg(price) AS m FROM uk.uk_price_paid", ch, as_of=AS_OF)
    assert not differ.agree and differ.reason
    assert all(cap == 200 for _, cap in ch.calls)


def test_failing_gpt_sql_is_a_disagreement_not_a_crash():
    q = bundled_questions("q-001")[0]
    ch = FakeExecutor("clickhouse", {bound(q, "clickhouse"): rs(["sales"], [5])})
    ch.set("SELECT nope FROM uk.uk_price_paid", "sql_error")
    out = compare_dual(q, "SELECT nope FROM uk.uk_price_paid", ch, as_of=AS_OF)
    assert not out.agree and out.reason == "GPT Sol's SQL failed (sql_error)"
    write = compare_dual(q, "DROP TABLE uk.uk_price_paid", ch, as_of=AS_OF)
    assert write.reason == "GPT Sol's SQL failed (guard)"  # the guard refused it


def test_dual_authorship_report_covers_answer_questions_only():
    qs = bundled_questions("q-001", "q-101", "q-026")
    ch = FakeExecutor("clickhouse")
    ch.set(bound(qs[0], "clickhouse"), rs(["sales"], [10]))
    ch.set(bound(qs[2], "clickhouse"), rs(["sales"], [20]))
    ch.set(GPT_SQL, rs(["n"], [10]))
    model = ScriptedModel(
        [
            turn(text=f"```sql\n{GPT_SQL}\n```"),
            turn(text="I can't help with that.", stop="refusal"),
        ],
        provider="openai",
        model=GPT_SOL_MODEL,
    )
    rows = asyncio.run(dual_authorship(qs, model=model, executor=ch, ddl=DDL, as_of=AS_OF))
    assert [(r.question_id, r.agree) for r in rows] == [("q-001", True), ("q-026", False)]
    assert "refused" in (rows[1].reason or "")
    assert rows[0].to_dict()["gpt_sql"] == GPT_SQL


@pytest.mark.live
@LIVE
def test_live_gpt_sol_writes_clickhouse_sql():  # pragma: no cover - needs OPENAI_API_KEY
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.gold import make_gpt_sol

    load_dotenv()
    q = bundled_questions("q-001")[0]
    as_of = date.fromisoformat(os.environ.get("AA_AS_OF", "2026-09-30"))
    draft = asyncio.run(gpt_sol_sql(q.text, ddl=DDL, as_of=as_of, model=make_gpt_sol()))
    assert draft.sql.lstrip().upper().startswith(("SELECT", "WITH"))


def test_make_gpt_sol_defaults_to_medium_effort_as_the_live_run() -> None:
    from agentic_analytics.gold import make_gpt_sol

    agent = make_gpt_sol(client=object())  # a client stub: nothing is called
    assert agent.effort == "medium" and agent.model.startswith("gpt-6.1-sol")
    assert make_gpt_sol(client=object(), effort="high").effort == "high"
