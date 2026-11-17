# F3.1 tasks

- [x] 1. Spec and criteria written
- [ ] 2. node_start GO (not run by the builder)
- [x] 3. Tests first (`tests/fixes/`: planted leak, planted rule hits, changed file after freeze)
- [x] 4. Build (`agentic_analytics/fixes/`, `packs/uk_price_paid/fixes/examples.yaml`, `self_check.yaml`)
- [x] 5. Freeze (`fixes/FROZEN.json`, before any baseline run)
- [x] 6. Live: run the five examples on ClickHouse and the result-set leak check against the real gold (after `agentic-analytics gold`; 2026-11-17: all pass, 0/84 self-check false positives, no re-freeze)
- [x] 7. Runner wiring of `fix_blocks(pack)` and `make_self_check` is R3.1's (`run/runner.py`, committed with R3.1)
- [x] 8. Converge twice (isolated tree, 2026-11-17: PASS)
- [x] 9. Verifier (independent: round 1 GREEN with conditions, fixed; round 2 GREEN)
- [x] 10. Proofs, green, commit
