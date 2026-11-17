"""R3.1 offline prep, three findings carried in from other verifiers (RUN-STATE 2026-11-17):

1. a Cortex answer is timed question in to answer out (the API round trip plus the execution,
   measured by the runner around `ask_cortex`), so its latency compares with the agents';
   Cortex's own `analyst_latency_ms` is kept beside it;
2. the OWNER D-62 caveat is worded from the run stamp's Snowflake region (`run.yaml`, else
   `SELECT CURRENT_REGION()` through the read-only executor at run start), not a fixed string;
3. sampled traces are public (ANS-04), failure traces are not.

Everything runs on the fake world: no network, no accounts.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics import cli
from agentic_analytics.aggregate import latency_ms
from agentic_analytics.run import runner
from agentic_analytics.run.runner import (
    REGION_SQL,
    Services,
    run_benchmark,
    snowflake_region,
    stamped_region,
)
from agentic_analytics.schemas import Execution, Record, ResultSet
from agentic_analytics.tracing import Trace
from tests.run import world as w

pytestmark = pytest.mark.contract

ANALYST_MS = 812.5


def run(s: w.Setup, cfg=None, *, services: Any = "default", **opts: Any):
    cfg = cfg or w.config()
    svc = (lambda: s.services(cfg)) if services == "default" else services
    return run_benchmark(cfg, s.pack(), services=svc, options=w.options(s.tmp, **opts))


def rd(out: Any) -> Path:
    assert out.run_dir is not None, out.message
    return out.run_dir


def records(out: Any) -> list[Record]:
    lines = (rd(out) / "records.jsonl").read_text(encoding="utf-8").splitlines()
    return [Record.model_validate_json(ln) for ln in lines]


def manifest(out: Any) -> dict[str, Any]:
    return json.loads((rd(out) / "run.json").read_text(encoding="utf-8"))


def cortex_summary(out: Any):
    assert out.summary is not None, out.message
    return next(b for b in out.summary.backends if b.backend == "cortex_analyst")


def with_analyst_latency(rec: dict[str, Any], ms: float = ANALYST_MS) -> None:
    """Put Cortex's own `response_metadata.analyst_latency_ms` into every recorded body."""
    for body in rec["cortex"].values():
        for b in body["trials"] if isinstance(body, dict) and "trials" in body else [body]:
            b.setdefault("response_metadata", {})["analyst_latency_ms"] = ms


# -- 1. Cortex wall time ------------------------------------------------------------------------


def test_cortex_records_carry_the_ask_wall_time_and_cortexs_own_latency(tmp_path):
    s = w.setup(tmp_path)
    with_analyst_latency(s.rec)
    out = run(s, stopwatch=w.fixed_stopwatch(2_400))
    assert out.exit_code == 0, out.message
    recs = records(out)
    cortex = [r for r in recs if r.backend == "cortex_analyst"]
    agents = [r for r in recs if r.backend != "cortex_analyst"]
    assert cortex and agents
    for r in cortex:
        assert r.ask_wall_ms == 2_400 and r.analyst_latency_ms == ANALYST_MS
        assert latency_ms(r) == 2_400  # not the execution alone
        if r.execution is not None:
            assert r.execution.elapsed_ms < 2_400
    for r in agents:
        assert r.ask_wall_ms == 2_400 and r.analyst_latency_ms is None
        assert latency_ms(r) == sum(s.latency_ms for s in r.answer.steps) + (
            r.execution.elapsed_ms if r.execution is not None else 0
        )
    b = cortex_summary(out)
    assert (b.latency_p50_ms, b.latency_p95_ms) == (2_400, 2_400)


def test_cortex_wall_time_spans_the_api_call_and_the_execution(tmp_path):
    """The watch starts before the Cortex request and stops after the SQL ran: each watch
    reads 1000 ms per Cortex request plus 1 ms per Snowflake execution made while it ran."""
    s = w.setup(tmp_path)
    counter = s.counter

    def stopwatch():
        start = (counter.cortex_requests, counter.executions["snowflake"])
        return lambda: (
            1000 * (counter.cortex_requests - start[0])
            + (counter.executions["snowflake"] - start[1])
        )

    cfg = w.config(backends=("cortex_analyst",), concurrency={"cortex_analyst": 1})
    out = run(s, cfg, stopwatch=stopwatch)
    assert out.exit_code == 0, out.message
    cortex = [r for r in records(out) if r.backend == "cortex_analyst"]
    with_sql = [r for r in cortex if r.execution is not None]
    without = [r for r in cortex if r.execution is None]
    assert with_sql and all(r.ask_wall_ms == 1001 for r in with_sql)
    assert all(r.ask_wall_ms == 1000 for r in without)


def test_wall_time_and_cortex_latency_survive_the_cache_and_rescore(tmp_path):
    s = w.setup(tmp_path)
    with_analyst_latency(s.rec)
    first = run(s, stopwatch=w.fixed_stopwatch(2_400))
    again = run(
        s,
        services=lambda: (_ for _ in ()).throw(AssertionError("no services on a cached rerun")),
        stopwatch=w.fixed_stopwatch(9_999),  # never started: nothing is asked
        rescore=True,
        run_id="rescore",
    )
    assert again.exit_code == 0, again.message
    a = {(r.backend, r.question_id, r.fixes, r.trial): r for r in records(first)}
    for r in records(again):
        old = a[(r.backend, r.question_id, r.fixes, r.trial)]
        assert (r.ask_wall_ms, r.analyst_latency_ms) == (old.ask_wall_ms, old.analyst_latency_ms)
    assert first.summary is not None and again.summary is not None
    assert cortex_summary(again).latency_p50_ms == 2_400


def test_cortex_trace_span_is_the_wall_time_less_the_execution(tmp_path):
    """L-9: the exported Cortex trace adds up to the measured wall time."""
    s = w.setup(tmp_path)
    out = run(s, stopwatch=w.fixed_stopwatch(2_400), sample_every=1)
    seen = 0
    for r in records(out):
        if r.backend != "cortex_analyst" or not r.answer.trace_export:
            continue
        data = json.loads((tmp_path / r.answer.trace_export).read_text(encoding="utf-8"))
        spans = {sp["name"]: sp for sp in data["spans"]}
        executed = r.execution.elapsed_ms if r.execution is not None else 0
        assert spans["cortex_analyst"]["latency_ms"] == 2_400 - executed
        assert sum(sp["latency_ms"] for sp in data["spans"]) == 2_400
        seen += 1
    assert seen


# -- 2. the Snowflake region and the D-62 caveat ------------------------------------------------


class RegionExecutor:
    """A read-only executor stand-in that answers CURRENT_REGION() and records every SQL."""

    def __init__(self, value: Any = "aws_ap_southeast_7", *, error: str | None = None,
                 raises: BaseException | None = None) -> None:  # fmt: skip
        self.value, self.error, self.raises = value, error, raises
        self.sql: list[tuple[str, int]] = []

    def execute(self, sql: str, *, row_cap: int) -> Execution:
        self.sql.append((sql, row_cap))
        if self.raises is not None:
            raise self.raises
        if self.error is not None:
            return Execution(
                engine="snowflake",
                sql=sql,
                result=None,
                rows_total=None,
                truncated=False,
                elapsed_ms=3,
                error=self.error,  # type: ignore[arg-type]
            )
        rs = ResultSet(columns=("REGION",), rows=((self.value,),))
        return Execution(
            engine="snowflake", sql=sql, result=rs, rows_total=1, truncated=False, elapsed_ms=3
        )


def test_region_comes_from_run_yaml_without_any_query():
    ex = RegionExecutor()
    cfg = w.config(snowflake_region="AWS_US_WEST_2")
    assert snowflake_region(cfg, Services(snowflake=ex)) == {
        "region": "AWS_US_WEST_2",
        "source": "run.yaml",
    }
    assert ex.sql == []


def test_region_is_read_with_current_region_through_the_read_only_executor():
    ex = RegionExecutor(" aws_ap_southeast_7 ")
    assert snowflake_region(w.config(), Services(snowflake=ex)) == {
        "region": "AWS_AP_SOUTHEAST_7",
        "source": "current_region()",
    }
    assert ex.sql == [(REGION_SQL, 1)] and REGION_SQL == "SELECT CURRENT_REGION() AS REGION"


@pytest.mark.parametrize(
    ("ex", "error"),
    [
        (RegionExecutor(error="sql_error"), "sql_error"),
        (RegionExecutor(raises=RuntimeError("boom")), "RuntimeError"),
        (RegionExecutor("<script>"), "unreadable"),
        (RegionExecutor(42), "unreadable"),
        (RegionExecutor(None), "unreadable"),
    ],
)
def test_an_unreadable_region_is_stamped_none_and_the_run_goes_on(ex, error):
    got = snowflake_region(w.config(), Services(snowflake=ex))
    assert got == {"region": None, "source": "current_region()", "error": error}


def test_no_region_without_cortex_and_none_without_an_executor():
    ex = RegionExecutor()
    cfg = w.config(backends=("claude_mcp", "gpt_mcp"))
    assert snowflake_region(cfg, Services(snowflake=ex)) is None and ex.sql == []
    got = snowflake_region(w.config(), None)
    assert got is not None and got["region"] is None


def test_stamped_region_reads_only_a_recorded_string():
    assert stamped_region({"snowflake_region": {"region": "AWS_US_WEST_2"}}) == "AWS_US_WEST_2"
    bad: list[dict[str, Any]] = [
        {},
        {"snowflake_region": None},
        {"snowflake_region": {"region": None}},
        {"snowflake_region": "AWS_US_WEST_2"},
        {"snowflake_region": {"region": 7}},
    ]
    for m in bad:
        assert stamped_region(m) is None


def test_run_stamps_the_queried_region_and_the_dashboard_words_the_caveat_from_it(tmp_path):
    s = w.setup(tmp_path)
    out = run(s)
    assert out.exit_code == 0, out.message
    assert manifest(out)["snowflake_region"] == {
        "region": w.REGION,
        "source": "current_region()",
    }
    start = json.loads((rd(out) / "run.log").read_text(encoding="utf-8").splitlines()[0])
    assert start["event"] == "start" and start["snowflake_region"]["region"] == w.REGION
    page = (rd(out) / "index.html").read_text(encoding="utf-8")
    assert "Snowflake account in AWS ap-southeast-7 (Thailand)" in page
    assert "region not recorded" not in page


def test_run_yaml_region_wins_and_the_caveat_follows_it(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config(snowflake_region="AWS_US_WEST_2")
    calls: list[str] = []

    def services() -> Services:
        svc = s.services(cfg)
        inner = svc.snowflake.execute

        def spy(sql: str, *, row_cap: int):
            calls.append(sql)
            return inner(sql, row_cap=row_cap)

        svc.snowflake.execute = spy  # type: ignore[method-assign]
        return svc

    out = run(s, cfg, services=services)
    assert out.exit_code == 0, out.message
    assert manifest(out)["snowflake_region"] == {"region": "AWS_US_WEST_2", "source": "run.yaml"}
    assert REGION_SQL not in calls
    page = (rd(out) / "index.html").read_text(encoding="utf-8")
    assert "Snowflake account in AWS us-west-2 (Oregon)" in page and "Thailand" not in page


def test_a_fully_cached_rerun_builds_no_services_and_says_the_region_is_unknown(tmp_path):
    s = w.setup(tmp_path)
    run(s)
    out = run(
        s, services=lambda: (_ for _ in ()).throw(AssertionError("built services")), run_id="b"
    )
    assert out.exit_code == 0 and out.called == 0, out.message
    assert manifest(out)["snowflake_region"]["region"] is None
    page = (rd(out) / "index.html").read_text(encoding="utf-8")
    assert "Snowflake account region not recorded for this run" in page


@pytest.fixture
def cli_run(tmp_path, monkeypatch):
    from agentic_analytics.run.config import RUN_YAML

    monkeypatch.chdir(tmp_path)
    s = w.setup(tmp_path)
    text = RUN_YAML.replace("trials: 3", "trials: 1").replace("max_cost_usd: 25.0", "")
    (tmp_path / "run.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(runner, "configured_engines", lambda env: ("clickhouse", "snowflake"))
    monkeypatch.setattr(runner, "services_from_env", lambda cfg, pack, env: s.services(cfg))
    common = ["--pack", "./pack", "--measurements", "measurements", "--resamples", "200"]
    assert cli.main(["run", "--json", *common]) == 0
    return tmp_path


def test_report_and_reconcile_rerender_with_the_stamped_region(cli_run):
    run_dir = runner.resolve_run(cli_run / "results", "latest")
    page = run_dir / "index.html"
    page.unlink()
    assert cli.main(["report", "--run", "latest", "--json"]) == 0
    assert "AWS ap-southeast-7 (Thailand)" in page.read_text(encoding="utf-8")
    page.unlink()
    credits = cli_run / "credits.json"
    credits.write_text('{"clickhouse": 0.2, "snowflake": 0.1}', encoding="utf-8")
    assert cli.main(["reconcile", "--run", "latest", "--credits", str(credits), "--json"]) == 0
    assert "AWS ap-southeast-7 (Thailand)" in page.read_text(encoding="utf-8")


# -- 3. public sample traces (ANS-04) -----------------------------------------------------------


class SpyTracer:
    def __init__(self) -> None:
        self.sent: list[Trace] = []

    def send(self, trace: Trace) -> str | None:
        self.sent.append(trace)
        return None

    def flush(self) -> None:
        pass


def test_sampled_traces_are_public_and_failures_are_not(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, sample_every=1)  # every correct answer is a sample
    recs = records(out)
    kinds = set()
    for r in recs:
        assert r.answer.trace_export, r.question_id
        data = json.loads((tmp_path / r.answer.trace_export).read_text(encoding="utf-8"))
        correct = r.scores.correct if not r.question_id.startswith("p-") else None
        if correct is None:
            continue
        assert data["public"] is correct, (r.question_id, r.backend)
        kinds.add(correct)
    assert kinds == {True, False}


def test_the_tracer_gets_public_only_for_samples(tmp_path):
    s = w.setup(tmp_path)
    tracer = SpyTracer()
    cfg = w.config()

    def services() -> Services:
        svc = s.services(cfg)
        svc.tracer = tracer
        return svc

    out = run(s, cfg, services=services, sample_every=3)
    assert out.exit_code == 0, out.message
    from agentic_analytics.tracing import select_for_export

    by_key = {(t.key.question_id, t.key.backend, t.key.trial, t.key.fixes): t for t in tracer.sent}
    assert len(by_key) == out.called
    reasons = set()
    for r in records(out):
        t = by_key[(r.question_id, r.backend, r.trial, r.fixes)]
        correct = r.scores.correct
        if r.question_id.startswith("p-"):
            from agentic_analytics.aggregate import probe_passed

            correct = probe_passed(r)
        reason = select_for_export(t.key, correct=correct, sample_every=3)
        reasons.add(reason)
        assert t.public is (reason == "sample"), (r.question_id, r.backend, reason)
    assert reasons == {"sample", "failure", None}


def test_wall_time_is_a_whole_non_negative_number_of_ms():
    watch = runner.perf_stopwatch()
    ms = watch()
    assert isinstance(ms, int) and ms >= 0 and not math.isnan(ms)


def test_summary_is_unchanged_by_the_new_fields_when_nothing_was_timed():
    """Records made before R3.1 timed asks keep M2.1's execution-only Cortex latency."""
    from tests.aggregate.helpers import rec

    r = rec("q-001", backend="cortex_analyst", elapsed_ms=900)
    assert r.ask_wall_ms is None and latency_ms(r) == 900
    assert "ask_wall_ms" not in r.model_dump_json()
