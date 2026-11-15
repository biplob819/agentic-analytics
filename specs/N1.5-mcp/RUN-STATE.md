# N1.5 run state

**Now:** green (verifier round 2 passed with conditions, all fixed with isolating tests; converged on a clean checkout; L-4).

## History
- 2026-11-15 — built against mcp 2.3.0.
- 2026-11-15 — verifier round 1 NOT GREEN (MCPErrors never retried; shape crashes; no char cap; first page only); fixed.
- 2026-11-15 — verifier round 2 passed with conditions (rows_before_limit misread, timeout retries vs budget, reopen across tasks); fixed.
- 2026-11-15 — flaky jitter test found by converge; seeded.
