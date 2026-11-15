import asyncio
import json
import math
from datetime import UTC, datetime

import pytest

from agentic_analytics.cost import (
    AgentAssumptions,
    CostCapReached,
    SpendCap,
    append_ledger,
    credit_rates,
    estimate_run,
    model_prices,
)
from agentic_analytics.models.errors import classify_error
from tests.models.fakes import Status
from tests.schemas.factories import run_config


def test_prices_cover_benchmark_models_with_sources():
    p = model_prices()
    assert {"claude-sonnet-5-5", "gpt-6.1-sol"} <= p.keys()
    assert p["claude-sonnet-5-5"].cache_read == 0.10
    assert all(v.source and v.checked_on for v in p.values())


def test_credit_rates_are_sourced_and_flagged():
    r = credit_rates()
    assert r["cortex_analyst_credits_per_message"].value == 0.067
    assert r["cortex_analyst_credits_per_message"].verified
    assert "2026-11-12" in r["cortex_analyst_credits_per_message"].source
    assert not r["snowflake_usd_per_credit"].verified


@pytest.mark.parametrize(
    "text",
    [
        "cortex_analyst_credits_per_message: {value: 0.067, verified: true}\n",
        "cortex_analyst_credits_per_message: {value: -1, verified: true, source: x}\n",
        "cortex_analyst_credits_per_message: {value: .nan, verified: true, source: x}\n",
        "cortex_analyst_credits_per_message: {value: 0.067, verified: true, source: x}\n",
    ],
)
def test_bad_rate_files_are_errors(tmp_path, text):
    f = tmp_path / "credits.yaml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        credit_rates(f)


def test_estimate_scales_with_trials_and_fixsets_on_agents_only():
    base = estimate_run(run_config(trials=1), 100)
    fixed = estimate_run(run_config(trials=3, fixes=["descriptions", "examples"]), 100)
    by = {ln.backend: ln for ln in base.lines}
    fx = {ln.backend: ln for ln in fixed.lines}
    assert fx["claude_mcp"].asks == by["claude_mcp"].asks * 9
    assert fx["cortex_analyst"].asks == by["cortex_analyst"].asks * 3
    assert math.isclose(fx["claude_mcp"].usd, by["claude_mcp"].usd * 9)
    assert by["cortex_analyst"].credits > 100 * 0.067  # messages plus warehouse
    assert "snowflake_usd_per_credit" in base.unverified
    assert base.total_usd == pytest.approx(base.model_usd + base.compute_usd + base.credits * 3.0)


def test_estimate_uses_measured_assumptions():
    cfg = run_config(backends=["claude_mcp"], models={"claude_mcp": "claude-sonnet-5-5"})
    small = estimate_run(cfg, 10, assumptions={"claude_mcp": AgentAssumptions(turns=2)})
    big = estimate_run(cfg, 10, assumptions={"claude_mcp": AgentAssumptions(turns=8)})
    line = {ln.backend: ln for ln in small.lines}, {ln.backend: ln for ln in big.lines}
    assert line[1]["claude_mcp"].usd == pytest.approx(line[0]["claude_mcp"].usd * 4)
    with pytest.raises(ValueError):
        AgentAssumptions(cached_share=1.5)


def test_spend_cap_refuses_past_the_cap():
    async def go():
        cap = SpendCap(1.0)
        r = await cap.reserve(0.6)
        with pytest.raises(CostCapReached):
            await cap.reserve(0.5)
        await cap.settle(r, 0.2)
        await cap.reserve(0.7)
        return cap

    cap = asyncio.run(go())
    assert cap.spent == pytest.approx(0.2) and cap.in_flight == pytest.approx(0.7)


def test_concurrent_reservations_never_exceed_the_cap():
    async def go():
        cap = SpendCap(1.0)
        results = await asyncio.gather(
            *(cap.reserve(0.3) for _ in range(10)), return_exceptions=True
        )
        return cap, results

    cap, results = asyncio.run(go())
    assert sum(1 for r in results if not isinstance(r, Exception)) == 3
    assert cap.in_flight <= 1.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1])
def test_bad_money_is_rejected(bad):
    async def go():
        cap = SpendCap(1.0)
        await cap.reserve(bad)

    with pytest.raises(ValueError):
        asyncio.run(go())
    with pytest.raises(ValueError):
        SpendCap(bad)
    with pytest.raises(ValueError):
        SpendCap(0.0)


def test_classify_error():
    for code in (400, 401, 403, 404, 422):
        assert classify_error(Status(code)) == "config"
    for code in (429, 500, 503):
        assert classify_error(Status(code)) == "transport"
    assert classify_error(ConnectionError()) == "transport"
    assert classify_error(ValueError("bug")) == "config"


def test_ledger_appends_sorted_lines(tmp_path):
    path = tmp_path / "m" / "spend-ledger.jsonl"
    at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    append_ledger(
        path, kind="smoke", run_id="r-1", spent_usd=0.1234567, at=at, credits={"snowflake": 0.5}
    )
    append_ledger(path, kind="baseline", run_id="r-2", spent_usd=1.0, at=at)
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first == {
        "at": "2026-11-15T12:00:00+00:00",
        "credits": {"snowflake": 0.5},
        "kind": "smoke",
        "note": "",
        "run_id": "r-1",
        "spent_usd": 0.123457,
    }
    assert lines[0] == json.dumps(first, sort_keys=True)
    with pytest.raises(ValueError):
        append_ledger(path, kind="x", run_id="r", spent_usd=float("nan"))
