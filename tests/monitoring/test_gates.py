"""MON-01: the scheduled smoke run is gated by `evalkit gate` (EvalKit B5.3) on per-backend
accuracy floors (`monitoring/gates.yaml`), read from the run's metrics-1 export (MON-05)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from evalkit.gates import EXIT_FAIL, EXIT_INPUT, EXIT_PASS, load_gates, run_gate
from evalkit.metrics import SCHEMA, Metric, MetricsSummary

from tests.golden.record import GOLDEN

GOLDEN_METRICS = GOLDEN / "metrics"

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "monitoring" / "gates.yaml"
BACKENDS = ("claude_mcp", "gpt_mcp", "cortex_analyst")
FIRST_SMOKE = ROOT / "docs" / "measurements" / "smoke-2026-11-17.json"


def smoke_metrics(tmp: Path, **points: float) -> Path:
    """A smoke run's metrics-1 file: one no-fix accuracy per backend (12 questions, 1 trial)."""
    m = MetricsSummary(
        schema_version=SCHEMA,
        project="agentic-analytics",
        run_id="smoke-test",
        created_at="2026-11-17T07:00:00+00:00",
        metrics=[
            Metric(
                name="accuracy", unit="rate", value=p, n=12, labels={"model": b, "fixes": "none"}
            )
            for b, p in points.items()
        ],
    )
    path = tmp / "metrics.json"
    path.write_text(m.model_dump_json(), encoding="utf-8")
    return path


def test_checked_in_gates_floor_every_backend_and_pass_the_first_live_smoke(tmp_path):
    gates = load_gates(GATES).gates
    named = {g.labels["model"] for g in gates if g.labels and g.labels["model"] != "*"}
    assert named == set(BACKENDS)
    assert all(g.metric == "accuracy" and g.op == ">=" and 0 < g.value < 1 for g in gates)
    assert all(g.labels and g.labels["fixes"] == "none" for g in gates)
    # D-78: the first live smoke scored Claude 8/12, GPT 11/12, Cortex 8/12
    first = json.loads(FIRST_SMOKE.read_text("utf-8"))["per_backend"]
    points = {b: first[b]["correct"] / first[b]["of"] for b in BACKENDS}
    assert run_gate(smoke_metrics(tmp_path, **points), GATES).exit_code == EXIT_PASS


def test_a_backend_below_its_floor_fails_the_gate(tmp_path):
    path = smoke_metrics(tmp_path, claude_mcp=5 / 12, gpt_mcp=11 / 12, cortex_analyst=8 / 12)
    report = run_gate(path, GATES)
    assert report.exit_code == EXIT_FAIL
    failed = {r.labels["model"] for r in report.rows if r.result == "fail"}
    assert failed == {"claude_mcp"}


def test_accuracy_equal_to_the_floor_passes(tmp_path):
    path = smoke_metrics(tmp_path, claude_mcp=6 / 12, gpt_mcp=6 / 12, cortex_analyst=6 / 12)
    assert run_gate(path, GATES).exit_code == EXIT_PASS


def test_a_backend_missing_from_the_run_is_an_input_error(tmp_path):
    """A gate matching nothing is never a silent pass (exit 2)."""
    path = smoke_metrics(tmp_path, claude_mcp=0.9, gpt_mcp=0.9)
    assert run_gate(path, GATES).exit_code == EXIT_INPUT


def test_a_backend_added_later_is_gated_too(tmp_path):
    path = smoke_metrics(
        tmp_path, claude_mcp=0.9, gpt_mcp=0.9, cortex_analyst=0.9, new_backend=0.25
    )
    report = run_gate(path, GATES)
    assert report.exit_code == EXIT_FAIL
    assert {r.labels["model"] for r in report.rows if r.result == "fail"} == {"new_backend"}


def test_only_the_no_fix_configuration_is_gated(tmp_path):
    path = smoke_metrics(tmp_path, claude_mcp=0.9, gpt_mcp=0.9, cortex_analyst=0.9)
    data = json.loads(path.read_text("utf-8"))
    data["metrics"].append(
        {"name": "accuracy", "unit": "rate", "value": 0.1, "n": 12,
         "labels": {"model": "claude_mcp", "fixes": "examples"}}
    )  # fmt: skip
    path.write_text(json.dumps(data), encoding="utf-8")
    assert run_gate(path, GATES).exit_code == EXIT_PASS


@pytest.mark.golden
def test_the_golden_run_passes_the_checked_in_gates():
    assert run_gate(GOLDEN_METRICS / "metrics.json", GATES).exit_code == EXIT_PASS


def test_the_gate_cli_the_workflow_runs_exits_by_verdict(tmp_path):
    """`evalkit gate FILE --config monitoring/gates.yaml --out gate.json`, as monitor.yml runs
    it: exit 0 and 1 by verdict, gate.json written either way, and the same verdict and
    gate.json on a rerun."""
    evalkit = shutil.which("evalkit", path=str(Path(sys.executable).parent))
    assert evalkit is not None, "the evalkit console script is installed beside python"
    good = smoke_metrics(tmp_path, claude_mcp=0.7, gpt_mcp=0.9, cortex_analyst=0.7)
    outs = []
    for i in range(2):
        out = tmp_path / f"gate{i}.json"
        cmd = [evalkit, "gate", str(good), "--config", str(GATES), "--out", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == EXIT_PASS, proc.stdout + proc.stderr
        outs.append(json.loads(out.read_text("utf-8")))
    assert outs[0] == outs[1]
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = smoke_metrics(bad_dir, claude_mcp=0.2, gpt_mcp=0.9, cortex_analyst=0.7)
    out = bad_dir / "gate.json"
    cmd = [evalkit, "gate", str(bad), "--config", str(GATES), "--out", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == EXIT_FAIL and out.is_file()
