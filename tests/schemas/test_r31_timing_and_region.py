"""The fields R3.1 added to N1.1's schemas: `Record.ask_wall_ms` and `Record.analyst_latency_ms`
(Cortex timed question in to answer out, Cortex's own time beside it) and
`RunConfig.snowflake_region` (OWNER D-62 caveat). Each invariant has a test whose input breaks
only that invariant (L-2); unset fields are left out of the dump so older files keep their format.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_analytics.run.runner import config_hash
from agentic_analytics.schemas import Record, RunConfig
from tests.schemas import factories as f

pytestmark = pytest.mark.contract


def cortex_record(**kw):
    return f.record(
        backend="cortex_analyst",
        answer=f.answer(
            backend="cortex_analyst", model_snapshot=None, steps=(), sql=f.SQL["snowflake"]
        ),
        execution=f.execution(engine="snowflake", sql=f.SQL["snowflake"]),
        cost_usd=0.0,
        tokens={},
        **kw,
    )


def test_timings_round_trip_when_set():
    r = cortex_record(ask_wall_ms=2400, analyst_latency_ms=812.5)
    data = json.loads(r.model_dump_json())
    assert data["ask_wall_ms"] == 2400 and data["analyst_latency_ms"] == 812.5
    assert Record.model_validate_json(r.model_dump_json()) == r


def test_unset_timings_are_left_out_of_the_dump():
    r = f.record()
    assert r.ask_wall_ms is None and r.analyst_latency_ms is None
    assert "ask_wall_ms" not in r.model_dump() and "analyst_latency_ms" not in r.model_dump()
    assert "ask_wall_ms" not in r.model_dump_json()
    only_wall = json.loads(f.record(ask_wall_ms=0).model_dump_json())
    assert only_wall["ask_wall_ms"] == 0 and "analyst_latency_ms" not in only_wall


def test_analyst_latency_is_for_cortex_records_only():
    with pytest.raises(ValidationError, match="only Cortex records have it"):
        f.record(analyst_latency_ms=10.0)  # an MCP record, valid in every other way
    assert cortex_record(analyst_latency_ms=10.0).analyst_latency_ms == 10.0


@pytest.mark.parametrize("bad", [-1, 1.5, True, "12"])
def test_ask_wall_ms_is_a_non_negative_int(bad):
    with pytest.raises(ValidationError):
        f.record(ask_wall_ms=bad)


@pytest.mark.parametrize("bad", [-0.5, float("inf"), float("nan")])
def test_analyst_latency_is_finite_and_non_negative(bad):
    with pytest.raises(ValidationError):
        cortex_record(analyst_latency_ms=bad)


@pytest.mark.parametrize(
    "region", ["AWS_AP_SOUTHEAST_7", "aws_us_west_2", "PUBLIC.AWS_US_WEST_2", "AZURE_EASTUS2"]
)
def test_snowflake_region_accepts_current_region_forms(region):
    assert f.run_config(snowflake_region=region).snowflake_region == region


@pytest.mark.parametrize(
    "bad",
    ["", "AWS AP SOUTHEAST 7", "aws-us-west-2", "a.b.AWS_US_WEST_2", "X;DROP", "<b>", "A", "7AWS"],
)
def test_snowflake_region_refuses_anything_else(bad):
    with pytest.raises(ValidationError):
        f.run_config(snowflake_region=bad)


def test_an_unset_region_leaves_the_config_dump_and_hash_as_before():
    cfg = f.run_config()
    assert "snowflake_region" not in cfg.model_dump(mode="json")  # the golden config_hash holds
    assert RunConfig.model_validate(cfg.model_dump(mode="json")) == cfg
    with_region = f.run_config(snowflake_region="AWS_US_WEST_2")
    assert with_region.model_dump(mode="json")["snowflake_region"] == "AWS_US_WEST_2"
    assert config_hash(with_region) != config_hash(cfg)
