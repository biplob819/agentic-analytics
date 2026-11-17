# R3.2 acceptance criteria

**BAS-01** One baseline run covers 3 trials × the 100 selected questions on claude_mcp, gpt_mcp and cortex_analyst, plus each probe on the backends it names (1,065 jobs), with every record stamped with the gold_check_id, snapshot, fix_hash and code version, fixes off; the spend is in the ledger (model dollars and Cortex credits). *Proof:* the run's `records.jsonl` and `run.json`, summarised in `docs/measurements/baseline-2026-11-17.json`, and `tests/run/test_baseline_record.py`.

**BAS-02** Each trial set finishes in under 45 minutes, measured from `run.log` (first and last ask of each trial). *Proof:* the baseline record's `trial_wall_minutes` (whole run 14.8 minutes).

**BAS-03** After ACCOUNT_USAGE's lag, the metered Snowflake credits for the run window are passed to `agentic-analytics reconcile --credits`, so `cost_status` is `reconciled` and every Cortex record carries `warehouse_credits_alloc`. *Proof:* the reconciled `summary.json` of `rescore-20261117T100232Z-c89eaa` and the baseline record's `reconciled` block, from `docs/measurements/snowflake-metered-2026-11-17.json`. `reconcile` takes warehouse credits only; Cortex credits come from each record's messages.

**BAS-04** Ten failures, spread across the three backends and the categories, are opened from their traces and each score is confirmed or disputed by hand; any scorer defect found is fixed (and the run rescored, no new calls) before F3.2. *Proof:* the baseline record's `hand_check` list (question, backend, trial, verdict, reason).
