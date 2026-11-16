# Agentic Analytics build graph

Source of truth for build-node dependencies. Parsed by `scripts/harness_lib.py`.
A node may start only when every node in its `Depends on` cell is green (all its rows in `catalog.tsv` are green).
Edge rules: no cycles; a node inherits the constraints of what it depends on; `Verified by` must pass before a node flips green.
Generated from Agentic Analytics Tech Spec §12.2 on 2026-11-15; edit here from now on and keep the Tech Spec in sync.

## Wave 0 — harness

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **W0.1** | Repo and harness | — | node_converge + verifier | Repo, uv project, CI, harness scripts with `--self-test`, records seeded, EvalKit pin and contract test | — |

## Wave 1 — foundations

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **N1.1** | Schemas and config | W0.1 | node_converge + verifier | §3 schemas, `RunConfig` loader, results format v1 fixtures | US1, US8 |
| **N1.2** | Read-only executors | N1.1 | node_converge + verifier | SQL guard, ClickHouse and Snowflake executors, role SQL, `agentic-analytics doctor` | US9 |
| **N1.3** | Loaders, parity and data card | N1.2 | node_converge + verifier | Both loaders, parity fingerprints, probes database, `DATA_CARD.md`, as-of date | US9 |
| **N1.4** | Semantic layer | N1.3 | node_converge + verifier | `packs/uk_price_paid/semantic.yaml`, generated semantic view and descriptions block, parity test | US2 |
| **N1.5** | MCP client | N1.1, N1.6 | node_converge + verifier | Streamable-HTTP client, allow-list, `serviceId` injection, row cap, call counter, fake server | US1 |
| **N1.6** | Model adapters and cost | N1.1 | node_converge + verifier | Claude and GPT tool-calling adapters, `prices.yaml`, estimate, `--dry-run`, `--max-cost`, spend ledger | US5 |
| **N1.7** | Account setup command | N1.2 | node_converge + verifier | `agentic-analytics setup clickhouse` and `setup snowflake` (OWNER D-55): service, MCP key, read-only user and role, Snowflake key pairs and bootstrap SQL, `.env`, the two console steps, the `doctor` MCP role check | US9 |

## Wave 2 — questions and gold

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **Q2.1** | Question bank and probes | N1.1, N1.3 | node_converge + verifier | 120 drafted questions, lints, dual authorship, owner review, 20 probes | US1, US3, US8 |
| **Q2.2** | Gold check | Q2.1, N1.2 | node_converge + verifier | `agentic-analytics gold`, cross-engine match, tie check, the selected 100, `gold_check_id` | US9 |

## Wave 2 — backends and answers

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **A2.1** | MCP agent loop | N1.5, N1.6 | node_converge + verifier | One loop for Claude and GPT, system prompt, `final_answer`, budgets, live smoke | US1 |
| **A2.2** | Cortex Analyst backend | N1.4, N1.6 | node_converge + verifier | REST client (key-pair JWT), parser, execution, suggestions, templated answer | US1, US3 |
| **A2.3** | Answer assembly and tracing | A2.1, A2.2 | node_converge + verifier | Chart builder, `{sql, answer, chart}` for every backend, Langfuse spans, trace export | US4 |

## Wave 2 — scoring

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **S2.1** | Scorers | N1.1, N1.2 | node_converge + verifier | Re-execution, result match, clarification, answer-from-rows, policy | US1, US3 |

## Wave 2 — aggregate

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **M2.1** | Aggregate and statistics | S2.1 | node_converge + verifier | Category CIs, paired uplift with Holm, rates, consistency, cost per correct, `summary.json` | US1, US2, US5 |

## Wave 3 — runs

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **R3.1** | Runner, CLI and smoke run | A2.3, S2.1, M2.1, Q2.2 | node_converge + verifier | `agentic-analytics run`, cache and resume, stamps, gold and parity preconditions, 12-question smoke | US1, US5 |
| **R3.2** | Baseline runs | R3.1 | node_converge + verifier | k trials × 3 backends × 100 questions + probes, reconciled cost | US1, US3, US5 |

## Wave 3 — fixes

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **F3.1** | Fixes as flags | A2.1, N1.4, Q2.2 | node_converge + verifier | Three fixes, frozen hashes, leak check, self-check rules | US2 |
| **F3.2** | Fix runs and uplift | F3.1, R3.2 | node_converge + verifier | Fix runs, uplift table, hand-labelled failure taxonomy | US2, US4 |

## Wave 3 — reporting and release

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B3.1** | Dashboard | M2.1, A2.3 | node_converge + verifier | Static dashboard: category × backend, uplift, cost, clarification, failure explorer | US1–US5 |
| **B3.2** | README, deploy and write-up figures | B3.1, F3.2 | node_converge + verifier | README headline, Cloudflare Pages deploy, decision log, data card, figures for the write-up | US6 |
| **B3.3** | Open-source release v0.1.0 | B3.2 | node_converge + verifier | PyPI `agentic-analytics` by trusted publishing, onboarding (`init`, `report --example`, single-engine mode), docs, community files, CI matrix, tag v0.1.0 | US6, US8 |

## Wave 4 — demo

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **D4.1** | `ask()` and guard | A2.3, R3.1 | node_converge + verifier | `ask()`, replay cache, live path behind EvalKit's guard, warm-up | US7 |
| **D4.2** | Demo page | D4.1, B3.1 | node_converge + verifier | Streamlit page in `portfolio-demos`, measured live p95 | US7 |
