# agentic-analytics: an MCP agent against Snowflake Cortex Analyst, scored on the same data

A benchmark of two ways to answer analytics questions in plain English over the same 31.7 million
UK house sales: an agent (Claude Sonnet 5.5 or GPT-6.1 Sol) querying ClickHouse through its
remote MCP server, and Snowflake Cortex Analyst over a semantic view. 100 questions, each with
gold SQL that must agree on both engines before any run, three trials each, scored by re-running
every answer's SQL with EvalKit's `sql_match`.

On the baseline, GPT-6.1 Sol answered 97% correctly (92% under strict, pre-registered scoring),
Cortex Analyst 85% (62%) and Claude Sonnet 5.5 76% (76%). Almost every failure was a guess on an
ambiguous question instead of asking back. Claude's other misses came from an approximate median
function. One fix, five worked example queries, lifted Claude to 92% (+15.7 points), mostly by
getting it to ask back on ambiguous questions. It also made Claude follow an injected instruction
in every trial of one probe that it had resisted on the baseline. Results:
[baseline](measurements/baseline-2026-11-17.json), [fixes](measurements/fixruns-2026-11-17.json),
[failure taxonomy](measurements/failure-taxonomy-2026-11-17.json).

## My role

I wrote the product requirements and owned every ruling that shaped the benchmark: the dataset
and the as-of date, the Snowflake region (kept in Thailand, with a latency caveat), a sign-off on
all 140 questions and probes, the scoring rulings after the baseline hand check, and each paid
run's budget. I set up the ClickHouse Cloud and Snowflake accounts, approved every push, and own
the demo deploy on Streamlit Community Cloud.

Claude Code built the code, tests, runs and documents under EvalKit's node harness: tests first,
clean convergence runs, and a fresh skeptical verifier per node that tried to disprove each
criterion with planted defects. Verifiers found real defects before the paid runs (a self-check
that would have penalised correct empty answers, logs that hid every cache key). Every scope limit
is written down as a decision ([decisions](decisions.md)).

## Key decisions

**Score the numbers, not the shape, and keep the strict score beside it (OWNER D-81, D-89).**
The hand check found right answers marked wrong for their shape: Cortex returned percentages as
fractions on all 20 percent questions, and GPT pivoted tenure counts into one row. The headline
now accepts four narrow equivalences for every backend; the strict, pre-registered score is shown
beside it everywhere.
*Trade-off:* the scoring changed after the first results were seen. In exchange, the headline
answers the question a reader asks (did it get the numbers right?), and the unchanged strict
score shows exactly what moved.

**Gold must agree on both engines before any run (Q2.2).** Every gold query runs on ClickHouse
and Snowflake, must match within tolerance, and is checked for ties at a top-N cut and for
determinism; two questions were reworded when their answers tied.
*Trade-off:* every question costs two gold queries and some rewording. In exchange, a backend is
never scored against an answer that depends on which warehouse computed it.

**Freeze the fixes before the baseline and measure each alone (D-13, D-14).** The three fixes
were hashed before any baseline run and each was run on its own against the same baseline, with
a Holm correction across them.
*Trade-off:* no tuning between runs, and three runs instead of one. In exchange, the
gains are honest, including the unwelcome ones: descriptions cost GPT 4.7 points, and examples
made Claude easier to steer with an injected instruction.
