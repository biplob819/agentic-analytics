"""OWNER D-75: every reported metric names the reader question and trace it answers (§6.3)."""

from __future__ import annotations

import re
from pathlib import Path

from evalkit.metrics import EFFICIENCY_NAMES, load_metrics

from agentic_analytics.monitoring.metrics import SOURCES
from agentic_analytics.schemas.summary import BackendSummary, RunSummary

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "Agentic Analytics Tech Spec.md").read_text(encoding="utf-8")
# what identifies a run rather than measures it
NOT_METRICS = {
    "backend",
    "fixes",
    "trials",  # BackendSummary keys
    "schema_version",
    "run_id",
    "config_hash",
    "code_version",
    "as_of",
    "gold_check_id",
    "pack",
    "gold_mode",
    "backends",
    "cost_status",
    "started_at",
    "finished_at",
    "snapshot_sha256",
}
TRACE = re.compile(r"\b(US\d|NFR\d|[A-H]\d{1,2}|D-\d+|OWNER D-\d+)\b")


def relevance_rows() -> dict[str, tuple[str, str]]:
    section = SPEC[SPEC.index("### 6.3 Metric relevance") : SPEC.index("## 7. Fixes protocol")]
    rows = re.findall(r"^\| `(\w+)`(?: \([^)]*\))? \| (.+?) \| (.+?) \|$", section, re.M)
    return {name: (question, trace) for name, question, trace in rows}


def test_every_summary_metric_names_its_question_and_trace() -> None:
    rows = relevance_rows()
    fields = (set(BackendSummary.model_fields) | set(RunSummary.model_fields)) - NOT_METRICS
    missing = sorted(fields - set(rows))
    assert not missing, f"metrics with no §6.3 relevance row: {missing}"
    for name, (question, trace) in rows.items():
        assert question.endswith("?"), name
        assert TRACE.search(trace), f"{name}: trace {trace!r} names no story, finding or decision"


# V5.1 MON-07 (D-121): `drift` is not a summary field but the run ledger's verdict on every
# metric of a run, its alarm band from the run's own history (EvalKit B5.7), reported by
# `evalkit history`, the monitor's drift step and the trend page
LEDGER_METRICS = {"drift"}


def test_relevance_rows_name_real_metrics() -> None:
    fields = set(BackendSummary.model_fields) | set(RunSummary.model_fields)
    efficiency = {n for n, src in SOURCES.items() if src == "efficiency"}
    assert set(relevance_rows()) - fields - efficiency == LEDGER_METRICS


def test_drift_is_the_ledgers_band_verdict() -> None:
    from agentic_analytics.monitoring.history import FLAGGED, TREND_METRICS

    assert FLAGGED == ("warn", "alarm")
    question, trace = relevance_rows()["drift"]
    assert "band" in question and "MON-07" not in trace and "D-121" in trace
    assert set(TREND_METRICS) <= set(SOURCES)  # the trend page draws exported metrics


def test_every_metrics_1_metric_names_its_question_and_trace() -> None:
    """M4.1 MON-05: each metric of the metrics-1 export comes from a summary field with a §6.3
    row, or is one of EvalKit's efficiency names with a row of its own."""
    rows = relevance_rows()
    exported = {
        m.name for m in load_metrics(ROOT / "tests" / "golden" / "metrics" / "metrics.json").metrics
    }
    assert exported <= set(SOURCES)
    for name in sorted(exported):
        src = SOURCES[name]
        key = name if src == "efficiency" else src
        assert key in rows, f"metrics-1 {name}: no §6.3 row for {key}"
    efficiency = {n for n, src in SOURCES.items() if src == "efficiency"}
    assert efficiency <= set(EFFICIENCY_NAMES)  # EvalKit's standard names (D-67)
