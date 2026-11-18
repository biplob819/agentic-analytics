# B3.2 — README, deploy and write-up figures

**Wave 3. Depends on:** B3.1, F3.2, M4.1. **Unblocks:** B3.3.

## Goal
Every number a reader sees outside the dashboard (README headline, write-up figures, the
published dashboard) is generated from a finished run's `summary.json`, never typed by hand,
and carries the run's caveats: judge-free scoring, trials, gold mode, pack, as-of date, the
conservative intervals (D-42) and, for the bundled example, that it is synthetic.

## Approach (EvalKit B2.3, OWNER D-47)
- `agentic_analytics.report.readme`: `headline_block(summary)` returns the text between
  `<!-- headline:start -->` and `<!-- headline:end -->`: one chart reference (absolute raw URL
  of `docs/assets/headline.svg`, since the README is also the PyPI page) and exactly three
  sentences. Sentence 1: backends, question count, pack, trials, as-of date, scored without an
  LLM judge against gold SQL in the run's gold mode, ambiguous questions scored on asking back.
  Sentence 2: baseline accuracy per backend with 95% CIs, and the largest fix effect above
  noise (or that none helped, or that one hurt). Sentence 3: the intervals are two-level
  bootstrap intervals, conservative by design (about 99% coverage in simulation), so overlap is
  not a difference; cost pending when unreconciled. Figures to one decimal (`report.format`).
  A synthetic summary opens with "In the bundled SYNTHETIC example (generated data, not a
  measurement)". `update_readme(path, summary)` splices the block; it refuses missing,
  repeated or reversed markers and is idempotent.
- `agentic_analytics.report.figures`: standalone SVGs (own CSS, light/dark, `<title>`/`<desc>`,
  per-mark tooltips, shapes plus words, no external resources): `headline.svg` (every
  configuration's accuracy and CI), `categories.svg` (baseline accuracy by category, one shape
  per backend, "a map, not a ranking"), `uplift.svg` (fix deltas in points with the Holm verdict
  in words). Each prints the caveat stamps and the pack's licence credit. Pure functions of the
  summary.
- `scripts/publish_results.py RUN_DIR`: refuses the synthetic example and unfinished runs;
  renders the dashboard (B3.1 `render_report`) into `site/` with `summary.json` and exported
  traces (stale files removed), writes the figures into `docs/assets/` (a stale `uplift.svg`
  removed when a run has no fixes), regenerates the README block. `--example --root DIR`
  publishes the example into a scratch copy only, never this repository.
- `.github/workflows/pages.yml`: deploys `site/` to GitHub Pages on `workflow_dispatch` only,
  refusing an empty site or the synthetic example; top-level `contents: read`, the deploy job
  alone holds `pages: write` + `id-token: write` in environment `github-pages` (D-46).
- README rewritten: what it is, honest status (no results, not on PyPI), install (after
  v0.1.0), no-key quickstart, keyed flow, dataset packs, how scoring works (no judge,
  `sql_match`, clarification pair, conservative intervals), cost/privacy/security, documents
  (PRD, Tech Spec, decision log, data card, lessons), licence credit (kept in step with
  `pack.yaml` by `tests/packs`). Markers hold a "No results yet" placeholder.

## Decisions
- D-46: GitHub Pages, REL-02's named fallback, because Cloudflare Pages needs a stored API
  token and no workflow holds a secret. Cloudflare can follow if the owner adds a token.
- The report link sits outside the markers (static Pages URL, "live after the first published
  run"), so the block is exactly one chart reference and three sentences.
- Fix `all` is never the headline fix (Tech Spec §7).

## As built on the real runs (lane, before verifier)
- The published run is the fix-run rescore `rescore-20261117T122217Z-6ccebe` (baseline and
  every fix set, scored under OWNER D-81/D-89/D-92), reconciled offline with the baseline's
  metered Snowflake warehouse credits (0.313194458, `snowflake-metered-2026-11-17.json`), so
  Cortex's cost per correct answer equals `baseline-2026-11-17.json`. Its `summary.json` and
  `run.json` are committed under `docs/results/`; the README blocks and figures regenerate from
  them byte for byte (tests), and a test pins them to the measurement records.
- OWNER D-81 everywhere: strict beside each headline accuracy in the sentences and the table, a
  hollow mark in the headline and category figures.
- A second generated block (`<!-- results:start -->` … `<!-- results:end -->`): the results
  table, the single-fix uplifts with their Holm verdict, the agents' model ids from run.json and
  the D-62 Cortex latency caveat. The all-fixes run is in no README figure (§7).
- `publish_results.py` now passes the run's stamped Snowflake region to the dashboard (it
  printed "region not recorded" before) and writes `docs/results/`.
- OWNER D-60 screenshots (`docs/assets/dashboard.png`, `demo.png`, `demo-passcode.png`):
  taken with the installed Chrome through Playwright (`channel="chrome"`), the dashboard from
  the published site and the demo run offline (`streamlit run demo/app.py` under `env -i`,
  saved answers only), quantised to 96-colour PNGs with no text chunks.
- The case study is REL-03's "how I built it" note (proposed decision).
- `site/` (about 9 MB with 780 traces) is not committed by this node. OWNER D-107 splits REL-02:
  the publish tooling and the hand-run workflow stay here; the deploy is REL-05 in B3.3.
- Decisions: D-104 (published record; run timestamps in the screenshot are run data, fine
  under OWNER D-20), D-105 (results block, strict beside headline, `all` in no README figure),
  D-106 (case study as the "how I built it" note), OWNER D-107 (REL-02 split), D-108 (narrow
  pages.yml rule until B3.3's policy).

## Out of scope
The Pages deploy itself (REL-05 in B3.3, OWNER D-107), links to B3.3's files (CONTRIBUTING, SECURITY,
PRIVACY, docs guides), PyPI.
