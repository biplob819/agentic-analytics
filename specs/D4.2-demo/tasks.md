# D4.2 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO (not run by the builder)
- [x] 3. Tests first (`tests/demo/test_page.py`, live p95 in `test_live.py`)
- [x] 4. Build (`demo/page.py`, `demo/app.py`)
- [x] 5. Converge (isolated lane, 2026-11-17: PASS)
- [x] 6. Verifier
- [x] 7. Proofs, green, commit (the deployment and live p95 are DEM-15 under B3.3, OWNER D-93)
- [x] 8. OWNER D-56 own-key fields, notice and forget control (DEM-06..09, `tests/demo/test_byok_page.py`); converge and verifier still to run
- [x] 9. OWNER D-58..D-60 saved answers by default (DEM-10..12, `tests/demo/test_saved.py`) and D-57 own Streamlit app (DEM-13..14: `demo/app.py`, `demo/requirements.txt`, `demo` extra, `scripts/streamlit_secrets.py`; `tests/demo/test_deploy.py`, `tests/demo/test_streamlit_secrets.py`); converge and verifier still to run
- [x] 10. Verifier round 1 NOT GREEN: findings fixed in round 2; OWNER D-93 split DEM-05 (DEM-15 to B3.3); converge PASS; verifier round 2 to run
