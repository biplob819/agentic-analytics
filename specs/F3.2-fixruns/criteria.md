# F3.2 acceptance criteria

**FXR-01** The fix runs cover each of the three fixes alone and all three together (§7, never a headline figure) on claude_mcp and gpt_mcp at 3 trials over the 100 selected questions and the probes they name, with the frozen fix_hash on every record and the baseline answers shared; spend logged in the ledger. *Proof:* `docs/measurements/fixruns-2026-11-17.json` and `tests/run/test_fixrun_record.py`.

**FXR-02** The uplift table gives, per MCP backend and fix, the paired change in accuracy points with a 95% interval, a p-value and Holm separation (§6.2), from the headline score, with the strict accuracies shown beside. *Proof:* the record's `uplifts` and `per_config`, held consistent by `tests/run/test_fixrun_record.py`.

**FXR-03** Thirty baseline failures, spread over the three backends and the categories, are hand-labelled with one primary label each (wrong filter, wrong column or join, wrong aggregation, literal case, time anchoring, missed clarification, or `other` with a description when none fits, e.g. a diagnostic query submitted as the answer) and evidence. *Proof:* `docs/measurements/failure-taxonomy-2026-11-17.json` and its test.
