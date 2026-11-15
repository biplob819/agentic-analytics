# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, MEASURE.

## 2026-11-15
- [HARNESS] [W0.1] — repo and lean-Kova harness copied from EvalKit and re-pointed (wave letters W N Q A S M R F B D, gate 8 `gold`); graph (22 nodes) and catalog (123 capabilities) generated from Tech Spec §12; decisions D-1 to D-26; EvalKit pinned at 06591f8 behind an import contract test; CI on 3.11/3.12 × Linux/macOS; secret scan extended for Langfuse keys and any URL with a password
- [HARNESS] [W0.1] — verifier round 1 (GREEN, 8 should-fix and nits): workflow, catalog and env tests now catch planted defects; URL-password scan covers every scheme and skips placeholders; pre-commit runs through uv; CLI renamed `agentic-analytics` + `aa` (OWNER D-28); spec amended for PyPI release (OWNER D-27), dataset packs (OWNER D-29) and node B3.3 (D-30)
- [FEAT] [N1.1] — schemas and config: questions, probes, gold results, answers, executions, scores, records, summaries, `RunConfig` YAML loader and the `DatasetPack` manifest; frozen and strict (EK L-8, L-9); invariants make contradictory states unrepresentable; results format v1 fixtures
- [FIX] [N1.1] — verifier rounds 1–3: record scores tied to answer and execution, JSON-only result cells, ASCII ids, strict numbers and booleans, YAML merge precedence and duplicate checks, pickle/deepcopy, pack URL and name rules; isolating tests for every invariant (L-2)
