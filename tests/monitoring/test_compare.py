"""MON-02 (the part EvalKit v0.3.0 ships): two runs are compared with `evalkit compare` (EvalKit
B5.6) on their metrics-1 exports, paired on shared questions through the per-case sidecar.
Run history, the trend page and drift flags wait for EvalKit's ledger (B5.7, v0.4.0; MON-07 in
V5.1, OWNER D-115)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from evalkit.compare import EXIT_PASS, EXIT_REGRESSION, compare_runs, report_json

from agentic_analytics.cost import credit_rates, model_prices
from agentic_analytics.run.runner import (
    aggregate_rates,
    load_run,
    summarise,
    write_run_metrics,
)
from agentic_analytics.schemas import Record
from tests.golden.record import GOLDEN, golden_run

GOLDEN_METRICS = GOLDEN / "metrics"

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "monitoring" / "gates.yaml"


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> tuple[Path, Path]:
    """The golden run, and a copy in which Claude's no-fix answers are all wrong."""
    out = golden_run(tmp_path_factory.mktemp("golden"))
    assert out.exit_code == 0 and out.run_dir is not None, out.message
    old = out.run_dir
    new = tmp_path_factory.mktemp("worse") / "golden"
    shutil.copytree(old, new)
    manifest, records, questions = load_run(new)

    def worse(r: Record) -> Record:
        if r.backend != "claude_mcp" or r.fixes or r.question_id.startswith("p-"):
            return r
        sc = r.scores.model_copy(update={"correct": False, "correct_strict": False})
        return r.model_copy(update={"scores": sc})

    records = [worse(r) for r in records]
    rates = aggregate_rates(credit_rates())
    summary = summarise(records, questions, manifest, rates)
    write_run_metrics(new, summary, records, questions, manifest, rates, model_prices())
    return old, new


def _change(c, metric: str, **labels: str):
    hits = [ch for ch in c.changes if ch.metric == metric and ch.labels == labels]
    assert len(hits) == 1, hits
    return hits[0]


def test_compare_flags_a_regression_between_two_runs(runs):
    old, new = runs
    c = compare_runs(old / "metrics", new / "metrics", gates=GATES)
    assert c.exit_code == EXIT_REGRESSION, c.errors
    ch = _change(c, "accuracy", model="claude_mcp", fixes="none")
    assert ch.diff is not None and ch.diff < 0 and ch.flagged and ch.regression
    assert _change(c, "accuracy", model="gpt_mcp", fixes="none").diff == 0


@pytest.mark.contract
def test_compare_pairs_runs_on_shared_questions(runs):
    old, new = runs
    c = compare_runs(old / "metrics", new / "metrics")
    for labels in ({"model": "claude_mcp", "fixes": "none"}, {"model": "gpt_mcp", "fixes": "none"}):
        ch = _change(c, "accuracy", **labels)
        assert ch.pairing == "paired" and ch.n_shared == 12 and ch.interval_status is not None


@pytest.mark.golden
def test_the_golden_run_compared_with_itself_changes_nothing(tmp_path):
    for side in ("old", "new"):
        (tmp_path / side).mkdir()
        for f in ("metrics.json", "cases.jsonl"):
            shutil.copyfile(GOLDEN_METRICS / f, tmp_path / side / f)
    c = compare_runs(tmp_path / "old" / "metrics.json", tmp_path / "new" / "metrics.json")
    assert c.exit_code == EXIT_PASS and not c.errors and not c.missing
    assert all(not ch.flagged and ch.diff in (0, None, False) for ch in c.changes)


def test_compare_is_byte_identical_on_rerun(runs):
    old, new = runs
    a = report_json(compare_runs(old / "metrics", new / "metrics"))
    b = report_json(compare_runs(old / "metrics", new / "metrics"))
    assert a == b
    assert json.loads(a)["schema_version"]
