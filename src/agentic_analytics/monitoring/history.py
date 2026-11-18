"""Run history, the trend page and drift flags through EvalKit's run ledger (V5.1 MON-07;
EvalKit B5.7; OWNER D-74, OWNER D-75, OWNER D-98, OWNER D-115, D-121).

**A band point is one distinct set of answers, compared only with comparable runs.**

- **The ledger.** EvalKit's hash-chained run ledger at its default path,
  `docs/measurements/runs.jsonl`, beside the spend ledger. A run does not append its whole
  metrics-1 export (`metrics/metrics.json`, MON-05) but a ledger view of it,
  `history/metrics.json` (`ledger_metrics`), recorded with `evalkit.ledger.record_run` and
  stamped with the export's own `created_at` (the run's finish time).
- **New answers only.** A configuration (backend and fix set) is in the view only when this
  run asked at least one of its answers (`new_configs`); a configuration served entirely from
  the answer cache (the fix run's reused baseline answers, a rescore) adds no point, so the
  same answers are never two points. Rows of the whole run (the cost total, credits, probes
  per backend) are kept only when every configuration is new; an uplift row only when its fix
  configuration is. A run with no new configuration appends nothing.
- **Comparable runs only.** Every row carries `scope` (`smoke` for the 12-question smoke run,
  `full` otherwise), so EvalKit never bands a smoke run against a full one, and every cost row
  (`COST_METRICS`) carries `cost_status`: the run appends its estimated costs
  (`cost_status=estimated`); a reconcile appends only the reconciled cost rows of the run's
  new configurations (`cost_status=reconciled`), never its accuracy again, so neither series
  is counted twice and a reconcile is never judged against its own run's line.
- **Drift.** `record_run` judges each row against the comparable history before it: median
  and 1.4826 × MAD bands from at least 5 earlier runs, warn beyond 2 spreads, alarm beyond 3;
  with `monitoring/gates.yaml` (`GATES`, passed whenever it exists) a gated accuracy alarms
  only downward and reads `improved` upward. Fewer than 5 comparable runs is `learning`, never
  flagged. The verdict goes to the run log and output; the gate (MON-01), not the band,
  decides the monitor job, and the job's drift step (`evalkit history check` on
  `history/metrics.json`) fails on an alarm.
- **The trend page.** `trend_page` writes EvalKit's history page (`history_html`) for
  accuracy with its intervals, cost per correct answer and p95 latency, each run's band and
  every flagged run marked; `scripts/publish_results.py` writes it beside the dashboard as
  `trend.html`, and the monitor job writes the same page with
  `python -m agentic_analytics.monitoring.history --ledger ... --out trend.html`.

Nothing here makes a call.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from evalkit.ledger import RUNS_PATH, History, LedgerError, RunCheck, history, history_html
from evalkit.ledger import record_run as ek_record_run
from evalkit.metrics import Metric, MetricsSummary, load_metrics

from agentic_analytics.monitoring.metrics import METRICS_DIR, METRICS_FILE, metrics_json
from agentic_analytics.schemas._base import FIX_NAMES

LEDGER = Path(RUNS_PATH)  # docs/measurements/runs.jsonl, EvalKit's default
LEDGER_NAME = LEDGER.name
GATES = Path("monitoring/gates.yaml")
HISTORY_DIR = "history"  # the ledger view: <run>/history/metrics.json, no per-case sidecar
TREND_METRICS = ("accuracy", "cost_per_correct", "latency_p95")
TREND_TITLE = "Run history: accuracy, cost per correct answer and p95 latency"
FLAGGED = ("warn", "alarm")
COST_METRICS = frozenset(
    {"cost_per_correct", "cost_per_case", "system_cost", "total_cost_usd", "total_credits"}
)
NO_FIXES = "none"
ALL_FIXES = "+".join(FIX_NAMES)

Scope = Literal["smoke", "full"]
CostStatus = Literal["estimated", "reconciled"]
Config = tuple[str, str]  # (backend, fixes label: "none" or the fixes joined by "+")


def scope_of(kind: str) -> Scope:
    """A run's scope from its plan: the smoke run's 12 questions, or the full bank."""
    return "smoke" if kind == "smoke" else "full"


def config_label(backend: str, fixes: Sequence[str]) -> Config:
    return backend, "+".join(fixes) or NO_FIXES


def _row_config(m: Metric) -> Config | None:
    """The configuration a row belongs to, or None for a row of the whole run."""
    labels = m.labels
    if "model" not in labels:
        return None
    if "fixes" in labels:
        return str(labels["model"]), str(labels["fixes"])
    if "fix" in labels:  # an uplift: its fix configuration
        fix = str(labels["fix"])
        return str(labels["model"]), ALL_FIXES if fix == "all" else fix
    return None  # per backend over every fix set (probes)


def ledger_metrics(
    metrics: MetricsSummary,
    *,
    scope: Scope,
    new_configs: Iterable[Config],
    all_configs: Iterable[Config],
    cost_status: CostStatus,
    costs_only: bool = False,
) -> MetricsSummary | None:
    """The ledger view of a metrics-1 export (the module docstring): the rows of the new
    configurations, labelled with `scope` and, on cost rows, `cost_status`; with `costs_only`
    (a reconcile) the cost rows alone. None when nothing is new."""
    new = set(new_configs)
    whole = bool(new) and new >= set(all_configs)
    rows: list[Metric] = []
    for m in metrics.metrics:
        cost = m.name in COST_METRICS
        if costs_only and not cost:
            continue
        cfg = _row_config(m)
        if (cfg is None and not whole) or (cfg is not None and cfg not in new):
            continue
        labels = {**m.labels, "scope": scope}
        if cost:
            labels["cost_status"] = cost_status
        rows.append(m.model_copy(update={"labels": labels}))
    if not rows:
        return None
    return metrics.model_copy(update={"metrics": rows})


def write_ledger_view(
    run_dir: Path,
    *,
    scope: Scope,
    new_configs: Iterable[Config],
    all_configs: Iterable[Config],
    cost_status: CostStatus,
    costs_only: bool = False,
) -> Path | None:
    """Write the ledger view `history/metrics.json` from the run's `metrics/metrics.json` (a
    folder of its own: EvalKit would check a view beside `cases.jsonl` against the sidecar);
    None (and no file) when nothing is new."""
    src = run_dir / METRICS_DIR / METRICS_FILE
    out = run_dir / HISTORY_DIR / METRICS_FILE
    view = ledger_metrics(
        load_metrics(src),
        scope=scope,
        new_configs=new_configs,
        all_configs=all_configs,
        cost_status=cost_status,
        costs_only=costs_only,
    )
    if view is None:
        out.unlink(missing_ok=True)
        return None
    out.parent.mkdir(exist_ok=True)
    out.write_text(metrics_json(view), encoding="utf-8", newline="\n")
    return out


def record(
    ledger: Path, view: Path, *, note: str | None = None, gates: Path | None = None
) -> RunCheck:
    """Append a ledger view (a metrics-1 file) to the ledger, stamped with its `created_at`,
    and judge it against the history before it. Raises `evalkit.ledger.LedgerError` (a view
    already recorded, a broken chain)."""
    created = json.loads(view.read_text(encoding="utf-8"))["created_at"]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    return ek_record_run(
        ledger, view, gates=None if gates is None else str(gates), note=note, at=created
    )


def flags(check: RunCheck) -> list[dict[str, Any]]:
    """The run's warn and alarm rows: metric, labels, status, value, centre and bands."""
    rows = [b.as_dict() for b in check.bands if b.status in FLAGGED]
    keep = ("metric", "labels", "status", "value", "centre", "warn_band", "alarm_band")
    return [{k: r[k] for k in keep} for r in rows]


def default_gates(ledger: Path | None = None) -> Path | None:
    """`monitoring/gates.yaml` of the project the ledger belongs to: beside the `docs/` that
    holds `docs/measurements/runs.jsonl` (the repository root), else in the working folder;
    None when there is none."""
    if ledger is not None and ledger.parent.name == "measurements":
        docs = ledger.parent.parent
        if docs.name == "docs":
            root_gates = docs.parent / GATES
            return root_gates if root_gates.is_file() else None
    return GATES if GATES.is_file() else None


def record_quietly(
    ledger: Path, view: Path | None, *, note: str, gates: Path | None = None
) -> dict[str, Any]:
    """`record` for the runner: never raises; passes the gates file (`default_gates(ledger)`
    when `gates` is None), so a gated accuracy alarms only downward. Returns what to log."""
    if view is None:
        return {"recorded": False, "ledger": str(ledger), "error": None, "reason": "no new answers"}
    gates = gates if gates is not None else default_gates(ledger)
    try:
        check = record(ledger, view, note=note, gates=gates)
    except (LedgerError, OSError, ValueError) as exc:
        return {"recorded": False, "ledger": str(ledger), "error": str(exc)}
    d = check.as_dict()
    return {
        "recorded": True,
        "ledger": str(ledger),
        "gates": None if gates is None else str(gates),
        "rows": len(check.bands),
        "alarms": d["alarms"],
        "warns": d["warns"],
        "learning": d["learning"],
        "statuses": dict(sorted(Counter(b.status for b in check.bands).items())),
        "flagged": flags(check),
    }


def trend(ledger: Path, *, gates: Path | None = None) -> History:
    """The ledger's history, keeping the trend page's three series."""
    hist = history(ledger, gates=None if gates is None else str(gates))
    series = [s for s in hist.series if s.metric in TREND_METRICS]
    spread = [s for s in hist.spread if s.name in TREND_METRICS]
    return replace(hist, series=series, spread=spread)


def trend_page(ledger: Path, out: Path, *, gates: Path | None = None) -> Path:
    """Write EvalKit's trend page for `trend(ledger)` to `out`; the bytes repeat."""
    out.parent.mkdir(parents=True, exist_ok=True)
    page = history_html(trend(ledger, gates=gates), title=TREND_TITLE)
    out.write_text(page, encoding="utf-8", newline="\n")
    return out


def main(argv: list[str] | None = None) -> int:
    """`python -m agentic_analytics.monitoring.history --out trend.html`: the trend page (the
    monitor job's, the same as the dashboard's `trend.html`)."""
    ap = argparse.ArgumentParser(
        prog="python -m agentic_analytics.monitoring.history",
        description="Write the run-history trend page (accuracy, cost per correct, p95 "
        "latency) from EvalKit's run ledger; no calls.",
    )
    ap.add_argument("--ledger", default=str(LEDGER), help=f"default {LEDGER}")
    ap.add_argument("--gates", default=None, help="gates YAML (default: monitoring/gates.yaml)")
    ap.add_argument("--out", required=True, help="the HTML page to write")
    ns = ap.parse_args(argv)
    gates = Path(ns.gates) if ns.gates else default_gates(Path(ns.ledger))
    try:
        trend_page(Path(ns.ledger), Path(ns.out), gates=gates)
    except (LedgerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {ns.out}")
    return 0


__all__ = [
    "COST_METRICS",
    "GATES",
    "HISTORY_DIR",
    "LEDGER",
    "LEDGER_NAME",
    "TREND_METRICS",
    "config_label",
    "flags",
    "ledger_metrics",
    "main",
    "record",
    "record_quietly",
    "scope_of",
    "trend",
    "trend_page",
    "write_ledger_view",
]

if __name__ == "__main__":
    sys.exit(main())
