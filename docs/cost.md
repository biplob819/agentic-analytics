# Controlling cost

agentic-analytics spends money in two places: **model calls** on your Anthropic and OpenAI keys
(cash), and **warehouse work** on your accounts (Cortex Analyst messages and warehouse time,
which draw Snowflake credits; ClickHouse Cloud compute). It prints an estimate before the first
call and stops before it spends past your cap.

## Estimate first

```bash
agentic-analytics run --dry-run
```

The dry run prints the estimated cost of the run in `run.yaml` and makes no model or warehouse
calls. The estimate uses the dated list prices bundled with the package (`prices.yaml`) and the
Snowflake credit rates in `credits.yaml`, and, after a `smoke` run, the token counts measured
on your own setup. It needs no keys or accounts, and it says why a real run would refuse to
start (for example, no data loaded yet).

Cost grows with questions × trials × backends × fix sets. The bundled benchmark at three trials
with every fix on both agents is the expensive plan; one trial, or fixes on one agent, costs a
fraction of it.

## Cap the spend

```bash
agentic-analytics run --max-cost 10
```

`max_cost_usd` in `run.yaml`, or `--max-cost` on the command line, caps **model spend** for the
run. A run whose estimate is over the cap does not start (exit 3). While it runs, each question
reserves its estimated cost before it is asked; when the next reservation would pass the cap,
no new questions start, the ones in flight finish and are cached, and the run exits 3. The
reservations are estimates, so actual spend can end a little above the cap when answers run
longer than expected; set the cap with some headroom. Cortex messages and warehouse time draw trial credits rather than cash: they are
estimated and reported next to the model spend, not capped (decision D-44).

`smoke` caps itself at $2 unless you pass `--max-cost`.

## Resume for free

Every answer is cached under `.cache/` by its exact inputs. Rerunning the same command replays
finished answers at no cost, so a run stopped by the cap, a crash or Ctrl-C picks up where it
left off. Changing the model, prompt, fix content or question changes the cache key, so only the
affected answers run again.

```bash
agentic-analytics run --rescore
```

`--rescore` rescores a run from the cache with no calls at all, for example after a scorer
change.

## Reconcile warehouse credits

Snowflake's usage views lag by hours, so a fresh summary is stamped `cost_status: estimated` and
the dashboard shows "cost pending". When the metered credits are known:

```bash
agentic-analytics reconcile --run latest --credits credits.json
```

Every live run also appends a line to `docs/measurements/spend-ledger.jsonl`, so spend across
runs adds up in one place.

## Spend less

- Start with `smoke`, then one trial (`--trials 1`) before three.
- Run fixes on one agent first (`--backend claude_mcp --fix descriptions`).
- Use one engine while you iterate; the run is stamped `single-engine gold`.
- Suspend the Snowflake warehouse when you are done; auto-suspend tails are most of the
  warehouse cost of a run.
