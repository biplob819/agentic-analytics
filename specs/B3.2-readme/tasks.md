# B3.2 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO (eligibility confirmed by node_converge gate 2)
- [x] 3. Tests first (`tests/release/test_readme.py`, `test_publish.py`, `test_workflows.py`)
- [x] 4. Build (`report/readme.py`, `report/figures.py`, `scripts/publish_results.py`, `pages.yml`, README)
- [x] 5. Converge twice (lane)
- [x] 6. Verifier (independent, GREEN in round 2)
- [x] 7. Proofs, green, commit
- [x] 8. After the live runs: `publish_results.py` on the reconciled fix-run rescore (lane); owner review and the `pages` run remain
- [ ] 9. OWNER D-60 README: done in B3.2 for the guide, the dashboard, demo and offline CLI screenshots (`init`, `pack check`, `report --example`, `run --dry-run`, `doctor` with no keys) and every link to a file that exists; B3.3 adds the PyPI, live demo, Pages dashboard, CONTRIBUTING and SECURITY links and a `doctor` / `run` screenshot against real accounts (D-105) for the open-source launch:
  - screenshots of the demo (saved-answer view, passcode and own-key fields), the dashboard and the CLI (`doctor`, `run`, `report`);
  - a step-by-step guide: install, `init`, `setup clickhouse` / `setup snowflake`, `doctor`, run, report, own packs;
  - every important link: PyPI, GitHub, live demo, GitHub Pages dashboard, EvalKit, data source and licence, decision log, Tech Spec, CONTRIBUTING, SECURITY.
