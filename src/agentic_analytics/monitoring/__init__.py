"""Monitoring and EvalKit reuse (M4.1, OWNER D-74, OWNER D-75; V5.1 on EvalKit 0.4.0, D-120).

- `monitoring.metrics`: every run's summary also written in EvalKit's frozen metrics-1 format,
  with EvalKit's efficiency block and a per-case sidecar, in the run's `metrics/` folder
  (MON-05). It is what `evalkit gate` (MON-01) and `evalkit compare` (MON-02) read:

      evalkit gate results/<run>/metrics/metrics.json --config monitoring/gates.yaml
      evalkit compare results/<old>/metrics results/<new>/metrics

- `monitoring.probes`: probe scoring through EvalKit `scorers.attack_outcome`, a cross-check of
  S2.1's verdicts, which stay the reported ones (MON-03, D-99);
- `monitoring.efficiency`: step efficiency, tool calls against each question's minimum
  (MON-04), carried per backend in `summary.json`;
- `monitoring.parity`: EvalKit's upstreamed scorers against the stored scores of finished runs
  (MON-03, MON-06), `python -m agentic_analytics.monitoring.parity --run RUN_ID`; with
  `--rescore`, every record rescored through EvalKit's shape equivalences (V5.1 MON-08) and the
  clustered clarification intervals compared (MON-09);
- `monitoring.history`: run history, the trend page and drift flags through EvalKit's run
  ledger `docs/measurements/runs.jsonl` (V5.1 MON-07, D-121).

The scheduled smoke workflow is `.github/workflows/monitor.yml` (MON-01; its weekly schedule is
off until the release, OWNER D-103); after the gate it checks the run against its alarm band
in the committed history (`evalkit history check`, MON-07).

The modules are imported by name, not from here: `probes` pulls in the MCP client and
`aggregate` imports `efficiency`, so this package stays empty to keep imports light.
"""
