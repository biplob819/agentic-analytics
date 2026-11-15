# N1.6 run state

**Now:** green (reopened for D-54 and green again: GPT adapter on the Responses API; offline suite and one live round trip pass).

## History
- 2026-11-15 — built; Cortex rate verified from the consumption table.
- 2026-11-15 — verifier round 1 NOT GREEN (quoted `verified` accepted; 9 mutants survived); fixed.
- 2026-11-15 — verifier round 2: odd 429 bodies crashed the quota check; fixed with nits; green.
- 2026-11-15 — reopened for D-54: a live check got 400 "Function tools with reasoning_effort are not supported for gpt-6.1-sol in /v1/chat/completions"; `OpenAIAgent` moved to the Responses API (stateless: `store=False`, encrypted reasoning items replayed verbatim, no `previous_response_id`; flat tools with explicit `strict`; cache-write tokens reported); `AgentModel`/`ModelTurn` contract unchanged, so A2.1, Q2.2 dual authorship and the demo are untouched. Fakes now return real SDK `Response` objects; 15 planted defects (reasoning counted as output, reasoning or encrypted content dropped between turns, `tools: []`, `include`/`store`/`strict` wrong, cut-offs and filters ignored, ...) each fail a test. Live round trip on gpt-6.1-sol (effort low) green. Green.
- 2026-11-15 — D-54 verifier: GREEN (13/16 mutants killed; live two-turn replay of encrypted reasoning at effort medium accepted, $0.0037). Follow-up from its should-fix list: a reply with status `failed` is raised as `ResponseFailed` (status 503) inside the retry, so it is retried and then classified `transport` instead of looking like a cut-off turn, and its output never enters the history; the strict test tool now has a schema the real API accepts.
