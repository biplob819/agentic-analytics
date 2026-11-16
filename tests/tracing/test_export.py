"""ANS-04: failure and sample traces exported as JSON into the run folder (D-16, H9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_analytics.schemas import ResultSet
from agentic_analytics.tracing import (
    EXPORT_ROWS,
    Redactor,
    TraceKey,
    build_trace,
    export_name,
    export_trace,
    select_for_export,
)
from tests.schemas import factories as f
from tests.tracing.helpers import ALL_PLANTED, KEY, SECRETS, leaky_trace, turn


def test_export_path_layout_and_relative_return(tmp_path: Path):
    results = tmp_path / "results"
    trace = build_trace(KEY, "Median?", f.answer(), execution=f.execution(), turns=[turn(0)])
    rel = export_trace(trace, results, redactor=Redactor())
    assert rel == "results/r-0001/traces/q-001-claude_mcp-1.json"
    assert (tmp_path / rel).is_file()
    assert f.answer(trace_export=rel).trace_export == rel  # fits AnalyticsAnswer.trace_export


def test_fix_sets_get_their_own_file_name():
    key = TraceKey("r-1", "q-002", "gpt_mcp", 3, fixes=("descriptions", "examples"))
    assert export_name(key) == "q-002-gpt_mcp-descriptions+examples-3.json"
    assert (
        export_name(TraceKey("r-1", "p-w03", "cortex_analyst", 1)) == "p-w03-cortex_analyst-1.json"
    )


def test_export_contents(tmp_path: Path):
    steps = [f.step(0), f.step(1, error="sql_error: boom", rows_returned=None)]
    answer = f.answer(steps=steps, request_id="msg_1", chart={"mark": "text"})
    trace = build_trace(
        KEY,
        "Median?",
        answer,
        execution=f.execution(),
        turns=[turn(0), turn(1)],
        tokens={"in": 2000, "out": 100},
        public=True,
    )
    rel = export_trace(trace, tmp_path / "results", redactor=Redactor())
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0" and data["kind"] == "trace"
    assert data["trace_id"] == KEY.trace_id
    assert data["started_at"] is None
    assert data["trace_key"] == {
        "run_id": "r-0001",
        "question_id": "q-001",
        "backend": "claude_mcp",
        "fixes": [],
        "trial": 1,
    }
    assert data["question"] == "Median?"
    assert data["public"] is True
    assert data["answer"]["sql"] == answer.sql and data["answer"]["text"] == answer.answer
    assert data["answer"]["request_id"] == "msg_1" and data["answer"]["chart"] == {"mark": "text"}
    assert data["steps"][1]["error"] == "sql_error: boom"
    assert data["steps"][0]["arguments"] == {"query": "SELECT 1"}
    assert data["steps"][0]["rows_returned"] == 1
    ex = data["execution"]
    assert ex["columns"] == ["median_price", "sales"] and ex["rows_sample"] == [[250000, 812]]
    assert (ex["rows_total"], ex["engine"], ex["error"]) == (1, "clickhouse", None)
    assert data["costs"] == {
        "cost_usd": 0.02,
        "tokens": {"in": 2000, "out": 100},
        "cortex_messages": 0,
    }
    assert [s["name"] for s in data["spans"]][:2] == ["turn 0", "turn 1"]


def test_row_sample_is_capped(tmp_path: Path):
    rows = tuple((f"t{i}", i) for i in range(EXPORT_ROWS + 10))
    ex = f.execution(result=ResultSet(columns=("t", "n"), rows=rows), rows_total=len(rows))
    rel = export_trace(
        build_trace(KEY, "q", f.answer(), execution=ex), tmp_path / "results", redactor=Redactor()
    )
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert EXPORT_ROWS == 50 and len(data["execution"]["rows_sample"]) == 50


def test_export_is_deterministic_sorted_utf8(tmp_path: Path):
    trace = build_trace(KEY, "Prix médian à Londres ?", f.answer(), turns=[turn(0)])
    rel = export_trace(trace, tmp_path / "results", redactor=Redactor())
    first = (tmp_path / rel).read_bytes()
    export_trace(trace, tmp_path / "results", redactor=Redactor())
    assert (tmp_path / rel).read_bytes() == first
    text = first.decode("utf-8")
    assert "médian" in text and text.endswith("\n")
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert not list((tmp_path / "results" / "r-0001" / "traces").glob("*.tmp"))


def test_failed_execution_and_clarification_export(tmp_path: Path):
    failed = f.execution(result=None, rows_total=None, error="sql_error")
    rel = export_trace(
        build_trace(KEY, "q", f.answer(), execution=failed),
        tmp_path / "results",
        redactor=Redactor(),
    )
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["execution"]["error"] == "sql_error" and data["execution"]["rows_sample"] is None
    clar = f.answer(sql=None, answer="", clarification="Which Leeds?")
    rel = export_trace(build_trace(KEY, "q", clar), tmp_path / "results", redactor=Redactor())
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["execution"] is None and data["answer"]["clarification"] == "Which Leeds?"


def test_records_export_directly(tmp_path: Path):
    rel = export_trace(f.record(), tmp_path / "results", redactor=Redactor(), question="Median?")
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["scores"]["correct"] is True and data["question"] == "Median?"
    assert data["costs"]["cost_usd"] == 0.0123


@pytest.mark.safety
def test_planted_secrets_never_appear_in_an_export(tmp_path: Path):
    rel = export_trace(leaky_trace(), tmp_path / "results", redactor=Redactor(SECRETS))
    text = (tmp_path / rel).read_text(encoding="utf-8")
    for planted in ALL_PLANTED:
        assert planted not in text, planted
    assert "[REDACTED" in text


@pytest.mark.safety
def test_short_numeric_secret_never_corrupts_result_cells_in_an_export(tmp_path: Path):
    ex = f.execution(result=ResultSet(columns=("n",), rows=((482,),)), rows_total=1)
    trace = build_trace(KEY, "passcode 482 please", f.answer(), execution=ex)
    rel = export_trace(trace, tmp_path / "results", redactor=Redactor({"DEMO_PASSCODE": "482"}))
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["execution"]["rows_sample"] == [[482]]
    assert data["question"] == "passcode [REDACTED:DEMO_PASSCODE] please"


@pytest.mark.safety
def test_default_export_redactor_reads_the_environment(tmp_path: Path, monkeypatch):
    secret = "Quiet" + "Harbour42"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    rel = export_trace(build_trace(KEY, f"is it {secret}?", f.answer()), tmp_path / "results")
    assert secret not in (tmp_path / rel).read_text(encoding="utf-8")


def test_select_for_export_failures_and_deterministic_samples():
    keys = [TraceKey("r-1", f"q-{i:03d}", "claude_mcp", 1) for i in range(200)]
    assert all(select_for_export(k, correct=False) == "failure" for k in keys)
    picks = [select_for_export(k, correct=True, sample_every=10) for k in keys]
    assert picks == [select_for_export(k, correct=True, sample_every=10) for k in keys]
    n = picks.count("sample")
    assert set(picks) <= {"sample", None} and 5 <= n <= 40
    assert all(select_for_export(k, correct=True, sample_every=1) == "sample" for k in keys)
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            select_for_export(keys[0], correct=True, sample_every=bad)  # type: ignore[arg-type]
