# N1.4 tasks

- [x] 1. Spec and criteria written
- [ ] 2. node_start GO (rows marked `building` by hand 2026-11-16; check-only waits on N1.3 green)
- [x] 3. Tests first (model validation against the pack's load SQL, DDL golden files with quoting, prompt block determinism and golden, hash stability and sensitivity, parity with planted defects)
- [x] 4. Build (offline half: `semantic.yaml`, model, DDL generator and reader, prompt block and hash, parity)
- [ ] 5. Owner review of `semantic.yaml` (QST-05 / SEM review sitting)
- [ ] 6. Converge twice
- [ ] 7. Verifier
- [x] 8. Live run on the Snowflake trial account: check the DDL grammar, create the view, grant to `ANALYTICS_RO`, `DESCRIBE SEMANTIC VIEW`, sample values present in the data
- [ ] 9. Proofs, green, commit
