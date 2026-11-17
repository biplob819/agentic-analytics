# R3.2 — Baseline runs

**Wave 3. Depends on:** R3.1. **Unblocks:** F3.2, M4.1. Budget approved by the owner (OWNER D-80).

## Goal
The benchmark's baseline: every backend answers the 100 gold-selected questions and the 20 probes (for the backends each probe names), three trials each (D-12), with the frozen fixes OFF, as-of 2026-08-31 (D-50). This is the reference every fix uplift (F3.2) is measured against.

## Outputs
- `results/<run_id>/` from `agentic-analytics run` with `run.yaml` as committed (backends claude_mcp, gpt_mcp, cortex_analyst; fixes []; trials 3; effort medium; `max_cost_usd` 25): `records.jsonl`, `run.log`, `run.json`, `summary.json`, the dashboard, sampled traces.
- `docs/measurements/baseline-<date>.json`: the run id, per-backend accuracy with intervals, clarification and false-clarification rates, probe outcomes, latency, cost (model dollars and Snowflake trial credits), wall time per trial set, and the hand check of 10 failures (BAS-04).
- A spend-ledger line (the runner writes it; Cortex credits included, D-78).
- `agentic-analytics reconcile --credits` once Snowflake's ACCOUNT_USAGE has caught up (BAS-03), flipping `cost_status` to `reconciled`.

## Rules
- The fixes stay frozen (`fix_hash 7ae19078…`); the runner refuses otherwise.
- The run stops at the $25 cap with exit 3 and resumes for free; a transport failure is retried on resume, never cached.
- Results are not published until F3.2 and B3.2; this node records them.
- BAS-02's 45-minute line is per trial set (one trial across all backends), from `run.log` timestamps.
