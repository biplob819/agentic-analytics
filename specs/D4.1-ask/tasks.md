# D4.1 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO
- [x] 3. Tests first (`tests/demo/test_ask.py`, `test_guard.py`, `test_cache.py`, `test_live.py`)
- [x] 4. Build (`demo/ask.py`, `demo/cache.py`, `demo/example/replay.json`, `cli.py demo-cache`)
- [x] 5. Converge twice (lane worktree, 2026-11-17)
- [x] 6. Verifier (independent: round 1 NOT GREEN, fixed; round 2 GREEN)
- [ ] 7. Proofs (live-only rows need AA_LIVE=1 and the owner's accounts), green, commit
- [x] 8. OWNER D-56 visitor own keys (`demo/keys.py`, own-key limit, `tests/demo/test_keys.py`); converge and verifier still to run
- [x] 9. OWNER D-58..D-60 live switch (`live_backends`, `contact_url`, `run_live(live=)`) and D-57 Snowflake key text (`ro_key_from_env`, executor, Cortex JWT, `services_from_env`, doctor); tests in `tests/demo/test_saved.py` (the D-57 key-text tests are `tests/warehouse/test_snowflake_key_text.py`, committed with R3.1, D-79); converge and verifier still to run
- [x] 10. Verifier round 1 (NOT GREEN: criteria resting on tests that could not fail) fixed: 11 findings and the cheap nits, each mutant re-run and killed; verifier round 2 still to run
