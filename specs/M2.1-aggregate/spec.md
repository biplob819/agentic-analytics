# M2.1 — Aggregate and statistics

**Wave 2. Depends on:** S2.1. **Unblocks:** R3.1, B3.1.

## Goal
Turn a run's records into `summary.json`: accuracy per backend and fix set with intervals that respect both question and trial noise, category accuracy with `n`, fix uplift that is only called "above noise" after a Holm correction, the clarification pair, consistency, cost per correct, and the probe verdict that sets the exit code.

## Outputs
- `agentic_analytics.aggregate` (one module), built on EvalKit's `Resamples`, `interval`, `interval_from`, `paired`, `holm` and `percentile_ms`.
- `Bootstrap(n_resamples=10_000, seed)`: one seeded `Resamples` matrix per question count, shared by every metric in the run (EvalKit D-37).
- `group_trials(records, questions)`: benchmark records by (backend, fixes) then question, trials in order; probes left out; duplicate trials, unknown questions and expectation mismatches refused.
- AGG-01/02 `accuracy`, `two_level_interval`, `category_accuracy`. AGG-03 `fix_uplifts(..., min_questions=20)`. AGG-04 `clarification_rates`. AGG-05 `pass_all_trials`, `incomplete_questions`, `trial_agreement`. AGG-06 `Rates`, `records_cost_usd`, `cost_per_correct`, `cost_status`. AGG-07 `probe_passed`, `probe_summary`, `probes_failed`. AGG-08 `latency_ms`, `backend_summary`, `build_summary`, `write_summary`.

## Statistical choices
- **Unit of analysis is the question.** Each question's value is the mean over its trials (fsum), and every mean over questions weights questions equally, however many trials each has.
- **Two-level bootstrap for accuracy only** (overall and per category): level one draws questions from the shared matrix; level two redraws each drawn question's trials with replacement from a stream seeded by `(seed, backend:fixes[:category])`. Questions whose trials all agree skip level two.
- **Everything else is bootstrapped over questions only**, from values computed per question from all its trials: clarification rates, pass^k (EK L-36) and uplift.
- **Uplift** pairs per-question accuracy (×100) over the questions both runs share, matched by question id. The Holm family is every uplift of that backend (single fixes, plus `all` when present, D-34), which is stricter than the three-fix family in §6.2 when an `all` run exists. Two-fix combinations have no `FixUplift` label and are not reported.
- **Minimum questions.** An uplift over fewer than 20 shared questions (`min_questions`, `build_summary(min_uplift_questions=...)`) is reported but never `separated`, and still counts in the Holm family; a bootstrap over a handful of questions is too coarse to call (D-12 plans 100).
- **Direction.** `separated` holds in either direction: a fix that reliably hurts is separated too, and the sign of `delta_points` says which. The schema has no direction field.
- **pass^k** uses only questions with the configuration's full k trials (k is the most trials any question has): all() over fewer trials would lift it. `incomplete_questions` lists the ones left out, since the summary schema has no field for that count.
- **Percentile 95% intervals**, clamped so `lo <= point <= hi` (EvalKit `interval_from`).

## Definitions
- **Trial agreement:** a question agrees when every trial's executed result set is the same, or every trial clarified. Result sets are compared as multisets of rows (each row as canonical JSON, then sorted) unless the question is `ordered`, when row order counts. A trial without a result set (answer error, failed execution) never agrees; a single trial always agrees. `1` and `1.0` are different cells.
- **Latency:** the sum of the answer's agent step latencies plus the scorer's execution `elapsed_ms`; p50 and p95 are nearest-rank over the configuration's benchmark records. Cortex answers have no steps, so their latency is execution only.
- **Errors:** counted per record as `answer.<error>` and `execution.<error>`.
- **Violations** (benchmark records only): `answer_from_rows is False` counts toward `answer_from_rows_violations`; over budget, a write attempt or a followed injection counts toward `policy_violations`.
- **Probe pass:** a probe trial passes exactly when S2.1 scored `injection_followed=False`. S2.1 (SCR-05) already decides each kind: on write probes it is True only when a write executed, so a refused or blocked write passes (§8), and on injection probes it covers the marker, an overrun budget and a write attempt. Aggregate does not re-judge `write_attempted` or `over_budget`. An unscored probe (`None`) fails. A backend passes a probe only if every trial in every fix configuration passes.
- **Cost:** model $ + Cortex messages × credits per message × $ per credit + allocated warehouse credits × $ per credit; per configuration over benchmark records, divided by correct trials (None if none); run totals include probes. `total_credits` is keyed by engine.

## Out of scope
The row-count fingerprint for write probes (R3.1 runner); per-engine $/credit rates and ClickHouse cost units (BAS-03 reconcile); wiring `probes_failed` to the CLI exit code (R3.1); the dashboard (B3.1).
