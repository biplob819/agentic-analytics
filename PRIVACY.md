# Privacy

**agentic-analytics has no telemetry.** It does not phone home, collect usage statistics or send
crash reports.

## Where your data goes

It makes network requests only to the services you configure in `.env`, and only for these
purposes:

| Purpose | Destination | When |
|---|---|---|
| Agent model calls (questions, schema context, query results the agent reads) | Anthropic and OpenAI, with your keys | `smoke`, `run` |
| Agent queries through MCP | Your ClickHouse Cloud service's remote MCP endpoint | `smoke`, `run` |
| Cortex Analyst questions | Your Snowflake account's Cortex Analyst REST API | `smoke`, `run` |
| Re-executing SQL for scoring, loading, parity, gold, role checks | Your ClickHouse service and Snowflake account | `doctor`, `load`, `gold`, `smoke`, `run` |
| Dataset download | The source URL in the pack's manifest (HM Land Registry for the bundled pack) | `load` |
| Account setup: creating or adopting the service and the MCP key, then the read-only user, role and key-pair users | The ClickHouse Cloud API (through `clickhousectl`, with your admin Cloud API key), your ClickHouse service and Snowflake account | `setup clickhouse`, `setup snowflake --run` |
| Traces, only if you set Langfuse keys | Your Langfuse project | `smoke`, `run` |
| Demo live mode on a visitor's own key (OWNER D-56) | Anthropic or OpenAI, with the visitor's own key, kept only in that browser session's memory and never stored | the demo page |
| Demo spend counters (daily budgets and the rate limit; amounts and counts only, never a question or a key) | The Postgres database at `EVALKIT_DEMO_DATABASE_URL`, through psycopg; without it, a local SQLite file under `.cache/` | the demo page in live mode |
| Serving the demo page | Streamlit serves the page to its visitors; Streamlit's own usage statistics are off (`gatherUsageStats = false` in `.streamlit/config.toml`) | the demo page |
| Demo latency measurement | The deployed demo page's https URL, opened in a headless browser by Playwright (installed on the measuring machine, not a dependency) | the p95 tool (`python -m agentic_analytics.demo.p95 --mode page`) |

Questions, schema descriptions and query results are sent to the model providers you configure,
under those providers' own terms. Nothing is sent anywhere else. `init`, `pack check`,
`report`, `run --dry-run` and `setup snowflake` without `--run` make no network requests.

## What is stored on your machine

- `.cache/`: the answer cache, so reruns and `--rescore` are free, and the demo's local spend
  counters (`demo-spend-*.sqlite`) when no Postgres URL is set.
- `results/<run id>/`: each run's records, summary, dashboard and exported traces (secrets are
  redacted from traces before they are written or sent).
- `docs/measurements/`: snapshot hashes, parity reports and the spend ledger.

Delete any of these at any time; the cache rebuilds as needed (at the cost of the calls).

The only other programs it starts are `clickhousectl` (account setup, above) and `git`, which
reads the local checkout's commit and status to stamp runs and measurements with their code
version, and never contacts a remote.

`tests/release/test_community.py` checks this statement against the code: only the modules
behind the destinations above may import networking or provider libraries (by any spelling,
aliased or loaded by name) or open a connection, and only the listed modules may start a
process, none of them a network tool.
