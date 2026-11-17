"""OWNER D-75: every reported metric names the reader question and trace it answers (§6.3)."""

from __future__ import annotations

import re
from pathlib import Path

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


def test_relevance_rows_name_real_metrics() -> None:
    planned = {"step_efficiency", "drift"}  # M4.1, not yet in the summary schema
    fields = set(BackendSummary.model_fields) | set(RunSummary.model_fields)
    assert set(relevance_rows()) - fields == planned
