# N1.6 — Model adapters and cost

**Wave 1. Depends on:** N1.1. **Unblocks:** A2.1, A2.2.

## Goal
The two agent models behind one provider-neutral conversation interface with native tool calling, every call costed exactly, and the money controls (estimate, dry run, cap, ledger) the runs need.

## Outputs
- `agentic_analytics.models`: `ToolSpec`, `ToolCall`, `ToolResult`, `Usage`, `ModelTurn`, `Conversation`, the `AgentModel` protocol and `make_agent(model, effort, prices)`.
- `AnthropicAgent` (MOD-01): Messages API, native tool use, `output_config.effort` set explicitly, automatic prompt caching, no sampling parameters, no refusal fallbacks (D-31); assistant content sent back unchanged (thinking blocks included); refusals carry their category.
- `OpenAIAgent` (MOD-02): Chat Completions with function tools (the endpoint EvalKit's real runs used), `reasoning_effort`, reasoning tokens reported separately; malformed tool arguments become a `parse_error`, never a crash.
- `prices.yaml` (EvalKit `Price` format) and `credits.yaml` (Cortex message credits, XS warehouse credits, $ per credit, ClickHouse $ per hour), each with source and `verified` (MOD-03).
- `cost.estimate_run(cfg, n)` per backend, honouring trials and fix sets (fixes only on MCP agents); `SpendCap` reserve/settle (MOD-04). The `--dry-run`/`--max-cost` flags are wired by R3.1 on top of these.
- `classify_error`: transport vs config (MOD-05); retries through EvalKit's `with_retries` with SDK retries off.
- `append_ledger` for `docs/measurements/spend-ledger.jsonl` (MOD-06).

## Out of scope
The agent loop and system prompt (A2.1); Cortex calls (A2.2).
