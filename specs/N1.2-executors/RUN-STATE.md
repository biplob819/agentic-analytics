# N1.2 run state

**Now:** green (verifier round 5 GREEN; live doctor run against the owner's accounts is a precondition for N1.3, D-36; L-5, D-38).

## History
- 2026-11-15 — verifier round 4: replaced the position-based IN rule with a name rule over every documented ClickHouse table function; `IN (today())` and similar allowed again; 38 mutants all killed.
- 2026-11-15 — verifier round 3: `IN table_function(...)` bypass, ARRAY JOIN function false positive, line-comment `/*` false positive, `TO_FILE`; fixed with isolating tests; 36 mutants all killed.
- 2026-11-15 — verifier round 2: ARRAY JOIN subquery bypass, `IN system.x`, count after a trailing comment, false positives on deny-listed table and column names, `@` and `$1`, nested comments; fixed with isolating tests; 31 mutants all killed.
- 2026-11-15 — verifier round 1 disproved EXE-01 (SETTINGS on set operations, quoted Snowflake stages) plus 9 smaller findings; all fixed with isolating tests; 20 mutants of the fixes and of the previously surviving checks all killed.
- 2026-11-15 — spec and criteria; guard (allow-list over sqlglot), ClickHouse and Snowflake executors with fakes built from real driver objects (EK L-17), role SQL with a checker and planted-defect tests (EK L-1), doctor core. Found that sqlglot reads `FROM dictionary('d')` as a table with alias columns; the guard now refuses that shape.

## Open points for the live run
- Confirm the ClickHouse Cloud service accepts `CHANGEABLE_IN_READONLY` (it may need `settings_constraints_replace_previous`); if not, the executor still caps rows client-side and the profile's `max_execution_time` applies.
- Confirm the error codes the trial accounts return for refused writes and `url()` (doctor accepts ClickHouse 164/291/497 and Snowflake errno 3001 / SQLSTATE 42501).
- Confirm Snowflake accepts `GRANT SELECT ON FUTURE SEMANTIC VIEWS`.
