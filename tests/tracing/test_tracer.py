"""ANS-03: one trace per (run, question, backend, fix set, trial), spans per turn and tool call."""

from __future__ import annotations

import json
import sys
from itertools import pairwise
from typing import Any

import pytest

from agentic_analytics.schemas import ResultSet
from agentic_analytics.tracing import (
    LangfuseTracer,
    NoopTracer,
    Redactor,
    TraceKey,
    Tracer,
    TracingUnavailable,
    build_trace,
    trace_from_record,
)
from tests.schemas import factories as f
from tests.tracing.helpers import ALL_PLANTED, KEY, SECRETS, FakeLangfuse, leaky_trace, turn


# -- keys -------------------------------------------------------------------------------------
def test_trace_id_is_deterministic_32_hex_and_unique_per_key():
    tid = KEY.trace_id
    assert tid == TraceKey("r-0001", "q-001", "claude_mcp", 1).trace_id
    assert len(tid) == 32 and all(c in "0123456789abcdef" for c in tid)
    others = {
        TraceKey("r-0002", "q-001", "claude_mcp", 1).trace_id,
        TraceKey("r-0001", "q-002", "claude_mcp", 1).trace_id,
        TraceKey("r-0001", "q-001", "gpt_mcp", 1).trace_id,
        TraceKey("r-0001", "q-001", "claude_mcp", 2).trace_id,
        TraceKey("r-0001", "q-001", "claude_mcp", 1, fixes=("descriptions",)).trace_id,
    }
    assert tid not in others and len(others) == 5


@pytest.mark.parametrize(
    "kw",
    [
        dict(run_id="../etc"),
        dict(run_id="a/b"),
        dict(run_id=""),
        dict(question_id="q-1/../x"),
        dict(backend="bard"),
        dict(trial=0),
        dict(trial=True),
        dict(fixes=("examples", "descriptions")),
        dict(fixes=("nope",)),
    ],
)
def test_bad_keys_are_refused(kw):
    base: dict[str, Any] = dict(run_id="r-0001", question_id="q-001", backend="claude_mcp", trial=1)
    base.update(kw)
    with pytest.raises(ValueError):
        TraceKey(**base)


# -- build_trace ------------------------------------------------------------------------------
def test_one_generation_span_per_model_turn_with_tokens_cost_latency_request_id():
    answer = f.answer()
    trace = build_trace(KEY, "Median?", answer, turns=[turn(0), turn(1, cost=0.02)])
    gens = [s for s in trace.spans if s.kind == "generation"]
    assert [s.name for s in gens] == ["turn 0", "turn 1"]
    g = gens[1]
    assert (g.tokens_in, g.tokens_out, g.cost_usd, g.latency_ms, g.request_id) == (
        1000,
        55,  # visible output plus reasoning
        0.02,
        701,
        "req_1",
    )
    assert g.model == "claude-sonnet-5-5-20261101"
    assert g.metadata["tokens_cached"] == 100 and g.metadata["stop"] == "tool_use"
    assert trace.cost_usd == pytest.approx(0.03)


def test_one_tool_span_per_agent_step():
    steps = [
        f.step(0),
        f.step(
            1,
            tool="list_tables",
            arguments={},
            rows_returned=None,
            error="sql_error: boom",
            latency_ms=33,
        ),
    ]
    trace = build_trace(KEY, "q", f.answer(steps=steps), turns=[turn(0)])
    tools = [s for s in trace.spans if s.kind == "tool"]
    assert [s.name for s in tools] == ["tool run_select_query", "tool list_tables"]
    assert tools[0].input == {"query": "SELECT 1"}
    assert tools[0].output == {"rows_returned": 1, "truncated": False}
    assert (tools[1].error, tools[1].latency_ms) == ("sql_error: boom", 33)
    assert tools[0].metadata["step_index"] == 0


def test_final_answer_time_is_counted_once_on_its_turn():
    """A2.3 live: the final_answer step carries its turn's latency (A2.1); with the turns traced,
    the old trace counted it twice and lasted 16.2 s for an 11.1 s question."""
    final_args = {"sql": "SELECT 1", "answer": "1"}
    steps = [
        f.step(0, latency_ms=900),
        f.step(1, tool="final_answer", arguments=final_args, rows_returned=None, latency_ms=701),
    ]
    answer = f.answer(steps=steps)
    trace = build_trace(KEY, "q", answer, turns=[turn(0), turn(1)])
    final = next(s for s in trace.spans if s.name == "tool final_answer")
    assert (final.latency_ms, final.metadata["turn_latency_ms"]) == (0, 701)
    run_query = next(s for s in trace.spans if s.name == "tool run_select_query")
    assert run_query.latency_ms == 900 and "turn_latency_ms" not in run_query.metadata
    assert sum(s.latency_ms for s in trace.spans) == 700 + 701 + 900
    # without the turns, the step is the only record of that model time, so it is kept
    derived = build_trace(KEY, "q", answer)
    final = next(s for s in derived.spans if s.name == "tool final_answer")
    assert final.latency_ms == 701 and "turn_latency_ms" not in final.metadata


def test_without_turns_generation_spans_come_from_steps_with_tokens():
    steps = [f.step(0), f.step(1, tokens_in=0, tokens_out=0), f.step(2, tokens_in=7, tokens_out=3)]
    trace = build_trace(KEY, "q", f.answer(steps=steps), cost_usd=0.5)
    gens = [s for s in trace.spans if s.kind == "generation"]
    assert [(g.tokens_in, g.tokens_out, g.cost_usd) for g in gens] == [
        (900, 40, None),
        (7, 3, None),
    ]
    assert all(g.metadata["derived_from_steps"] for g in gens)
    assert trace.cost_usd == 0.5


def test_cortex_trace_carries_request_id_on_its_span():
    answer = f.answer(
        backend="cortex_analyst",
        steps=[],
        request_id="cx-req-9",
        model_snapshot="claude-sonnet-5-5",
    )
    key = TraceKey("r-0001", "q-001", "cortex_analyst", 1)
    trace = build_trace(key, "q", answer, cost_usd=0.0134, latency_ms=2100)
    (span,) = [s for s in trace.spans if s.kind == "generation"]
    assert (span.name, span.request_id, span.latency_ms, span.cost_usd) == (
        "cortex_analyst",
        "cx-req-9",
        2100,
        0.0134,
    )


def test_execution_span_summarises_the_execution():
    ex = f.execution()
    trace = build_trace(KEY, "q", f.answer(), execution=ex, turns=[turn(0)])
    (span,) = [s for s in trace.spans if s.kind == "execution"]
    assert span.input == ex.sql and span.latency_ms == 40
    assert span.output == {"rows_total": 1, "truncated": False, "query_id": "qid-1"}


def test_backend_mismatch_between_key_and_answer_is_an_error():
    with pytest.raises(ValueError, match="backend"):
        build_trace(TraceKey("r-0001", "q-001", "gpt_mcp", 1), "q", f.answer())


def test_trace_from_record_uses_record_costs_and_scores():
    rec = f.record()
    trace = trace_from_record(rec, question="Median?", public=True)
    assert trace.key == TraceKey("r-0001", "q-001", "claude_mcp", 1)
    assert trace.cost_usd == 0.0123 and dict(trace.tokens) == {"in": 1800, "out": 80, "cached": 900}
    assert trace.scores is not None and trace.scores["correct"] is True
    assert trace.public is True and trace.execution == rec.execution


# -- tracers ----------------------------------------------------------------------------------
def test_noop_tracer_sends_nothing():
    tracer: Tracer = NoopTracer()
    assert tracer.send(leaky_trace()) is None
    tracer.flush()


def test_langfuse_missing_raises_tracing_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "langfuse", None)  # import langfuse -> ImportError
    with pytest.raises(TracingUnavailable, match="uv add 'langfuse>=3,<4'"):
        LangfuseTracer(environ={"LANGFUSE_PUBLIC_KEY": "pk-lf-x", "LANGFUSE_SECRET_KEY": "s"})


def test_langfuse_without_keys_raises_tracing_unavailable(monkeypatch):
    class Mod:
        class Langfuse:
            def __init__(self, **kw):
                raise AssertionError("must not be built without keys")

    monkeypatch.setitem(sys.modules, "langfuse", Mod)
    with pytest.raises(TracingUnavailable, match="LANGFUSE_SECRET_KEY"):
        LangfuseTracer(environ={"LANGFUSE_PUBLIC_KEY": "pk-lf-x"})


def test_langfuse_client_is_built_lazily_from_env(monkeypatch):
    built = {}

    class Mod:
        class Langfuse:
            def __init__(self, **kw):
                built.update(kw)

    monkeypatch.setitem(sys.modules, "langfuse", Mod)
    LangfuseTracer(
        environ={
            "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk",
            "LANGFUSE_HOST": "https://cloud.langfuse.com",
        }
    )
    assert built == {"public_key": "pk", "secret_key": "sk", "host": "https://cloud.langfuse.com"}


def test_langfuse_tracer_sends_one_trace_with_a_span_per_turn_and_tool():
    client = FakeLangfuse()
    tracer = LangfuseTracer(client, redactor=Redactor())
    trace = build_trace(
        KEY, "Median?", f.answer(), execution=f.execution(), turns=[turn(0), turn(1)], public=True
    )
    url = tracer.send(trace)
    assert url == f"https://cloud.langfuse.com/project/p1/traces/{KEY.trace_id}"
    kinds = [k for k, _ in client.log]
    assert kinds[0] == "root" and kinds[1] == "update_trace"
    root = client.log[0][1]
    assert root["trace_context"] == {"trace_id": KEY.trace_id}
    upd = client.log[1][1]
    assert upd["public"] is True and upd["session_id"] == "r-0001"
    assert upd["name"] == "q-001 claude_mcp trial 1"
    assert upd["tags"] == ["claude_mcp"]
    gens = [kw for k, kw in client.log if k == "generation"]
    spans = [kw for k, kw in client.log if k == "span"]
    assert [g["name"] for g in gens] == ["turn 0", "turn 1"]
    assert gens[0]["usage_details"] == {"input": 1000, "output": 55}
    assert gens[0]["cost_details"] == {"total": 0.01}
    assert gens[0]["metadata"]["request_id"] == "req_0"
    assert gens[0]["metadata"]["latency_ms"] == 700
    assert [s["name"] for s in spans] == [
        "tool run_select_query",
        "tool run_select_query",
        "execute",
    ]
    ends = [kw["kind"] for k, kw in client.log if k == "end"]
    assert ends.count("root") == 1 and len(ends) == 1 + len(gens) + len(spans)
    assert kinds[-1] == "end"  # root ends last
    tracer.flush()
    assert client.flushed == 1


def test_langfuse_errors_are_marked_on_spans_and_fixes_tagged():
    client = FakeLangfuse()
    steps = [f.step(0, error="sql_error: bad column")]
    key = TraceKey("r-0001", "q-001", "claude_mcp", 2, fixes=("descriptions", "self_check"))
    LangfuseTracer(client, redactor=Redactor()).send(
        build_trace(key, "q", f.answer(steps=steps), turns=[turn(0)])
    )
    (tool,) = [kw for k, kw in client.log if k == "span"]
    assert tool["level"] == "ERROR" and tool["status_message"] == "sql_error: bad column"
    upd = next(kw for k, kw in client.log if k == "update_trace")
    assert upd["tags"] == ["claude_mcp", "descriptions", "self_check"]
    assert upd["public"] is False


def test_langfuse_spans_carry_real_durations_and_offsets():
    from datetime import UTC, datetime

    ticks = iter(range(10**12, 10**12 + 100))
    client = FakeLangfuse()
    tracer = LangfuseTracer(client, redactor=Redactor(), clock_ns=lambda: next(ticks))
    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    trace = build_trace(KEY, "q", f.answer(), turns=[turn(0), turn(1)], started_at=t0)
    tracer.send(trace)
    ends = [kw for k, kw in client.log if k == "end"]
    # root takes tick 0; each child takes the next tick at creation and ends latency later
    assert ends[0]["end_time"] == 10**12 + 1 + 700 * 1_000_000
    assert ends[1]["end_time"] == 10**12 + 2 + 701 * 1_000_000
    total = sum(s.latency_ms for s in trace.spans)
    assert ends[-1]["kind"] == "root" and ends[-1]["end_time"] == 10**12 + total * 1_000_000
    gens = [kw for k, kw in client.log if k == "generation"]
    assert gens[1]["metadata"]["start_offset_ms"] == 700
    assert gens[1]["metadata"]["end_offset_ms"] == 1401
    assert gens[1]["metadata"]["start_time"] == "2026-11-15T12:00:00.700000+00:00"
    assert gens[1]["metadata"]["end_time"] == "2026-11-15T12:00:01.401000+00:00"


def test_span_offsets_lay_spans_end_to_end():
    trace = build_trace(KEY, "q", f.answer(), execution=f.execution(), turns=[turn(0)])
    offsets = [(s.start_offset_ms, s.latency_ms) for s in trace.spans]
    for (start, lat), (nxt, _) in pairwise(offsets):
        assert nxt == start + lat
    assert offsets[0][0] == 0


@pytest.mark.safety
def test_short_secret_scrubbed_in_strings_but_numbers_kept_in_spans():
    client = FakeLangfuse()
    ex = f.execution(result=ResultSet(columns=("n",), rows=((482,),)), rows_total=1)
    trace = build_trace(KEY, "is the passcode 482?", f.answer(), execution=ex, turns=[turn(0)])
    LangfuseTracer(client, redactor=Redactor({"DEMO_PASSCODE": "482"})).send(trace)
    root = client.log[0][1]
    assert root["input"] == "is the passcode [REDACTED:DEMO_PASSCODE]?"


@pytest.mark.safety
def test_planted_secrets_never_reach_the_langfuse_client():
    client = FakeLangfuse()
    tracer = LangfuseTracer(client, redactor=Redactor(SECRETS))
    tracer.send(leaky_trace(public=True))
    sent = json.dumps(client.log, default=str)
    for planted in ALL_PLANTED:
        assert planted not in sent, planted
    assert "[REDACTED" in sent


@pytest.mark.safety
def test_default_redactor_reads_secrets_from_the_given_environment():
    client = FakeLangfuse()
    secret = "Zebra" + "Crossing99"
    tracer = LangfuseTracer(client, environ={"CLICKHOUSE_RO_PASSWORD": secret})
    trace = build_trace(KEY, f"what about {secret}?", f.answer(), turns=[turn(0)])
    tracer.send(trace)
    assert secret not in json.dumps(client.log, default=str)


def test_result_rows_are_not_sent_to_the_tracer_beyond_the_summary():
    rows = tuple((f"town-{i}", i) for i in range(500))
    ex = f.execution(result=ResultSet(columns=("t", "n"), rows=rows), rows_total=500)
    client = FakeLangfuse()
    LangfuseTracer(client, redactor=Redactor()).send(
        build_trace(KEY, "q", f.answer(), execution=ex, turns=[turn(0)])
    )
    assert "town-499" not in json.dumps(client.log, default=str)
