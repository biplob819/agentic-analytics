# F3.2 — Fix runs and uplift

**Wave 3. Depends on:** F3.1, R3.2. **Unblocks:** B3.2. Budget: OWNER D-80 (`--max-cost` up to $35).

## Goal
Each frozen fix (F3.1: `descriptions`, `examples`, `self_check`; `fix_hash 7ae19078…`) run alone on the two MCP agents (D-14) at k = 3 trials, against the same baseline answers, so each fix's effect is measured on its own (US2); the uplift table with a paired bootstrap and Holm separation (§6.2); and a hand-labelled failure taxonomy of 30 baseline failures (US4).

## Outputs
- `agentic-analytics run --fix all --max-cost 35` (`run-20261117T085653Z-85f3b0`, 2,160 new MCP answers, $15.49) rescored with no calls under the committed scoring (OWNER D-81, D-89) as `rescore-20261117T110131Z-6ccebe`.
- `docs/measurements/fixruns-2026-11-17.json`: per configuration accuracy (headline and strict) with intervals, the uplift table (point, 95% interval, p, Holm-separated), probe outcomes per configuration, and spend.
- `docs/measurements/failure-taxonomy-2026-11-17.json`: 30 baseline failures, one primary label each from the Tech Spec's list, with evidence (FXR-03).
- `tests/run/test_fixrun_record.py`.

## Rules
- Cortex gets the baseline only (fixes apply to the MCP agents).
- Uplift is paired per question against the same backend's baseline (§6.2); a fix is "above noise" only when Holm-separated.
- The probe split (D-51's strict marker rule) is reported per configuration; quoted refusals are not separated automatically.
