# Quickstart

agentic-analytics asks plain-language questions of a warehouse through AI agents and scores
every answer against gold SQL, with no LLM judge. This page gets you from nothing to a rendered
dashboard with no keys and no accounts, then shows what changes once you add them.

## 1. Install (Python 3.11 or newer)

```bash
pip install agentic-analytics
agentic-analytics --version
```

The package is published from v0.1.0 onwards; until then, install from a clone with
`git clone https://github.com/biplob819/agentic-analytics` and `uv sync`, and put `uv run` in
front of each command below. The command has a short alias, `aa`.

## 2. Scaffold a folder

```bash
agentic-analytics init
```

`init` writes `run.yaml` (backends, models, fixes, trials, budgets, cost cap) and
`.env.example` (every variable the tool reads) into the current folder. It never overwrites a
file that exists; `--force` does.

## 3. Check the bundled dataset pack

```bash
agentic-analytics pack check
```

This validates the bundled `uk_price_paid` pack with no accounts: the pack manifest, every
question and probe against its schema, the gold SQL lints for both engines, the category quotas
and the semantic model. Point it at your own pack with `--pack PATH`
([writing a dataset pack](writing-a-dataset-pack.md)).

## 4. See a dashboard

```bash
agentic-analytics report --example
```

This writes `results/example-synthetic/index.html` from a run bundled with the package. The run
is **synthetic**: generated data that shows what the dashboard contains (accuracy by category
with intervals, fix uplift, cost per correct answer next to clarification rates, safety probes,
a failure explorer). It is not a measurement of any system, and the page says so at the top.
`--out DIR` writes it somewhere else.

## 5. Add keys and accounts

Copy `.env.example` to `.env` and fill in what you use. `.env` is git-ignored and the tool never
prints key values.

- Models: `ANTHROPIC_API_KEY` for the Claude agent, `OPENAI_API_KEY` for the GPT agent.
- ClickHouse Cloud: the remote MCP key and service id, and a read-only user for re-executing
  SQL.
- Snowflake: a read-only user with a key pair, used for re-executing SQL and by Cortex Analyst.

One engine is enough. With only ClickHouse or only Snowflake configured, the cross-engine gold
check cannot run, and every summary and chart is stamped `single-engine gold`.

```bash
agentic-analytics doctor
```

`doctor` lists which variables are set (names only), reaches each configured engine, and checks
that the roles can only read.

## 6. Estimate, then run

```bash
agentic-analytics run --dry-run
agentic-analytics smoke
agentic-analytics run --backend all --fix none --trials 3 --max-cost 25
agentic-analytics report --run latest
```

`run --dry-run` prints the cost estimate from `run.yaml` and the pack and makes no model or
warehouse calls. It works with no keys and no accounts, and says why a real run would refuse
to start (for example, no data loaded yet).
`smoke` asks 12 questions (two per category) on every configured backend and stops at $2 unless
you pass `--max-cost`. A full run needs a clean gold check for the current data snapshot
([reproduce the UK benchmark](reproduce-the-uk-benchmark.md)), and stops at the cost cap with
its work cached ([controlling cost](cost.md)).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Finished |
| 1 | A parity, gold or probe check failed |
| 2 | Configuration or runtime error, or a command that is not built yet |
| 3 | Stopped at the cost cap; finished work is cached, rerun the same command to resume |

Every command takes `--json` for machine-readable output.
