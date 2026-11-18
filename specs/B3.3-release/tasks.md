# B3.3 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO (check only; `.base` 2e0f70b; OSS-01..07 flipped to building by hand so DEM-15, MON-07, REL-05 stay not-started)
- [x] 3. Tests first (`tests/release/test_docs.py`, `test_community.py`, `test_workflows.py`; workflow policy in `tests/harness/test_records.py`)
- [x] 4. Build (release.yml, CI matrix, docs, community files, templates; D-45, D-46)
- [ ] 5. Converge twice (offline: 11/12 pass; proofs fails only on DEM-15, MON-07, REL-05)
- [ ] 6. Verifier
- [ ] 7. Proofs, green, commit
- [x] 8a. pyproject metadata, sdist excludes, no direct references (D-109)
- [ ] 8b. Owner: push, GitHub settings, PyPI trusted publisher, REL-05 deploy, DEM-15 deploy and p95, version bump, tag v0.1.0 (RUN-STATE)
- [x] 9. EvalKit v0.4.0 work moved to V5.1 (OWNER D-115: pin move, MON-07, MON-08 B5.15 equivalences, MON-09 clustered intervals); v0.1.0 ships on EvalKit 0.3.0
