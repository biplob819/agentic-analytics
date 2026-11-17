# M4.1 — Monitoring and EvalKit reuse

**Wave 4. Depends on:** R3.2, B3.1. **Unblocks:** B3.2. Added by OWNER D-74; scope OWNER D-75; EvalKit's answer D-76. Built on EvalKit 0.3.0 from PyPI (D-97). Spend: $0 (no live call; the scheduled run is off until the release, OWNER D-103).

## Goal
The benchmark is monitored after the baseline and reuses EvalKit wherever EvalKit owns the general piece: a hand-run (later weekly) smoke run gated by `evalkit gate`, run pairs compared with `evalkit compare`, the summary exported in EvalKit's metrics-1 format, probe verdicts cross-checked by EvalKit's `attack_outcome`, step efficiency per backend, and the answer-from-rows and clarification scorers run from EvalKit with parity on every recorded answer. Every metric answers a reader's question (§6.3, OWNER D-75).

## Outputs
- `.github/workflows/monitor.yml` (MON-01): `workflow_dispatch` only; the weekly `schedule` is written but commented out until the release (OWNER D-103); `agentic-analytics smoke --max-cost 2` from the `monitoring` environment's secrets, then `evalkit gate` on `results/<run>/metrics/metrics.json` with `monitoring/gates.yaml`; the spend line the run appended in the job summary (never committed back); the run's `metrics/`, `gate.json` and spend line uploaded as the artifact `monitor-<run id>`.
- `src/agentic_analytics/monitoring/metrics.py` (MON-05): `metrics/metrics.json` (metrics-1, with EvalKit D-67's efficiency block) and `metrics/cases.jsonl` (per-case sidecar) beside every `summary.json` (run and reconcile); golden copies under `tests/golden/metrics/`.
- `evalkit compare results/<old>/metrics results/<new>/metrics` (MON-02, the compare part; OWNER D-98 moves history, trend and drift to B3.3 as MON-07).
- `src/agentic_analytics/monitoring/probes.py` (MON-03): the probe answer as an EvalKit trajectory, `attack_outcome`'s verdict beside S2.1's (S2.1's verdict reported, `attack_outcome` the cross-check, D-99).
- `src/agentic_analytics/monitoring/efficiency.py` (MON-04): `step_efficiency` per MCP configuration in `summary.json`.
- `scoring/numbers.py` runs EvalKit's `unsupported_numbers` (MON-06); `src/agentic_analytics/monitoring/parity.py` and `docs/measurements/evalkit-parity-2026-11-17.json`: EvalKit's answer-from-rows, clarification rates and attack_outcome against every stored record of the first smoke, the baseline rescore and the fix-run rescore (5,046 records), all identical; the clarification verdict and rates stay this project's, EvalKit's rates a cross-check (OWNER D-100).
- EvalKit pinned to `evalkit-harness==0.3.0` from PyPI (tag v0.3.0, `4ddfef5`), `uv.lock` and `demo/requirements.txt` regenerated (D-95, D-97).

## Rules
- No live call, no `.env`, no schedule enabled. The workflow's secrets are the smoke run's model keys and read-only warehouse credentials only, in the `monitoring` environment, never on pull requests or forks.
- Parity before removal (OWNER D-75): a local scorer is removed only when EvalKit's version scores every recorded answer identically.
- The published numbers do not move (D-92's principle): `summary.json` keeps its bytes for every existing field; new outputs are added beside it.
