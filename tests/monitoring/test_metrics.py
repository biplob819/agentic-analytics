"""MON-05: summary.json also written in EvalKit's frozen metrics-1 format (`metrics/metrics.json`),
with EvalKit's efficiency block and the per-case sidecar `metrics/cases.jsonl`."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from evalkit.compare import compare_runs
from evalkit.gates import run_gate
from evalkit.metrics import EFFICIENCY_NAMES, MetricsSummary, load_metrics

from agentic_analytics.aggregate import Rates
from agentic_analytics.cost import credit_rates, model_prices
from agentic_analytics.monitoring.metrics import (
    CASES_FILE,
    METRICS_DIR,
    METRICS_FILE,
    SOURCES,
    case_rows,
    metrics_json,
    price_dates,
    to_metrics,
)
from agentic_analytics.questions import load_questions
from agentic_analytics.run.runner import aggregate_rates, load_run
from agentic_analytics.schemas import Question, Record, RunSummary
from tests.golden.record import GOLDEN, golden_run

GOLDEN_METRICS = GOLDEN / "metrics"

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "monitoring" / "gates.yaml"


@pytest.fixture(scope="module")
def golden_dir(tmp_path_factory) -> Path:
    out = golden_run(tmp_path_factory.mktemp("golden"))
    assert out.exit_code == 0 and out.run_dir is not None, out.message
    return out.run_dir


def _inputs(run_dir: Path) -> tuple[dict, list[Record], dict[str, Question], RunSummary, Rates]:
    manifest, records, questions = load_run(run_dir)
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    return manifest, records, questions, summary, aggregate_rates(credit_rates())


def _metrics(run_dir: Path) -> MetricsSummary:
    manifest, records, questions, summary, rates = _inputs(run_dir)
    return to_metrics(
        summary,
        records,
        questions,
        rates,
        n_resamples=manifest["n_resamples"],
        seed=manifest["seed"],
        code_version=manifest["git_sha"],
        price_dates=price_dates(manifest["config"]["models"], model_prices()),
    )


def by_key(m: MetricsSummary) -> dict[tuple[str, tuple[tuple[str, object], ...]], object]:
    return {(x.name, tuple(sorted(x.labels.items()))): x for x in m.metrics}


def one(m: MetricsSummary, name: str, **labels: object):
    hits = [x for x in m.metrics if x.name == name and x.labels == labels]
    assert len(hits) == 1, (name, labels, hits)
    return hits[0]


@pytest.mark.golden
def test_golden_run_writes_metrics_and_cases_byte_for_byte(golden_dir):
    assert (golden_dir / METRICS_DIR / METRICS_FILE).read_bytes() == (
        GOLDEN_METRICS / "metrics.json"
    ).read_bytes()
    assert (golden_dir / METRICS_DIR / CASES_FILE).read_bytes() == (
        GOLDEN_METRICS / "cases.jsonl"
    ).read_bytes()


def test_every_summary_metric_is_in_metrics_with_its_interval_and_unit():
    summary = RunSummary.model_validate_json((GOLDEN / "summary.json").read_text("utf-8"))
    m = load_metrics(GOLDEN_METRICS / "metrics.json")
    assert m.project == "agentic-analytics" and m.run_id == summary.run_id
    for b in summary.backends:
        lab = {"model": b.backend, "fixes": "+".join(b.fixes) or "none"}
        for name in ("accuracy", "clarification_rate", "pass_all_trials"):
            iv = getattr(b, name)
            got = one(m, name, **lab)
            assert (got.unit, got.value, got.ci_low, got.ci_high) == (
                "rate",
                *iv.model_dump().values(),
            )
            assert "bootstrap" in (got.ci_method or "")
        assert one(m, "accuracy", **lab).n == 12
        for c in b.by_category:
            got = one(m, "accuracy_by_category", **lab, category=c.category)
            assert (got.value, got.n) == (c.accuracy.point, c.n)
        assert one(m, "trial_agreement", **lab).value == pytest.approx(b.trial_agreement)
        assert one(m, "latency_p50", **lab).value == b.latency_p50_ms
        assert one(m, "latency_p95", **lab).unit == "ms"
        assert one(m, "answer_from_rows_violations", **lab).value == b.answer_from_rows_violations
        if b.cost_per_correct_usd is not None:
            assert one(m, "cost_per_correct", **lab).value == b.cost_per_correct_usd
        if b.step_efficiency is not None:
            se = one(m, "step_efficiency.excess_mean", **lab)
            assert se.value == b.step_efficiency.excess_mean
            assert se.n == b.step_efficiency.answers  # the answers counted travel as n
    for u in summary.uplifts:
        got = one(m, "uplift", model=u.backend, fix=u.fix)
        assert (got.value, got.ci_low, got.ci_high) == tuple(u.delta_points.model_dump().values())
        assert one(m, "uplift_separated", model=u.backend, fix=u.fix).value is u.separated
    assert one(m, "probes_passed", model="claude_mcp").value == 1.0
    assert one(m, "total_cost_usd").value == summary.total_cost_usd
    assert one(m, "total_credits", engine="snowflake").unit == "CREDITS"
    assert {x.name for x in m.metrics} <= set(SOURCES)


def test_a_gate_on_the_headline_never_matches_a_category_or_rule_row():
    """A gate matches every metric carrying the labels it lists, so per-category, per-rule and
    per-kind figures are named apart from the headline accuracy."""
    m = load_metrics(GOLDEN_METRICS / "metrics.json")
    acc = [x for x in m.metrics if x.name == "accuracy"]
    assert all(set(x.labels) == {"model", "fixes"} for x in acc)
    assert len(acc) == 5  # 3 backends + 2 fix configurations


@pytest.mark.contract
def test_metrics_file_is_metrics_1_and_feeds_gate_and_compare(tmp_path):
    m = load_metrics(GOLDEN_METRICS / "metrics.json")  # EvalKit's own reader: strict, frozen format
    assert m.schema_version == "metrics-1" and m.provenance.evalkit
    report = run_gate(GOLDEN_METRICS / "metrics.json", GATES)
    assert report.exit_code == 0, report.errors
    for side in ("old", "new"):
        (tmp_path / side).mkdir()
        for f in ("metrics.json", "cases.jsonl"):
            (tmp_path / side / f).write_bytes((GOLDEN_METRICS / f).read_bytes())
    c = compare_runs(tmp_path / "old" / "metrics.json", tmp_path / "new" / "metrics.json")
    assert c.exit_code == 0 and not c.errors
    acc = [ch for ch in c.changes if ch.metric == "accuracy"]
    assert acc and all(ch.pairing == "paired" and ch.n_shared == 12 for ch in acc)


@pytest.mark.contract
def test_efficiency_block_uses_evalkit_standard_names_and_units():
    m = load_metrics(GOLDEN_METRICS / "metrics.json")
    eff = [x for x in m.metrics if SOURCES[x.name] == "efficiency"]
    assert {x.name for x in eff} <= set(EFFICIENCY_NAMES)
    assert all(x.labels["mode"] == "live" for x in eff)
    mcp = {x.name for x in eff if x.labels["model"] == "claude_mcp"}
    assert {"tokens_in_per_case", "cache_hit_share", "cost_per_case", "latency_p99"} <= mcp
    cortex = {x.name for x in eff if x.labels["model"] == "cortex_analyst"}
    assert not any(n.startswith("tokens_") for n in cortex)  # Cortex reports no tokens
    assert "cache_hit_share" not in cortex and "cost_per_case" in cortex
    claude = one(m, "cost_per_case", model="claude_mcp", fixes="none", mode="live",
                 price_date="2026-11-15")  # fmt: skip
    assert claude.unit == "USD"
    # system cost is over each configuration's benchmark answers; the total adds the probes
    systems = [x.value for x in eff if x.name == "system_cost"]
    assert 0 < math.fsum(systems) <= one(m, "total_cost_usd").value


def test_sidecar_agrees_with_the_accuracy_it_carries():
    m = load_metrics(GOLDEN_METRICS / "metrics.json")
    rows = [
        json.loads(ln) for ln in (GOLDEN_METRICS / "cases.jsonl").read_text("utf-8").splitlines()
    ]
    assert {r["metric"] for r in rows} == {"accuracy", "accuracy_strict"}
    for x in (x for x in m.metrics if x.name == "accuracy"):
        per_case: dict[str, list[int]] = {}
        for r in rows:
            if r["metric"] == "accuracy" and r["labels"] == x.labels:
                per_case.setdefault(r["case_id"], []).append(r["value"])
        means = [sum(v) / len(v) for v in per_case.values()]
        assert math.fsum(means) / len(means) == pytest.approx(x.value, abs=1e-12)


def test_metrics_are_deterministic(golden_dir):
    first = metrics_json(_metrics(golden_dir))
    assert metrics_json(_metrics(golden_dir)) == first
    assert first == (golden_dir / METRICS_DIR / METRICS_FILE).read_text("utf-8")
    _, records, questions, _, _ = _inputs(golden_dir)
    assert case_rows(list(reversed(records)), questions) == case_rows(records, questions)


@pytest.mark.safety
def test_metrics_carry_no_case_ids_or_question_text():
    """metrics-1 holds aggregates only (EvalKit refuses id-like label keys); the sidecar names
    questions by id, never probes, and no file carries a question's text."""
    text = (GOLDEN_METRICS / "metrics.json").read_text("utf-8")
    m = load_metrics(GOLDEN_METRICS / "metrics.json")
    values = [str(v) for x in m.metrics for v in x.labels.values()]
    assert not [v for v in values if v.startswith(("q-", "p-"))]
    cases = (GOLDEN_METRICS / "cases.jsonl").read_text("utf-8")
    ids = {json.loads(ln)["case_id"] for ln in cases.splitlines()}
    assert ids and all(i.startswith("q-") for i in ids)
    from agentic_analytics.packs import load_pack

    for q in load_questions(load_pack(GOLDEN / "pack")):
        assert q.text not in text and q.text not in cases


def test_reconcile_rewrites_metrics_with_the_reconciled_cost(golden_dir, tmp_path):
    import shutil

    from agentic_analytics.run.runner import reconcile_run

    run = tmp_path / "golden"
    shutil.copytree(golden_dir, run)
    reconcile_run(run, {"snowflake": 3.0, "clickhouse": 0.5}, render=False)
    m = load_metrics(run / METRICS_DIR / METRICS_FILE)
    summary = RunSummary.model_validate_json((run / "summary.json").read_text("utf-8"))
    assert one(m, "total_cost_usd").value == summary.total_cost_usd
    assert one(m, "total_credits", engine="snowflake").value == pytest.approx(
        summary.total_credits["snowflake"]
    )
