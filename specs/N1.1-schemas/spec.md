# N1.1 — Schemas and config

**Wave 1. Depends on:** W0.1. **Unblocks:** N1.2, N1.5, N1.6, Q2.1, S2.1.

## Goal
Every object the pipeline passes around, typed and immutable, with the invariants that make bad states unrepresentable: a clarify-expected question with gold SQL, an answer that is both SQL and a clarification, a "correct" score that contradicts its parts, a Cortex record carrying fixes.

## Outputs (`agentic_analytics.schemas`)
- `Tolerance`, `Question`, `Probe`, `GoldResult` (SCH-01).
- `AgentStep`, `Execution`, `AnalyticsAnswer` (SCH-02).
- `Scores`, `Record` (SCH-03).
- `CategoryAccuracy`, `BackendSummary`, `FixUplift`, `RunSummary` (SCH-04).
- `RunConfig` and `load_run_config(path)` for YAML, with errors that name the file and field (SCH-05).
- `DatasetPack` manifest for `pack.yaml`, and the `pack` and `gold_mode` stamps on `RunSummary` (SCH-07, OWNER D-29).
- Results format v1: `SCHEMA_VERSION = "1.0"`, fixtures under `tests/fixtures/`, round-trip tests (SCH-06).
- Reuses `ResultSet` and `Interval` from `evalkit.schemas` (D-2). Base model mirrors EvalKit's: frozen, unknown fields rejected, finite floats, deep-frozen JSON (EK L-8, L-9).

## Names
Backends `claude_mcp`, `gpt_mcp`, `cortex_analyst`; engines `clickhouse`, `snowflake`; fixes `descriptions`, `examples`, `self_check` (the CLI spells the last one `self-check`).

## Out of scope
Loading questions from JSONL (Q2.1), computing scores (S2.1), statistics (M2.1).
