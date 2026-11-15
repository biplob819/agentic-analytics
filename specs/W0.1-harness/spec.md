# W0.1 — Repo and harness

**Wave 0. Depends on:** —. **Unblocks:** N1.1.

## Goal
A repo where build nodes can be started, converged and flipped green the way EvalKit's were: the uv project, CI, the six harness scripts copied from `~/EvalKit/scripts/` and re-pointed, the records seeded from the Tech Spec, and the EvalKit dependency pinned behind a contract test.

## Outputs
- uv project `agentic-analytics` (import `agentic_analytics`, CLI `agentic-analytics` and alias `aa`, OWNER D-28), MIT, ruff pre-commit (HAR-01).
- CI on push and pull request: lint, format, types, tests (not `live`), secret scan, graph eligibility, build; Python 3.11 and 3.12 on Linux and macOS; no secrets, no network calls in tests (HAR-02, D-26).
- `scripts/`: `harness_lib`, `verify_graph_eligibility`, `node_start`, `node_converge` (12 gates; gate 8 is `gold`, not EvalKit's `judge`), `status`, `secret_scan`, with `verify_graph_eligibility`, `node_start`, `node_converge` and `secret_scan` each passing `--self-test`; gates 7 to 9 exclude `live` tests; node ids accept the W, N, Q, A, S, M, R, F, B and D waves (HAR-03).
- Records: `docs/graph/graph.md` and `catalog.tsv` generated from Tech Spec §12, `docs/decisions.md` with D-1 to D-31, `docs/lessons.md` carrying EvalKit's applicable lessons, `changelog.md`, `docs/measurements/` (HAR-04).
- `Makefile` targets `load`, `gold`, `smoke`, `run`, `report` that call `agentic-analytics` subcommands; unbuilt subcommands exit 2 and name their node (HAR-05).
- `evalkit-harness` pinned to commit `06591f8` (D-24); `tests/test_evalkit_contract.py` imports every EvalKit symbol this project uses, from a list written independently of EvalKit's `__all__` (HAR-06, EK L-10).
- `.env.example` naming every variable in Tech Spec §4 with empty values; secret scan extended for Langfuse secret keys and any URL carrying a password, placeholders excepted (HAR-07).
- GitHub repo `biplob819/agentic-analytics` (public, OWNER D-22); first push only with the owner's approval.

## Out of scope
Any warehouse, model or MCP code (N1.x onwards). Real README content (B3.2).

## Constraints
No network in tests. Every text read and write names `encoding="utf-8"` (EK L-32).
