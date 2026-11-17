"""Regenerate the golden run's inputs and its expected `summary.json` (Tech Spec §11, R3.1).

    uv run python -m tests.golden.record

Writes, under `tests/golden/`:

- `pack/`: 12 questions (2 per category, frozen from the bundled bank; one ordered top-N), 2
  probes, gold results for both engines, a clean gold check, and the bundled fix files frozen
  afresh (`fixes/FROZEN.json`; every run needs a freeze, D-13/D-52);
- `measurements/`: the snapshot and parity records the run's preconditions read (RUN-04);
- `recording.json`: the recorded model turns, Cortex bodies and warehouse answers;
- `run.yaml`: the run configuration;
- `summary.json`: what `run_benchmark` produced from all of that;
- `scores.json`: each record's verdict and mismatch reason (`scores_digest`).

The responses are recorded from the fakes for now (`tests/run/world.py`). When A2.1 and A2.2
record the live model, MCP and Cortex responses, they replace `recording.json` (and the pack's
gold with live gold), and this script's last step regenerates `summary.json` from them. Only
regenerate after a deliberate change; `pytest -m golden` fails on any drift.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from agentic_analytics.packs import load_pack
from agentic_analytics.run.runner import RunOptions, RunOutcome, run_benchmark
from agentic_analytics.schemas import load_run_config
from agentic_analytics.tracing import Redactor
from tests.run import world as w

GOLDEN = Path(__file__).resolve().parent
RUN_ID = "golden"
N_RESAMPLES = 2_000
RUN_YAML = """\
# The golden run (Tech Spec §11): 12 questions, 3 backends, 3 trials, the examples fix on both
# agents, recorded responses. One worker per backend, so per-trial recordings replay in order.
backends: [claude_mcp, gpt_mcp, cortex_analyst]
models:
  claude_mcp: claude-sonnet-5-5
  gpt_mcp: gpt-6.1-sol
fixes: [examples]
trials: 3
as_of: 2026-09-30
seed: 7
concurrency:
  claude_mcp: 1
  gpt_mcp: 1
  cortex_analyst: 1
"""
# Two per category; q-071 is the bank's ordered top-N question, q-061 an unordered one.
IDS = ("q-001", "q-002", "q-021", "q-022", "q-041", "q-042", "q-061", "q-071", "q-081",
       "q-082", "q-101", "q-102")  # fmt: skip
# Per-trial outcomes that pin the scorer's edges, so these mutants change summary.json:
# - tolerance: q-002 (median, rel 0.005) answered 0.4% and 0.6% off (REL_TOL 0.005 -> 0.05);
# - order: q-071 (ordered) and q-061 (unordered) answered with permuted rows (ignoring `ordered`
#   either way);
# - row count: q-061 with one extra row, q-071 with a missing row (relaxing the row check).
VARIANTS: dict[tuple[str, str], list[str]] = {
    ("claude_mcp", "q-002"): ["tol_in", "tol_in", "tol_out"],
    ("gpt_mcp", "q-002"): ["tol_out", "tol_out", "tol_in"],
    ("cortex_analyst", "q-002"): ["tol_in"],
    ("claude_mcp", "q-071"): ["permuted", "correct", "permuted"],
    ("cortex_analyst", "q-071"): ["permuted"],
    ("gpt_mcp", "q-061"): ["permuted", "permuted", "correct"],
    ("claude_mcp", "q-061"): ["extra_row", "correct", "extra_row"],
    ("gpt_mcp", "q-071"): ["wrong", "correct", "correct"],
    ("cortex_analyst", "q-061"): ["extra_row"],
}
# Under the examples fix: some answers improve, one regresses (uplift and Holm have work).
FIX_VARIANTS: dict[tuple[str, str, str], list[str]] = {
    ("claude_mcp", "examples", "q-082"): ["correct", "correct", "correct"],
    ("claude_mcp", "examples", "q-002"): ["tol_in", "tol_in", "tol_in"],
    ("claude_mcp", "examples", "q-061"): ["correct", "correct", "extra_row"],
    ("claude_mcp", "examples", "q-021"): ["wrong", "correct", "wrong"],
    ("gpt_mcp", "examples", "q-022"): ["correct", "correct", "correct"],
    ("gpt_mcp", "examples", "q-071"): ["correct", "correct", "permuted"],
}


def scores_digest(run_dir: Path) -> str:
    """Every record's verdict and mismatch reason, sorted: pins scorer behaviour the summary's
    aggregates can hide (two different reasons for the same wrong answer)."""
    rows = []
    for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        sc = r["scores"]
        rows.append(
            [
                r["question_id"],
                r["backend"],
                "+".join(r["fixes"]),
                r["trial"],
                sc["correct"],
                sc["result_match"],
                sc["mismatch_reason"],
            ]
        )
    rows.sort(key=lambda x: [str(v) for v in x])
    return "".join(json.dumps(r) + "\n" for r in rows)


def golden_run(tmp: Path, *, services=None) -> RunOutcome:
    """Run the golden configuration on the recorded responses into `tmp`."""
    cfg = load_run_config(GOLDEN / "run.yaml")
    rec = json.loads((GOLDEN / "recording.json").read_text(encoding="utf-8"))
    svc = services if services is not None else (lambda: w.services_from_recording(rec, cfg))
    options = RunOptions(
        results_dir=tmp / "results",
        cache_dir=tmp / ".cache",
        measurements_dir=GOLDEN / "measurements",
        ledger_path=None,
        run_id=RUN_ID,
        now=w.fixed_now,
        stopwatch=w.fixed_stopwatch(),
        git_sha="0000000",
        n_resamples=N_RESAMPLES,
        render=False,
        redactor=Redactor({}),
    )
    return run_benchmark(cfg, load_pack(GOLDEN / "pack"), services=svc, options=options)


def main() -> int:
    world = w.World.from_bundled(IDS, variants=VARIANTS, fix_variants=FIX_VARIANTS, edges=True)
    for sub in ("pack", "measurements"):
        shutil.rmtree(GOLDEN / sub, ignore_errors=True)
    w.write_pack(GOLDEN / "pack", world)
    w.copy_bundled_fixes(GOLDEN / "pack", reason="golden run fixture freeze")
    w.write_measurements(GOLDEN / "measurements")
    text = json.dumps(world.recording(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    (GOLDEN / "recording.json").write_text(text, encoding="utf-8")
    (GOLDEN / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        out = golden_run(Path(tmp))
        if out.exit_code != 0 or out.run_dir is None:
            print(out.message, file=sys.stderr)
            return 1
        shutil.copyfile(out.run_dir / "summary.json", GOLDEN / "summary.json")
        (GOLDEN / "scores.json").write_text(scores_digest(out.run_dir), encoding="utf-8")
    print(f"wrote {GOLDEN}/summary.json ({out.jobs} asks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
