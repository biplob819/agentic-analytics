# Writing a dataset pack

A dataset pack holds everything that is specific to one dataset, so the engine never names a
table. The bundled `uk_price_paid` pack (in `src/agentic_analytics/packs/uk_price_paid/`) is
the reference; copy it and change what differs. Point any command at your pack with
`--pack PATH` (a directory), or by name for a pack bundled in the wheel.

## Layout

```
my_pack/
  pack.yaml              # the manifest (required)
  load/clickhouse.sql    # idempotent load, templated with {database}
  load/snowflake.sql
  parity.yaml            # fingerprint queries both engines must agree on
  semantic.yaml          # the single semantic source: Cortex view and the descriptions fix
  questions.jsonl        # one question per line, with gold SQL for each engine
  probes.jsonl           # safety probes: write attempts, prompt injection, identity
  probes/clickhouse.sql  # planted rows the injection probes read
  probes/snowflake.sql
  gold/gold_check.json   # written by `agentic-analytics gold`, never by hand
```

A pack for one engine lists only that engine; its runs are stamped `single-engine gold`.

## pack.yaml

```yaml
schema_version: "1.0"
name: my_pack                 # lowercase letters, digits and underscores
version: "1.0"                # bump it when questions, gold or load SQL change
title: My dataset
licence: CC-BY-4.0
attribution: >-
  The credit line the data's licence requires, shown on every dashboard and figure.
source_url: https://example.org/my-dataset
engines: [clickhouse, snowflake]
databases:
  clickhouse: my_db
  snowflake: ANALYTICS.MY_DB
tables: [my_table]
as_of_rule: last_complete_month   # or `fixed`, with an `as_of: YYYY-MM-DD` line
quotas:                           # how many questions per category a run selects
  aggregation: 17
  time: 17
  geography: 17
  top_n: 17
  yoy: 16
  ambiguous: 16
```

The manifest is validated by the `DatasetPack` schema: every engine needs a database, quotas
may name only the six categories, and `source_url` must be an http(s) URL with a real host.
Only publish a pack whose data licence allows redistribution and evaluation use, and put its
attribution in `attribution` word for word.

## Load SQL and parity

`load/<engine>.sql` creates and fills the pack's tables; `{database}` is replaced by the
manifest's database for that engine. Make it idempotent: `agentic-analytics load` may run it
again. `parity.yaml` lists fingerprint queries (row counts, sums, breakdowns) that run on both
engines after loading and must return identical rows; return dates and enums as text so no
driver type decides a match. Every fingerprint must pass the read-only SQL guard.

## Semantic model

`semantic.yaml` describes the table: what each column means, synonyms, sample values and
measure definitions. It generates both the Snowflake semantic view that Cortex Analyst reads
and the `descriptions` fix the MCP agents get, so both see the same information. The comments
at the top of the bundled pack's file list the rules the model checker enforces.

## Questions and probes

See [adding questions](adding-questions.md) for the question format and the gold SQL rules.
Probes test safety rather than accuracy: write attempts the backend must refuse, prompt
injections planted in rows (with an `injection_marker` that must never appear in an answer),
and identity questions. Copy the bundled `probes.jsonl` and adapt the planted rows.

## Check it

```bash
agentic-analytics pack check --pack my_pack
```

`pack check` needs no accounts. It validates the manifest, every question and probe, lints the
gold SQL for both engines (parse, read-only guard, exact functions only, known tables and
columns, top-N ordering and limits, and more), checks the quotas can be met, and reads the
semantic model. Fix every issue it lists before loading. Then follow [reproduce the UK
benchmark](reproduce-the-uk-benchmark.md) from step 3 with `--pack my_pack`.
