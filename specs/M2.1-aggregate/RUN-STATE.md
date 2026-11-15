# M2.1 run state

**Now:** green (verifier round 2 GREEN; D-42 budget rule and Holm-family test added after).

## History
- 2026-11-15 — spec, criteria, `aggregate.py` and `tests/aggregate/`. Full-size timing: 3,300 records over 11 configurations at 10,000 resamples build in about 7 s. `interval_from` is imported from `evalkit.stats` but not yet listed in `tests/test_evalkit_contract.py` (HAR-06); `tests/aggregate/test_accuracy.py` pins it meanwhile.
- 2026-11-15 — verifier round 1 (NOT GREEN) fixed: probe verdict is `injection_followed is False` (S2.1 owns write judgment); uplifts need 20 shared questions to separate; D-34 pinned by a test; trial agreement compares multisets unless `ordered`; pass^k only over questions with all k trials; L-27 test uses values where 3.12 `sum` differs; pairing test puts unshared questions first and in the middle.
