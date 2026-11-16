"""ANS-02: `{sql, answer, chart}` assembled the same way for every backend."""

from __future__ import annotations

import json

import pytest

from agentic_analytics.answer import assemble, chart_for, with_trace
from agentic_analytics.schemas import AnalyticsAnswer, ResultSet
from tests.schemas import factories as f

BARS = ResultSet(columns=("town", "n"), rows=(("LEEDS", 3), ("YORK", 5)))
SF_SQL = "SELECT town, COUNT(*) AS n FROM ANALYTICS.UK.PRICE_PAID GROUP BY town"


def plain(chart):
    """A frozen chart (tuples, FrozenDicts) as the plain JSON the builder returned."""
    return json.loads(json.dumps(chart))


def bar_execution(**kw):
    base = dict(result=BARS, rows_total=2)
    base.update(kw)
    return f.execution(**base)


def cortex_answer(**kw) -> AnalyticsAnswer:
    base = dict(
        backend="cortex_analyst",
        model_snapshot="claude-sonnet-5-5",
        sql=SF_SQL,
        answer="YORK has 5 sales.",
        steps=[],
        request_id="cortex-req-1",
    )
    base.update(kw)
    return f.answer(**base)


def test_successful_execution_sets_the_builders_chart():
    out = assemble(f.answer(), bar_execution())
    assert plain(out.chart) == chart_for(BARS)
    assert out.chart is not None and out.chart["mark"]["type"] == "bar"
    # nothing else changes
    assert out.model_dump(exclude={"chart"}) == f.answer().model_dump(exclude={"chart"})


def test_question_text_becomes_the_title():
    out = assemble(f.answer(), bar_execution(), question_text="Sales by town?")
    assert out.chart is not None and out.chart["title"] == "Sales by town?"


def test_result_the_builder_declines_gives_no_chart():
    out = assemble(f.answer(), f.execution())  # 1 row x 2 measures
    assert out.chart is None


def test_answer_is_revalidated_not_mutated(monkeypatch):
    original = f.answer()
    calls = []
    real = AnalyticsAnswer.model_validate.__func__  # type: ignore[attr-defined]

    def spy(cls, data, *a, **kw):
        calls.append(data)
        return real(cls, data, *a, **kw)

    monkeypatch.setattr(AnalyticsAnswer, "model_validate", classmethod(spy))
    out = assemble(original, bar_execution())
    assert calls and calls[-1]["chart"] == chart_for(BARS)
    assert out is not original and original.chart is None
    with pytest.raises(TypeError):
        out.chart["mark"] = "x"  # type: ignore[index]  # deep-frozen by validation


def test_truncated_execution_gets_no_chart():
    rows = tuple((f"t{i}", i) for i in range(3))
    ex = f.execution(
        result=ResultSet(columns=("town", "n"), rows=rows), rows_total=10_000, truncated=True
    )
    assert assemble(f.answer(), ex).chart is None


def test_failed_execution_leaves_no_chart():
    failed = f.execution(result=None, rows_total=None, error="sql_error")
    stale = f.answer(chart={"mark": "bar"})
    out = assemble(stale, failed)
    assert out.chart is None
    assert out.model_dump(exclude={"chart"}) == stale.model_dump(exclude={"chart"})


def test_no_execution_leaves_the_answer_unchanged():
    a = f.answer()
    assert assemble(a, None) is a


def test_clarification_never_gets_a_chart():
    clar = f.answer(sql=None, answer="", clarification="Which Leeds: city or district?")
    assert assemble(clar, None) is clar


def test_execution_without_sql_in_the_answer_is_an_error():
    clar = f.answer(sql=None, answer="", clarification="Which Leeds?")
    with pytest.raises(ValueError, match="no SQL"):
        assemble(clar, bar_execution())


def test_execution_of_other_sql_is_an_error():
    with pytest.raises(ValueError, match="answer's SQL"):
        assemble(f.answer(), bar_execution(sql="SELECT 2"))


def test_execution_on_the_wrong_engine_is_an_error():
    with pytest.raises(ValueError, match="clickhouse"):
        assemble(f.answer(), bar_execution(engine="snowflake"))


def test_cortex_answers_use_the_same_builder():
    out = assemble(cortex_answer(), bar_execution(engine="snowflake", sql=SF_SQL))
    agent = assemble(f.answer(), bar_execution())
    assert plain(out.chart) == plain(agent.chart) == chart_for(BARS)
    assert out.request_id == "cortex-req-1"


def test_never_invents_rows_values_come_from_execution():
    out = assemble(f.answer(), bar_execution())
    assert out.chart is not None
    shown = {(v["label"], v["value"]) for v in out.chart["data"]["values"]}
    assert shown == set(BARS.rows)


def test_with_trace_sets_url_and_export_path():
    out = with_trace(f.answer(), trace_url="https://lf.example/t/1", trace_export="results/r/x")
    assert (out.trace_url, out.trace_export) == ("https://lf.example/t/1", "results/r/x")
    assert with_trace(out).trace_url == "https://lf.example/t/1"  # None keeps what is there
