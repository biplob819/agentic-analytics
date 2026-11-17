"""RUN-01..RUN-03 and RUN-05: the runner end to end on the fake world (no network)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.cost import AgentAssumptions
from agentic_analytics.run.cache import AnswerCache
from agentic_analytics.run.config import ASSUMPTIONS_FILE, load_assumptions, save_assumptions
from agentic_analytics.run.runner import (
    Services,
    agent_prompt_hash,
    cortex_prompt_hash,
    run_benchmark,
)
from agentic_analytics.schemas import Execution, Record, RunSummary
from tests.mcp import fake_server
from tests.models.fakes import Status
from tests.run import world as w

pytestmark = pytest.mark.contract


def run(s: w.Setup, cfg=None, *, services: Any = "default", **opts: Any):
    cfg = cfg or w.config()
    svc = (lambda: s.services(cfg)) if services == "default" else services
    return run_benchmark(cfg, s.pack(), services=svc, options=w.options(s.tmp, **opts))


def rd(out: Any) -> Path:
    assert out.run_dir is not None, out.message
    return out.run_dir


def sm(out: Any) -> RunSummary:
    assert out.summary is not None, out.message
    return out.summary


def records(out) -> list[Record]:
    lines = (rd(out) / "records.jsonl").read_text(encoding="utf-8").splitlines()
    return [Record.model_validate_json(ln) for ln in lines]


def no_services() -> Services:
    raise AssertionError("services were built although every answer is cached")


# -- RUN-01 / RUN-03: a full run ---------------------------------------------------------------


def test_full_run_writes_records_log_summary_and_report(tmp_path):
    s = w.setup(tmp_path)
    out = run(s)
    assert out.exit_code == 0, out.message
    n_q, n_p = len(s.world.questions), len(s.world.probes)
    assert out.jobs == 3 * n_q + 3 * n_p == out.called
    recs = records(out)
    assert len(recs) == out.jobs
    assert len({(r.backend, r.question_id, r.fixes, r.trial) for r in recs}) == out.jobs
    run_dir = rd(out)
    for name in ("summary.json", "index.html", "summary.vl.json", "run.json", "questions.jsonl"):
        assert (run_dir / name).is_file(), name
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    assert summary == out.summary and summary.gold_mode == "cross_engine"
    assert {b.backend for b in summary.backends} == {"claude_mcp", "gpt_mcp", "cortex_analyst"}
    assert set(summary.probes) == set(w.PROBE_IDS) and out.probes_failed == ()
    assert (tmp_path / "results" / "latest").read_text(encoding="utf-8").strip() == out.run_id


def test_probe_gold_reaches_scoring(tmp_path):
    """Q2.2 stores probe gold; the runner scores probes with gold against it."""
    s = w.setup(tmp_path)
    out = run(s)
    with_gold = {p.id for p in s.world.probes if p.gold_sql is not None}
    assert with_gold
    for r in records(out):
        if r.question_id in with_gold:
            assert r.scores.result_match is not None, (r.question_id, r.backend)
        elif r.question_id.startswith("p-"):
            assert r.scores.result_match is None


def test_every_record_is_stamped(tmp_path):
    s = w.setup(tmp_path)
    out = run(s)
    check = json.loads((s.pack_dir / "gold" / "gold_check.json").read_text(encoding="utf-8"))
    for r in records(out):
        assert r.git_sha == "abc1234"
        assert r.gold_check_id == check["gold_check_id"]
        assert r.snapshot_sha256 == w.SNAPSHOT
        assert r.seed == 7 and r.run_id == out.run_id
        want = agent_prompt_hash(w.AS_OF) if r.backend != "cortex_analyst" else None
        assert r.prompt_hash == (want or cortex_prompt_hash(w.AS_OF))
        assert len(r.fix_hash) == 64 and len(r.semantic_hash) == 64
        assert r.warehouse_credits_alloc is None


def test_run_log_is_json_lines_from_one_writer(tmp_path):
    s = w.setup(tmp_path)
    out = run(s)
    lines = (rd(out) / "run.log").read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln) for ln in lines]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "finish"
    asks = [e for e in events if e["event"] == "ask"]
    assert len(asks) == out.jobs and all(e["cache_hit"] is False for e in asks)
    assert all({"node", "cache_key", "cache_hit"} <= set(e) for e in asks)
    # the cache key is logged in clear (a field named `key` would be redacted) and names the entry
    cache_files = {f.stem for f in (tmp_path / ".cache").rglob("*.json")}
    assert all(re.fullmatch(r"[0-9a-f]{64}", e["cache_key"]) for e in asks)
    assert {e["cache_key"] for e in asks} <= cache_files
    scores = [e for e in events if e["event"] == "score"]
    assert len(scores) == out.jobs and all("latency_ms" in e and "cost_usd" in e for e in scores)
    assert sum(e["event"] == "fingerprint" for e in events) == 2


def test_ledger_gets_one_line_per_spending_run(tmp_path):
    s = w.setup(tmp_path)
    out = run(s)
    rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["run_id"] == out.run_id and rows[0]["spent_usd"] > 0
    # trial credits are recorded too: each fresh Cortex message at the published rate
    assert out.run_dir is not None
    records = [
        json.loads(x) for x in (out.run_dir / "records.jsonl").read_text("utf-8").splitlines()
    ]
    messages = sum(r["cortex_messages"] for r in records)
    assert messages > 0
    assert rows[0]["credits"] == {"snowflake": round(messages * 0.067, 6)}
    assert f"{messages} Cortex messages" in rows[0]["note"]
    again = run(s, services=no_services, run_id="rerun")
    assert again.exit_code == 0 and again.called == 0
    assert len((tmp_path / "ledger.jsonl").read_text("utf-8").splitlines()) == 1


def test_failures_and_samples_export_traces(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, sample_every=1)
    recs = records(out)
    assert all(r.answer.trace_export for r in recs)
    for r in recs:
        assert (tmp_path / r.answer.trace_export).is_file()  # results/<run>/traces/...
    out2 = run(w.setup(tmp_path / "b"), sample_every=10_000)
    wrong = [
        r for r in records(out2) if not r.scores.correct and not r.question_id.startswith("p-")
    ]
    assert wrong and all(r.answer.trace_export for r in wrong)


def test_per_backend_concurrency_is_respected(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp", "gpt_mcp"), concurrency={"claude_mcp": 3, "gpt_mcp": 1})
    live: dict[str, int] = {"claude_mcp": 0, "gpt_mcp": 0}
    peak: dict[str, int] = {"claude_mcp": 0, "gpt_mcp": 0}

    class Slow(w.ReplayModel):
        async def step(self, conv):
            b = "claude_mcp" if self.provider == "anthropic" else "gpt_mcp"
            live[b] += 1
            peak[b] = max(peak[b], live[b])
            await asyncio.sleep(0.005)
            live[b] -= 1
            return await super().step(conv)

    def services() -> Services:
        svc = s.services(cfg)
        for b in ("claude_mcp", "gpt_mcp"):
            svc.models[b] = Slow(b, s.rec["agents"][b], s.counter)
        return svc

    out = run(s, cfg, services=services, engines=("clickhouse", "snowflake"))
    assert out.exit_code == 0, out.message
    assert peak == {"claude_mcp": 3, "gpt_mcp": 1}
    assert s.counter.mcp_connections == 4  # one connection per worker


@pytest.mark.safety
def test_agent_queries_pass_the_sql_guard_before_the_mcp_server(tmp_path):
    s = w.setup(tmp_path)
    q = s.world.questions[0]
    script = s.rec["agents"]["claude_mcp"][q.text]
    script[0]["calls"][0]["arguments"]["query"] = "DELETE FROM uk.uk_price_paid WHERE 1 = 1"
    fake_server.CALLS.clear()
    out = run(s, w.config(backends=("claude_mcp",)))
    assert out.exit_code == 0, out.message
    rec = next(r for r in records(out) if r.question_id == q.id)
    assert (rec.answer.steps[0].error or "").startswith("write_refused")
    assert rec.scores.write_attempted is True
    assert not any("DELETE" in str(args) for _, args in fake_server.CALLS)


# -- RUN-02: cache, resume, rescore ------------------------------------------------------------


def test_rerun_is_all_cache_hits_with_no_calls(tmp_path):
    s = w.setup(tmp_path)
    first = run(s)
    calls = s.counter.total
    assert calls > 0
    second = run(s, services=no_services, run_id="second")
    assert second.exit_code == 0 and second.called == 0 and second.cached == second.jobs
    assert s.counter.total == calls
    log = [json.loads(x) for x in (rd(second) / "run.log").read_text("utf-8").splitlines()]
    cached_asks = [e for e in log if e["event"] == "ask"]
    assert len(cached_asks) == second.jobs and all(e["cache_hit"] for e in cached_asks)
    a = sm(first).model_dump(exclude={"run_id"})
    b = sm(second).model_dump(exclude={"run_id"})
    assert a == b


def test_resume_after_interruption_only_calls_what_is_missing(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp",), concurrency={"claude_mcp": 1})

    class Dies(w.ReplayModel):
        async def step(self, conv):
            if self.counter.model_steps >= 7:
                raise Status(401)  # a config error: stops the run (EK L-21)
            return await super().step(conv)

    def dying() -> Services:
        svc = s.services(cfg)
        svc.models["claude_mcp"] = Dies("claude_mcp", s.rec["agents"]["claude_mcp"], s.counter)
        return svc

    first = run(s, cfg, services=dying)
    assert first.exit_code == 2 and "rerun the same command" in first.message
    done = first.called
    assert 0 < done < first.jobs
    assert len(list((tmp_path / ".cache").rglob("*.json"))) == done
    before = s.counter.model_steps
    second = run(s, cfg, run_id="resumed")
    assert second.exit_code == 0, second.message
    assert second.cached == done and second.called == second.jobs - done
    assert second.cumulative_usd > second.spent_usd > 0  # spend behind all answers, cached too
    assert "model spend behind all answers" in second.message
    expected_steps = sum(
        len(s.rec["agents"]["claude_mcp"][j])
        for j in [*(q.text for q in s.world.questions), *(p.text for p in s.world.probes)]
    )
    assert s.counter.model_steps - before < expected_steps  # cached asks were not replayed


def test_rescore_recomputes_from_cache_with_no_calls(tmp_path):
    s = w.setup(tmp_path)
    first = run(s)
    calls = s.counter.total
    again = run(s, services=None, rescore=True, kind="rescore", run_id="rescored")
    assert again.exit_code == 0, again.message
    assert s.counter.total == calls
    assert sm(again).model_dump(exclude={"run_id"}) == sm(first).model_dump(exclude={"run_id"})


def test_rescore_with_missing_answers_exits_2(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, services=None, rescore=True)
    assert out.exit_code == 2 and "--rescore needs every answer cached" in out.message
    assert s.counter.total == 0


def test_cache_key_change_misses(tmp_path):
    s = w.setup(tmp_path)
    run(s)
    out = run(s, w.config(seed=7, max_turns=9), services=None, rescore=True)
    assert out.exit_code == 2  # a loop limit is part of the key
    keys = list((tmp_path / ".cache").rglob("*.json"))
    assert all(AnswerCache(tmp_path / ".cache").get(p.stem) is not None for p in keys)


# -- §9: estimate, dry run, cost cap -----------------------------------------------------------


def test_dry_run_makes_no_calls_and_writes_nothing(tmp_path):
    s = w.setup(tmp_path)
    said: list[str] = []
    out = run(s, services=no_services, dry_run=True, say=said.append)
    assert out.exit_code == 0 and out.estimate_usd > 0 and out.remaining_usd == out.estimate_usd
    assert s.counter.total == 0
    assert not (tmp_path / "results").exists() and not (tmp_path / ".cache").exists()
    assert "Estimate $" in said[0]


def test_estimate_over_the_cap_stops_before_any_call(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, w.config(max_cost_usd=0.01))
    assert out.exit_code == 3 and "nothing was called" in out.message
    assert s.counter.total == 0 and not (tmp_path / "results").exists()


def _tiny_estimates(s: w.Setup) -> None:
    tiny = AgentAssumptions(
        turns=0.01,
        prompt_tokens_per_turn=10,
        cached_share=0.0,
        output_tokens_per_turn=1,
        reasoning_tokens_per_turn=0,
    )
    save_assumptions(s.tmp / "measurements" / ASSUMPTIONS_FILE, {"claude_mcp": tiny})


def test_cost_cap_stops_mid_run_with_a_resume_hint_then_resumes(tmp_path):
    s = w.setup(tmp_path)
    _tiny_estimates(s)  # estimates far below the recorded costs, so the cap bites mid-run
    cfg = w.config(backends=("claude_mcp",), concurrency={"claude_mcp": 1}, max_cost_usd=0.03)
    out = run(s, cfg)
    assert out.exit_code == 3, out.message
    assert "rerun the same command to resume" in out.message
    assert 0 < out.called < out.jobs
    assert out.spent_usd <= 0.03 + 0.02  # one in-flight ask at most past the last check
    assert not (rd(out) / "summary.json").exists()
    log = [json.loads(x) for x in (rd(out) / "run.log").read_text("utf-8").splitlines()]
    assert any(e["event"] == "stop" for e in log)
    resumed = run(s, w.config(backends=("claude_mcp",)), run_id="resumed")
    assert resumed.exit_code == 0 and resumed.cached == out.called
    assert resumed.called == resumed.jobs - out.called


# -- RUN-05: smoke writes measured tokens back to the cost model -------------------------------


def test_smoke_measures_tokens_into_the_cost_model(tmp_path):
    s = w.setup(tmp_path)
    before = run(s, dry_run=True, services=no_services).estimate_usd
    out = run(s, kind="smoke", include_probes=False)
    assert out.exit_code == 0 and out.jobs == 3 * 12
    measured = load_assumptions(tmp_path / "measurements" / ASSUMPTIONS_FILE)
    assert set(measured) == {"claude_mcp", "gpt_mcp"}
    assert 1 <= measured["claude_mcp"].turns <= 2
    after = run(s, dry_run=True, services=no_services).estimate_usd
    assert after < before  # measured replay turns are cheaper than the planning figures


# -- D-29: single-engine mode ------------------------------------------------------------------


def test_single_engine_run_is_stamped(tmp_path):
    s = w.setup(tmp_path, engines=("clickhouse",))
    cfg = w.config(backends=("claude_mcp", "gpt_mcp"))
    out = run(s, cfg, engines=("clickhouse",))
    assert out.exit_code == 0, out.message
    assert sm(out).gold_mode == "single_engine" and out.gold_mode == "single_engine"
    assert "single-engine" in (rd(out) / "index.html").read_text(encoding="utf-8")


def test_two_configured_engines_stay_cross_engine(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)), engines=("clickhouse", "snowflake"))
    assert out.exit_code == 0 and sm(out).gold_mode == "cross_engine"


def test_backend_whose_engine_is_not_configured_exits_2(tmp_path):
    s = w.setup(tmp_path, engines=("clickhouse",))
    out = run(s, w.config(), engines=("clickhouse",))
    assert out.exit_code == 2 and "snowflake" in out.message and s.counter.total == 0


# -- exit codes and failures -------------------------------------------------------------------


def test_followed_injection_fails_the_probe_and_exits_1(tmp_path):
    s = w.setup(tmp_path)
    probe = next(p for p in s.world.probes if p.kind == "injection_question")
    final = s.rec["agents"]["claude_mcp"][probe.text][-1]["calls"][0]["arguments"]
    final["answer"] += " IQ-ALPHA-7"
    out = run(s)
    assert out.exit_code == 1 and f"{probe.id}:claude_mcp" in out.probes_failed
    assert out.summary is not None and (rd(out) / "summary.json").is_file()


def test_row_count_change_during_write_probes_exits_1(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config()

    class Drift:
        engine = "clickhouse"

        def __init__(self, inner: Any) -> None:
            self.inner, self.counts = inner, 0

        def execute(self, sql: str, *, row_cap: int) -> Execution:
            ex = self.inner.execute(sql, row_cap=row_cap)
            if sql.startswith("SELECT count() AS n"):
                self.counts += 1
                if self.counts > 1:
                    data = ex.model_dump()
                    data["result"] = {"columns": ["n"], "rows": [[1]]}
                    return Execution.model_validate(data)
            return ex

    def services() -> Services:
        svc = s.services(cfg)
        svc.clickhouse = Drift(svc.clickhouse)
        return svc

    out = run(s, cfg, services=services)
    assert out.exit_code == 1 and "row counts changed" in out.message


@pytest.mark.parametrize("fixes", [(), ("examples",)])
def test_any_run_refuses_without_frozen_fixes(tmp_path, fixes):
    s = w.setup(tmp_path, frozen=False)
    out = run(s, w.config(fixes=fixes))
    assert out.exit_code == 1 and "FROZEN.json" in out.message and s.counter.total == 0


@pytest.mark.parametrize("fixes", [(), ("examples",)])
def test_runs_refuse_when_a_fix_file_changed_after_the_freeze(tmp_path, fixes):
    s = w.setup(tmp_path)
    p = s.pack_dir / "fixes" / "examples.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    out = run(s, w.config(fixes=fixes))
    assert out.exit_code == 1 and "changed after the freeze" in out.message
    assert s.counter.total == 0


def test_fix_run_refuses_a_fix_hash_other_than_its_baselines_unless_forced(tmp_path):
    from agentic_analytics.fixes import freeze_fixes, load_frozen

    s = w.setup(tmp_path)
    claude = ("claude_mcp",)
    base = run(s, w.config(backends=claude), include_probes=False)
    assert base.exit_code == 0
    old = load_frozen(s.pack())
    assert old is not None
    p = s.pack_dir / "fixes" / "examples.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# reviewed\n", encoding="utf-8")
    freeze_fixes(s.pack(), force=True, reason="D-99 test re-freeze")
    new = load_frozen(s.pack())
    assert new is not None and new.fix_hash != old.fix_hash
    cfg = w.config(backends=claude, fixes=("examples",))
    refused = run(s, cfg, include_probes=False, run_id="refused")
    assert refused.exit_code == 1 and "D-52" in refused.message
    forced = run(s, cfg, include_probes=False, run_id="forced", fix_hash_reason="owner: D-99")
    assert forced.exit_code == 0, forced.message
    run_json = json.loads((rd(forced) / "run.json").read_text(encoding="utf-8"))
    assert run_json["fix_hash_override"]["reason"] == "owner: D-99"
    assert run_json["fix_freeze_reason"] == "D-99 test re-freeze"
    log = (rd(forced) / "run.log").read_text(encoding="utf-8")
    assert '"event": "fix_hash_override"' in log
    hashes = {r.fixes: r.fix_hash for r in records(forced)}
    assert hashes[()] == old.fix_hash and hashes[("examples",)] == new.fix_hash


def test_fix_runs_use_f31_blocks_and_stamp_the_frozen_fix_hash(tmp_path):
    from agentic_analytics.fixes import fix_blocks, load_frozen

    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp", "cortex_analyst"), fixes=("descriptions", "examples"))
    svc = s.services(cfg)
    out = run(s, cfg, services=lambda: svc)
    assert out.exit_code == 0, out.message
    recs = records(out)
    claude = [r for r in recs if r.backend == "claude_mcp"]
    assert {r.fixes for r in claude} == {(), ("descriptions",), ("examples",)}
    assert all(r.fixes == () for r in recs if r.backend == "cortex_analyst")
    frozen = load_frozen(s.pack())
    assert frozen is not None and {r.fix_hash for r in recs} == {frozen.fix_hash}
    blocks = fix_blocks(s.pack())
    systems = svc.models["claude_mcp"].systems
    per_set = len(claude) // 3
    assert sum(blocks["examples"].strip() in sp for sp in systems) == per_set
    assert sum(blocks["descriptions"].strip() in sp for sp in systems) == per_set
    run_json = json.loads((rd(out) / "run.json").read_text(encoding="utf-8"))
    assert run_json["fix_hash"] == frozen.fix_hash


def test_self_check_fix_wires_make_self_check_into_the_agent_loop(tmp_path, monkeypatch):
    import agentic_analytics.fixes as fixes_pkg

    s = w.setup(tmp_path)
    seen: list[str] = []
    real = fixes_pkg.make_self_check

    def spy(executor, rules, **kw):
        assert executor.engine == "clickhouse"
        hook = real(executor, rules, **kw)

        async def wrapped(sql: str, **kw):
            seen.append(sql)
            return await hook(sql, **kw)

        return wrapped

    monkeypatch.setattr(fixes_pkg, "make_self_check", spy)
    out = run(s, w.config(backends=("gpt_mcp",), fixes=("self_check",)))
    assert out.exit_code == 0, out.message
    fixed = [r for r in records(out) if r.fixes == ("self_check",)]
    assert fixed and seen  # the hook ran on the agents' first SQL answers
    assert set(seen) <= {r.answer.sql for r in fixed}


# -- cache key contents (D-1) -----------------------------------------------------------------


def test_changing_effort_misses_the_cache(tmp_path):
    s = w.setup(tmp_path)
    claude = ("claude_mcp",)
    assert run(s, w.config(backends=claude), include_probes=False).exit_code == 0
    again = run(
        s, w.config(backends=claude, effort="high"), services=None, rescore=True,
        include_probes=False,
    )  # fmt: skip
    assert again.exit_code == 2 and "needs every answer cached" in again.message
    same = run(
        s,
        w.config(backends=claude),
        services=None,
        rescore=True,
        include_probes=False,
        run_id="same",
    )
    assert same.exit_code == 0


def test_each_trial_has_its_own_cache_key(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp",), trials=3)
    out = run(s, cfg, include_probes=False)
    assert out.exit_code == 0 and out.jobs == 3 * len(s.world.questions)
    entries = list((tmp_path / ".cache").rglob("*.json"))
    assert len(entries) == out.jobs
    keys = {(r.question_id, r.trial) for r in records(out)}
    assert len(keys) == out.jobs and {t for _, t in keys} == {1, 2, 3}


def test_answer_cache_ignores_gold_but_the_scoring_stamp_does_not():
    from agentic_analytics.run.runner import ask_hash, item_hash

    q = next(q for q in w.World.from_bundled().questions if q.expected == "answer")
    edited = q.model_copy(
        update={"author": "owner", "owner_reviewed": not q.owner_reviewed, "gold_sql": None}
    )
    assert ask_hash(edited) == ask_hash(q)
    assert item_hash(edited) != item_hash(q)
    assert ask_hash(q.model_copy(update={"text": q.text + " Please."})) != ask_hash(q)


def test_semantic_model_keys_fix_runs_and_cortex_not_mcp_baselines(tmp_path):
    from agentic_analytics.run.runner import plan_jobs

    world = w.World.from_bundled()
    cfg = w.config(fixes=("examples",))
    kw = dict(snapshot_sha256=w.SNAPSHOT, fix_blocks={"examples": "x"}, semantic_view="DB.S.V")
    a = plan_jobs(cfg, world.questions, [], semantic_hash="1" * 64, **kw)  # type: ignore[arg-type]
    b = plan_jobs(cfg, world.questions, [], semantic_hash="2" * 64, **kw)  # type: ignore[arg-type]
    for ja, jb in zip(a, b, strict=True):
        baseline_agent = ja.backend != "cortex_analyst" and not ja.fixes
        assert (ja.key == jb.key) == baseline_agent, ja.label
    c = plan_jobs(
        cfg, world.questions, [], semantic_hash="1" * 64, **{**kw, "semantic_view": "X.Y.Z"}
    )  # type: ignore[arg-type]
    for ja, jc in zip(a, c, strict=True):
        assert (ja.key == jc.key) == (ja.backend != "cortex_analyst"), ja.label


# -- transient failures, crashes, redaction -----------------------------------------------------


def test_transport_failures_are_not_cached_and_resume_retries_them(tmp_path):
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp",))
    out = run(s, cfg, services=lambda: s.services(cfg, fail_with=Status(503)), include_probes=False)
    assert out.exit_code == 0, out.message
    assert {r.answer.error for r in records(out)} == {"transport"}
    assert list((tmp_path / ".cache").rglob("*.json")) == []
    healthy = run(s, cfg, run_id="healthy", include_probes=False)
    assert healthy.exit_code == 0 and healthy.cached == 0 and healthy.called == healthy.jobs
    assert all(r.answer.error is None for r in records(healthy))


def test_a_crash_after_the_asks_still_closes_the_run(tmp_path, monkeypatch):
    import agentic_analytics.report as report

    def broken(*a, **k):
        raise OSError("disk full while rendering")

    monkeypatch.setattr(report, "render_report", broken)
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)))
    assert out.exit_code == 2 and "disk full" in out.message and out.called > 0
    run_json = json.loads((rd(out) / "run.json").read_text(encoding="utf-8"))
    assert run_json["finished_at"] is not None and "disk full" in run_json["error"]
    assert run_json["exit_code"] == 2
    lines = (rd(out) / "run.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "finish"
    assert any(json.loads(x)["event"] == "error" for x in lines)
    ledger = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1 and json.loads(ledger[0])["run_id"] == out.run_id
    assert len(list((tmp_path / ".cache").rglob("*.json"))) == out.called


@pytest.mark.safety
def test_secrets_in_exception_text_are_redacted(tmp_path):
    key = "sk-ant-api03-" + "Q" * 40
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp",))
    boom = RuntimeError(f"auth failed for {key}")
    out = run(s, cfg, services=lambda: s.services(cfg, fail_with=boom))
    assert out.exit_code == 2 and key not in out.message and "REDACTED" in out.message
    log = (rd(out) / "run.log").read_text(encoding="utf-8")
    assert key not in log and "REDACTED" in log
    assert key not in (rd(out) / "run.json").read_text(encoding="utf-8")


def test_code_version_reads_the_package_checkout_not_the_cwd(tmp_path, monkeypatch):
    import subprocess

    from agentic_analytics import __version__
    from agentic_analytics.run.runner import code_version

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "__init__.py").write_text("", encoding="utf-8")
    v = code_version(plain)
    assert v.sha == "0000000" and v.dirty is None and v.label == f"v{__version__}"
    repo = tmp_path / "repo" / "pkg"
    repo.mkdir(parents=True)
    (repo / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", str(repo.parent)], check=True)
    subprocess.run([*git, "-C", str(repo.parent), "add", "."], check=True)
    subprocess.run([*git, "-C", str(repo.parent), "commit", "-q", "-m", "c"], check=True)
    monkeypatch.chdir(plain)  # the current folder is not what is read
    clean = code_version(repo)
    assert len(clean.sha) == 40 and clean.dirty is False and clean.label == clean.sha
    (repo / "__init__.py").write_text("x = 2\n", encoding="utf-8")
    dirty = code_version(repo)
    assert dirty.sha == clean.sha and dirty.dirty is True and dirty.label.endswith("+dirty")
    untracked = tmp_path / "repo" / "other"
    untracked.mkdir()
    (untracked / "__init__.py").write_text("", encoding="utf-8")
    assert code_version(untracked).sha == "0000000"  # a package the checkout does not track


def test_run_json_records_the_code_version(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)), git_sha=None)
    run_json = json.loads((rd(out) / "run.json").read_text(encoding="utf-8"))
    code = run_json["code"]
    assert set(code) >= {"git_sha", "dirty", "version", "label"}
    assert {r.git_sha for r in records(out)} == {code["git_sha"]}


def test_incomplete_services_exit_2(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, services=lambda: Services())
    assert out.exit_code == 2 and "cannot run" in out.message


def test_existing_run_folder_is_never_overwritten(tmp_path):
    s = w.setup(tmp_path)
    run(s, run_id="fixed")
    out = run(s, run_id="fixed", services=no_services)
    assert out.exit_code == 2 and "already exists" in out.message


def test_selected_questions_only(tmp_path):
    s = w.setup(tmp_path)
    ids = [q.id for q in s.world.questions[:3]]
    out = run(s, question_ids=ids, include_probes=False)
    assert out.exit_code == 0 and {r.question_id for r in records(out)} == set(ids)
    bad = run(s, question_ids=["q-999"], services=no_services)
    assert bad.exit_code == 2


def test_outcome_json_shape(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, dry_run=True, services=no_services)
    assert set(out.to_dict()) == {
        "ok",
        "exit_code",
        "message",
        "run_id",
        "run_dir",
        "jobs",
        "cached",
        "called",
        "estimate_usd",
        "remaining_usd",
        "warehouse_usd",
        "spent_usd",
        "cumulative_usd",
        "gold_mode",
        "probes_failed",
    }
    assert json.loads(json.dumps(out.to_dict()))["ok"] is True
    assert isinstance(Path(".").resolve(), Path)


def test_cost_cap_covers_model_spend_not_trial_credits(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("cortex_analyst",), max_cost_usd=0.01))
    assert out.exit_code == 0, out.message  # Cortex is paid in credits: reported, not capped
    assert out.estimate_usd == 0 and out.warehouse_usd > 0 and out.spent_usd == 0


# -- R3.1 verifier round 1 ------------------------------------------------------------------------


@pytest.mark.parametrize("expects_empty", [True, False])
def test_the_self_check_hook_is_told_whether_the_question_expects_no_rows(
    monkeypatch, expects_empty
):
    """F3.1's hand-off: a correct empty answer to an expects_empty question gets no feedback."""
    import functools as ft

    import agentic_analytics.backends as backends
    from agentic_analytics.questions import load_questions
    from agentic_analytics.run.runner import Job, Services, ask_agent

    q = next(q for q in load_questions("uk_price_paid") if q.expects_empty is expects_empty)
    seen: dict = {}

    class Stop(Exception):
        pass

    async def fake_ask_mcp(text, **kw):
        seen["hook"] = kw["self_check"]
        raise Stop

    async def hook(sql, *, expects_empty=False):
        return None

    monkeypatch.setattr(backends, "ask_mcp", fake_ask_mcp)
    job = Job("gpt_mcp", q, ("self_check",), 1, None, "k")  # type: ignore[arg-type]
    services = Services(models={"gpt_mcp": object()}, self_check=hook)
    with pytest.raises(Stop):
        asyncio.run(ask_agent(job, w.config(), services, lambda: None, {}, 60.0))
    bound = seen["hook"]
    assert isinstance(bound, ft.partial) and bound.func is hook
    assert bound.keywords == {"expects_empty": expects_empty}
    base = Job("gpt_mcp", q, (), 1, None, "k")  # type: ignore[arg-type]
    with pytest.raises(Stop):
        asyncio.run(ask_agent(base, w.config(), services, lambda: None, {}, 60.0))
    assert seen["hook"] is None  # baseline answers get no self-check


def test_records_stamp_the_whole_question_hash_not_the_answer_cache_hash(tmp_path):
    from agentic_analytics.run.runner import ask_hash, item_hash

    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)), include_probes=False)
    by_id = {q.id: q for q in s.world.questions}
    for r in records(out):
        assert r.question_hash == item_hash(by_id[r.question_id])
        assert r.question_hash != ask_hash(by_id[r.question_id])


def test_the_raw_self_check_rules_never_reach_the_system_prompt(tmp_path, monkeypatch):
    import agentic_analytics.backends as backends

    s = w.setup(tmp_path)
    real = backends.ask_mcp
    blocks_seen: list[list[str]] = []

    async def spy(text, **kw):
        blocks_seen.append(list(kw["fix_blocks"]))
        return await real(text, **kw)

    monkeypatch.setattr(backends, "ask_mcp", spy)
    out = run(s, w.config(backends=("gpt_mcp",), fixes=("self_check",)), include_probes=False)
    assert out.exit_code == 0, out.message
    rules_text = (w.BUNDLED.root / "fixes" / "self_check.yaml").read_text(encoding="utf-8")
    assert blocks_seen and all(rules_text not in b for bl in blocks_seen for b in bl)
    assert all(bl == [] for bl in blocks_seen)  # self_check alone adds no prompt block


def test_a_long_secret_in_an_error_is_redacted_before_the_text_is_cut(tmp_path):
    head = "-----BEGIN " + "PRIVATE KEY-----"
    body = "\n".join(["MIIEv" + "Q" * 59] * 12)
    pem = f"{head}\n{body}\n{head.replace('BEGIN', 'END')}\n"
    key = "sk-ant-api03-" + "Z" * 400  # longer than the 300-character cut
    s = w.setup(tmp_path)
    cfg = w.config(backends=("claude_mcp",))
    for i, secret in enumerate((pem, key)):
        boom = RuntimeError(f"auth failed: {secret}")
        out = run(s, cfg, services=lambda b=boom: s.services(cfg, fail_with=b), run_id=f"leak{i}")
        log = (rd(out) / "run.log").read_text(encoding="utf-8")
        for text in (out.message, log, (rd(out) / "run.json").read_text(encoding="utf-8")):
            assert "QQQQQQQQQQ" not in text and "ZZZZZZZZZZ" not in text


def test_probes_fan_out_only_to_the_backends_they_name():
    from agentic_analytics.questions import load_probes
    from agentic_analytics.run.runner import plan_jobs

    probes = list(load_probes("uk_price_paid"))
    data_probes = {p.id for p in probes if p.kind == "injection_data"}
    assert data_probes and all(
        "cortex_analyst" not in p.backends for p in probes if p.id in data_probes
    )
    cfg = w.config(backends=("claude_mcp", "gpt_mcp", "cortex_analyst"), trials=1)
    jobs = plan_jobs(
        cfg, [], probes, snapshot_sha256="a" * 64, fix_blocks={}, semantic_hash="b" * 64
    )
    asked = {(j.backend, j.item.id) for j in jobs}
    for p in probes:
        for b in ("claude_mcp", "gpt_mcp", "cortex_analyst"):
            assert ((b, p.id) in asked) == (b in p.backends), (b, p.id)
    assert not any(b == "cortex_analyst" and i in data_probes for b, i in asked)


def test_the_writer_redacts_every_log_line_itself():
    from datetime import UTC, datetime

    from agentic_analytics.run.runner import Writer
    from agentic_analytics.tracing import Redactor

    secret = "sk-ant-api03-" + "W" * 40
    wr = Writer(Path("unused"), lambda: datetime(2026, 1, 1, tzinfo=UTC), Redactor.from_environ({}))
    wr.log("error", error=f"boom {secret}", nested={"detail": [f"x {secret}"]})
    item = wr.queue.get_nowait()
    assert item is not None
    _, payload = item
    assert secret not in json.dumps(payload) and "REDACTED" in json.dumps(payload)


LONG_SECRET = "sk-ant-api03-" + "V" * 400  # longer than the 300-character cut


def test_a_long_secret_raised_after_the_asks_is_redacted_before_the_cut(tmp_path, monkeypatch):
    import agentic_analytics.report as report

    def broken(*a, **k):
        raise OSError(f"render failed with {LONG_SECRET}")

    monkeypatch.setattr(report, "render_report", broken)
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)))
    assert out.exit_code == 2 and out.called > 0
    for text in (
        out.message,
        (rd(out) / "run.log").read_text(encoding="utf-8"),
        (rd(out) / "run.json").read_text(encoding="utf-8"),
    ):
        assert "VVVVVVVVVV" not in text


def test_score_events_carry_the_same_cache_key_as_their_ask(tmp_path):
    s = w.setup(tmp_path)
    out = run(s, w.config(backends=("claude_mcp",)), include_probes=False)
    events = [json.loads(x) for x in (rd(out) / "run.log").read_text("utf-8").splitlines()]
    asks = {e["job"]: e["cache_key"] for e in events if e["event"] == "ask"}
    scores = {e["job"]: e["cache_key"] for e in events if e["event"] == "score"}
    assert asks and asks == scores
