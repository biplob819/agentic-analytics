# Q2.1 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO (recheck after N1.3 went green)
- [x] 3. Tests first (loader, one planted defect per lint rule, shipped-bank invariants, pack check)
- [x] 4. Build (offline: `questions.jsonl`, `probes.jsonl`, `questions.bank`, `questions.lint`, `check_pack`)
- [x] 5. CLI wiring of `pack check [--pack PATH] [--json]` onto `check_pack` (exit 0 clean, 1 issues, 2 usage)
- [x] 6. Dual authorship, QST-04: GPT Sol pass over the 100 answer questions and the disagreement report (run live 2026-11-16: 82 agree, 18 shape-only disagreements, none on substance; `docs/measurements/dual-authorship-2026-11-16.json`)
- [x] 7. Owner review sitting, QST-05 (OWNER D-71, with D-69); gold check re-run after the rewordings
- [x] 8. Converge twice (isolated tree, 2026-11-17: PASS, 0 failing gates)
- [x] 9. Verifier (independent: round 1 NOT GREEN, fixed; round 2 GREEN)
- [x] 10. Proofs, green, commit
