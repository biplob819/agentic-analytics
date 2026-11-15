# S2.1 run state

**Now:** green (verifier PASS with should-fixes, all fixed: column mapping, per-column tolerance D-41, number forms).

## History
- 2026-11-15 — scorers built tests-first against the N1.1 schemas and EvalKit `sql_match`; executor taken as a protocol so S2.1 does not import N1.2's `warehouse` package.
- 2026-11-15 — verifier should-fixes: own column mapping, per-column tolerance (N1.1 `Tolerance.columns`, default rel 1e-9), dates in words, larger glued numbers, locale forms, decade overlap, ratio-named percent matching, compound number words, numeric markers across forms, mutant-killing tests; known-gap note removed (D-40).
