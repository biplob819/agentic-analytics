# Reproduce the UK benchmark

The bundled `uk_price_paid` pack holds everything the benchmark needs except accounts: load SQL
for both engines, parity fingerprints, the semantic model, the questions with gold SQL, the
safety probes and planted probe rows. This page is the order of operations. Expect to spend
model money on your own keys and trial credits on your Snowflake account; [controlling
cost](cost.md) has the estimates.

> The published numbers came from exactly these steps; their records are in
> [`docs/measurements/`](measurements/) and the published run summary in
> [`docs/results/`](results/summary.json).

## 1. Accounts

- **ClickHouse Cloud** service with the remote MCP server enabled, an API key for the MCP
  endpoint, and an admin user for loading.
- **Snowflake** account (a trial works) in a region where Cortex Analyst runs natively, with an
  admin user for loading and setup.
- **Anthropic** and **OpenAI** API keys for the two agents.
- Optional: **Langfuse** keys for traces. Failure traces are also exported as JSON into each
  run folder, so the dashboard never depends on Langfuse links.

Copy `.env.example` (from `agentic-analytics init`) to `.env` and fill it in. Admin variables
(`*_ADMIN_*`) are used only for loading and role setup.

## 2. Read-only roles

Every query the agents write passes four read-only layers: a SQL guard that allows one `SELECT`
and no table functions, a ClickHouse role that can select only the pack's databases, a Snowflake
role that can only select, and an allow-list on the MCP tools. The role SQL is rendered from
the pack; the setup commands create the users and roles once per account (`setup snowflake`
prints the SQL for review, `--run` runs it). Check the result:

```bash
agentic-analytics setup clickhouse
agentic-analytics setup snowflake --run
agentic-analytics doctor
```

## 3. Load and check parity

```bash
agentic-analytics load
```

`load` downloads HM Land Registry's Price Paid file, loads it into both engines idempotently,
records the snapshot's sha256, and runs the parity fingerprints (row counts, sums, breakdowns)
on both engines. Exit 1 means the engines disagree and nothing downstream will start. The file
is replaced monthly, so the snapshot hash and the as-of date (the last complete month) are
stamped on every result. See the [data card](../DATA_CARD.md).

## 4. Gold check

```bash
agentic-analytics gold
```

Each gold query runs on both engines and must agree at the question's tolerance; ClickHouse
runs twice to prove determinism; top-N questions must have no tie at the cut; results must be
non-empty unless a question expects empty. The check writes `gold/gold_check.json` into the
pack, stamped with the snapshot. A run refuses to start without a clean gold check for the
current snapshot.

## 5. Smoke run

```bash
agentic-analytics smoke
```

Twelve questions on every configured backend, capped at $2. It proves the keys, the MCP
connection and Cortex Analyst work, and records measured token use for the cost estimate.

## 6. The benchmark

```bash
agentic-analytics run --dry-run
agentic-analytics run --backend all --fix none --trials 3
agentic-analytics run --backend claude_mcp,gpt_mcp --fix descriptions --trials 3
agentic-analytics report --run latest
```

Run the baseline first, then each fix alone against it (`descriptions`, `examples`,
`self-check`). Fix content lives in the pack's `fixes/` folder and is frozen and hashed
before the first baseline run (`fixes/FROZEN.json`). Results land in `results/<run id>/`
(`summary.json`, `records.jsonl`, exported traces) and `report` writes the dashboard beside
them.

## 7. Reconcile cost

```bash
agentic-analytics reconcile --run latest --credits credits.json
```

Snowflake's usage views lag by hours, so a fresh summary is stamped `cost_status: estimated`
and the dashboard shows "cost pending". Once the metered credits are known, `reconcile` adds
them (`credits.json` holds them per engine, for example `{"snowflake": 0.42}`).

## 8. Publish (maintainers)

```bash
uv run python scripts/publish_results.py results/<run id>
```

This renders the dashboard into `site/`, draws the write-up figures into `docs/assets/` and
regenerates the README headline from the run's `summary.json`. It refuses unfinished runs and
the synthetic example. The `pages` workflow, run by hand, builds its own `site/` from the
committed record in `docs/results/` (`publish_results.py --from-record`, with no per-answer
records or traces) and deploys it; `site/` is never committed.
