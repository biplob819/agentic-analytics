# B3.3 — Open-source release v0.1.0

**Wave 3. Depends on:** B3.2, D4.2. OWNER D-27, D-28, D-29; D-30; D-45; OWNER D-47 / D-97; OWNER D-93 (DEM-15); OWNER D-107 (REL-05); D-108; D-114; OWNER D-110, OWNER D-111, OWNER D-115 (ships on EvalKit 0.3.0; MON-07 moved to V5.1).

## Goal
Anyone can `pip install agentic-analytics`, see a dashboard with no keys, then run the bundled
UK benchmark or their own dataset pack with their own accounts, with the community files, docs,
privacy statement and release automation EvalKit has.

## Approach (EvalKit B2.5)
- **Release (OSS-01, OSS-07; D-116).** `.github/workflows/release.yml` on `v*` tags only, four
  jobs: `checks` (`uv sync --locked`, ruff, format, pyright, `pytest -m "not live"`, secret
  scan); `build` (read-only token: tag = `v` + version, pyproject preflight, `uv build`, wheel
  preflight, the built wheel installed and run with no keys in an empty environment, then
  `dist/` uploaded); `publish` (needs `build`, environment `pypi`, `id-token: write` alone, no
  checkout and no repository code: the pinned artifact download, setup-uv and exactly
  `uv publish --trusted-publishing always dist/*`); `smoke` on Linux, macOS and Windows with
  `uvx` against PyPI. setup-uv pins uv's version with no cache; no `if:`, `continue-on-error` or
  swallowed failure in checks/build/publish. No secret, token or password anywhere.
- **CI (OSS-02).** `ci.yml` matrix Python 3.11–3.13 × ubuntu/macos/windows (D-45 supersedes
  D-26).
- **Least privilege, enforced.** `tests/harness/test_records.py` parses every workflow:
  known names only (ci, release, pages, monitor), exact triggers, top-level `contents: read`,
  no blanket grants or stored PyPI token, extra permissions only on release/publish and
  pages/deploy with their environments (D-108 replaces B3.2's narrow pages rule), publishing
  only by trusted publishing in release/publish after `checks`; secrets only in monitor.yml's
  hand-run `smoke` job, from the `monitoring` environment and its 12-name allow-list (M4.1
  MON-01, OWNER D-103: its schedule stays commented out); planted defects for each rule.
  Mirrors EvalKit's `test_release_workflow_uses_trusted_publishing`.
- **Onboarding (OSS-03).** Documented and tested against the real parser: `init`,
  `pack check`, `report --example`, `run --dry-run`, `doctor`; single-engine mode and its stamp.
- **Docs (OSS-04).** `docs/quickstart.md`, `reproduce-the-uk-benchmark.md`,
  `writing-a-dataset-pack.md`, `adding-questions.md`, `cost.md`, `versioning.md`. Every link
  resolves to a repository file; every `agentic-analytics`/`aa` command in a shell block parses
  (built commands strictly, unbuilt ones as known subcommands).
- **Community (OSS-05).** CONTRIBUTING, SECURITY (key handling, read-only layers, private
  reporting), CODE_OF_CONDUCT, issue templates (bug, feature/pack, config with the security
  link), PR template.
- **Privacy (OSS-06).** PRIVACY.md: no telemetry, a table of every destination; a test pins the
  set of modules that import networking or provider libraries to that table.

- **Deferred rows.** DEM-15 (deploy and live p95, OWNER D-93) and REL-05 (the Pages deploy,
  OWNER D-107) need the owner's accounts, settings and spend; they stay not-started until then
  (criteria and RUN-STATE say what each needs). MON-07 moved to V5.1 (OWNER D-115).

## Findings for other nodes
- `run --dry-run` now works with no engine variables (exit 0, "no calls made", and why a real
  run would refuse); the earlier R3.1 finding is closed and the docs say so.
- pyproject: metadata, sdist excludes, `httpx2` as a direct dependency (imported by
  `backends/cortex.py`) and no `allow-direct-references` (D-109). The version bump is the
  owner's.

## Out of scope
Publishing, tagging, pushing, PyPI trusted-publisher registration, GitHub environments and settings, the Streamlit deploy, turning on the weekly monitor (all owner), and everything on EvalKit v0.4.0 (node V5.1, OWNER D-115).
