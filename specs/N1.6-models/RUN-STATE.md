# N1.6 run state

**Now:** green (converged twice on a clean checkout; verifier round 2 green after one fix; L-3).

## History
- 2026-11-15 — built; Cortex rate verified from the consumption table.
- 2026-11-15 — verifier round 1 NOT GREEN (quoted `verified` accepted; 9 mutants survived); fixed.
- 2026-11-15 — verifier round 2: odd 429 bodies crashed the quota check; fixed with nits; green.
