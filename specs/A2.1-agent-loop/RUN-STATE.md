# A2.1 run state

**Now:** green (verifier PASS with should-fixes, all fixed; live smoke AGT-06 waits for accounts).

## History
- 2026-11-15 — built against the N1.5 toolbox and N1.6 adapters; error mappings, token attribution and over-budget grace decided in spec.md; 19 hand mutants of the loop killed (one survivor, write detection from the query, closed with an isolating test).
- Open: live smoke (AGT-06) not yet run against the real MCP server; fake server payloads still canned, not recorded from the real one (Tech Spec §13).
- 2026-11-15 — follow-ups: N1.5's final API (`needs_reopen`/`ensure_open`, `call_tool(timeout_s=)`, unretried timeouts) and the injected SQL guard (D-37); 7 more mutants, the 2 survivors closed with isolating tests.
- 2026-11-15 — verifier round 1 PASS with should-fixes: self-check feedback now needs 20 s of wall clock and falls back to the first answer on an overrun; the hook runs under the wall clock; blank feedback is passed; loop texts join the prompt hash; guard exceptions documented; 3 surviving mutants killed.
