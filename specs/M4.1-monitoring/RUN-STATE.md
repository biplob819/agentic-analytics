# M4.1 run state

**Now:** GREEN 2026-11-17, committed. Independent verifier GREEN in round 2 (its two remaining test gaps, `continue-on-error` on the job or on a step from the smoke run onward, closed before commit). MON-07 (ledger, trend, drift) waits for EvalKit v0.4.0 under B3.3 (OWNER D-98); the weekly schedule is off until the release (OWNER D-103).

## Owner rulings (2026-11-17)
- OWNER D-98: MON-02 is split; MON-02 keeps `evalkit compare`, run history, the trend page and drift flags are MON-07 in B3.3 (EvalKit B5.7, v0.4.0).
- OWNER D-100: the clarification verdict and its bootstrap-over-questions rates stay the reported figures; EvalKit's `clarification_rates` is a cross-check until EvalKit offers a per-question (clustered) interval, then this project switches (amends OWNER D-75 (2)). The answer-from-rows check runs from EvalKit.
- OWNER D-103: the weekly schedule stays off until the release.

## History
- 2026-11-17 — first lane (EvalKit 06591f8): MON-03 adapter and parity on the golden probes, MON-04 step efficiency, the monitor workflow with a local threshold check; gate, export and upstreamed scorers waited for v0.3.0 (D-76).
- 2026-11-17 — EvalKit v0.3.0 tagged (4ddfef5) and published to PyPI; pin `evalkit-harness==0.3.0` (D-97). MON-05 export, MON-01 on `evalkit gate`, MON-02 compare, MON-06 from EvalKit. Parity on every stored record (5,046 records, 810 probe answers): identical (docs/measurements/evalkit-parity-2026-11-17.json).
- 2026-11-17 — verifier round 1 NOT GREEN on records; two workflow mutants survived. Round 2: secrets in a workflow- or job-level `env` now fail both workflow checks (W2); the gate step must be exactly the gate command piped to the summary under `shell: bash` (`-eo pipefail`), so `|| true`, `; true`, `exit 0`, `set +o pipefail`, `continue-on-error` and `if` all fail (W4); the spend line shows only what the run appended; the run's metrics folder, gate verdict and spend line are uploaded as an artifact two CI runs can be compared from; MON-01/03/05/06 row texts match what was built; `step_efficiency` answers exported as `n`.
