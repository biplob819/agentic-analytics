from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_analytics.schemas import (
    CategoryAccuracy,
    ConfigError,
    FixUplift,
    Interval,
    load_run_config,
)
from tests.schemas.factories import IV, T0, backend_summary, run_config, run_summary


def test_summary_invariants():
    with pytest.raises(ValidationError):
        backend_summary(
            by_category=[
                CategoryAccuracy(category="time", n=17, accuracy=IV),
                CategoryAccuracy(category="time", n=17, accuracy=IV),
            ]
        )
    with pytest.raises(ValidationError):
        backend_summary(latency_p50_ms=30000)
    with pytest.raises(ValidationError):
        backend_summary(trial_agreement=1.5)
    with pytest.raises(ValidationError):
        run_summary(backends=[backend_summary(), backend_summary()])
    with pytest.raises(ValidationError):
        run_summary(cost_status="final")
    with pytest.raises(ValidationError):
        run_summary(finished_at=T0.replace(year=2025))


def test_uplifts_unique_and_cortex_has_no_fixes():
    up = FixUplift(
        backend="claude_mcp", fix="examples", delta_points=IV, p_value=0.2, separated=False
    )
    with pytest.raises(ValidationError):
        run_summary(uplifts=[up, up])
    with pytest.raises(ValidationError):
        FixUplift(
            backend="cortex_analyst", fix="examples", delta_points=IV, p_value=0.2, separated=False
        )
    with pytest.raises(ValidationError):
        backend_summary(backend="cortex_analyst", fixes=["descriptions"])


def test_uplift_touching_zero_is_not_separated():
    with pytest.raises(ValidationError):
        FixUplift(
            backend="claude_mcp",
            fix="examples",
            delta_points=Interval(point=1.0, lo=0.0, hi=2.0),
            p_value=0.04,
            separated=True,
        )


def test_separated_uplift_must_exclude_zero():
    with pytest.raises(ValidationError):
        FixUplift(
            backend="claude_mcp",
            fix="examples",
            delta_points=Interval(point=2.0, lo=-1.0, hi=5.0),
            p_value=0.2,
            separated=True,
        )
    with pytest.raises(ValidationError):
        FixUplift(
            backend="claude_mcp",
            fix="examples",
            delta_points=IV,
            p_value=1.5,
            separated=False,
        )


def test_run_config_defaults_match_tech_spec():
    c = run_config()
    assert (c.trials, c.max_tool_calls, c.max_turns) == (3, 6, 10)
    assert (c.row_cap_model, c.row_cap_exec) == (50, 10_000)
    assert c.max_cost_usd is None
    assert c.effort == "medium"


@pytest.mark.parametrize(
    "kw",
    [
        {"backends": []},
        {"backends": ["claude_mcp", "claude_mcp"]},
        {"models": {"claude_mcp": "claude-sonnet-5-5"}},  # gpt_mcp model missing
        {"models": {"claude_mcp": "a", "gpt_mcp": "b", "cortex_analyst": "c"}},
        {"concurrency": {"duck": 2}},
        {"trials": 0},
        {"max_cost_usd": 0},
        {"fixes": ["examples", "examples"]},
        {"effort": "max"},
    ],
)
def test_run_config_invariants(kw):
    with pytest.raises(ValidationError):
        run_config(**kw)


def test_cortex_only_config_needs_no_models():
    assert run_config(backends=["cortex_analyst"], models={}).models == {}


def _write(tmp_path: Path, text: str | bytes) -> Path:
    p = tmp_path / "run.yaml"
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    return p


GOOD = (
    "backends: [claude_mcp]\n"
    "models: {claude_mcp: claude-sonnet-5-5}\n"
    "as_of: 2026-09-30\n"
    "max_cost_usd: 20\n"
)


def test_load_run_config_reads_yaml(tmp_path):
    c = load_run_config(_write(tmp_path, GOOD))
    assert c.backends == ("claude_mcp",) and c.max_cost_usd == 20


def test_load_run_config_names_file_and_field(tmp_path):
    p = _write(tmp_path, GOOD.replace("max_cost_usd: 20", "max_cost_usd: -1"))
    with pytest.raises(ConfigError, match=r"run\.yaml: max_cost_usd"):
        load_run_config(p)


def test_yaml_merge_keys_work_and_bools_are_not_numbers(tmp_path):
    merged = "base: &b {trials: 2}\n" + GOOD
    with pytest.raises(ConfigError, match="base"):
        load_run_config(_write(tmp_path, merged))  # unknown key, not a constructor error
    ok = "<<: {trials: 2}\n" + GOOD
    assert load_run_config(_write(tmp_path, ok)).trials == 2
    for bad in ("max_cost_usd: yes", "max_cost_usd: true"):
        with pytest.raises(ConfigError, match="max_cost_usd"):
            load_run_config(_write(tmp_path, GOOD.replace("max_cost_usd: 20", bad)))
    with pytest.raises(ConfigError, match="seed"):
        load_run_config(_write(tmp_path, GOOD + "seed: yes\n"))


@pytest.mark.parametrize(
    "content, message",
    [
        (GOOD + "trials: 2\ntrials: 3\n", "duplicate key"),
        (b"backends: [claude_mcp]\nas_of: 2026-09-30\nx: \xff\n", "not UTF-8"),
        ("- just\n- a list\n", "top level must be a mapping"),
        ("backends: [unclosed\n", "invalid YAML"),
    ],
)
def test_load_run_config_rejects_bad_files(tmp_path, content, message):
    with pytest.raises(ConfigError, match=message):
        load_run_config(_write(tmp_path, content))


def test_load_run_config_on_directory_and_missing(tmp_path):
    with pytest.raises(ConfigError, match="directory"):
        load_run_config(tmp_path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_run_config(tmp_path / "nope.yaml")
