"""The run's metrics in EvalKit's frozen `metrics-1` format (M4.1 MON-05, OWNER D-75, EvalKit
D-62, EvalKit D-67).

Every run that writes `summary.json` also writes a `metrics/` folder beside it:

- `metrics/metrics.json`: each §6.3 metric of the summary as a `metrics-1` metric, with its interval
  (exactly as the summary has it; `ci_method` says how it was drawn), its unit and, for shares,
  its `n`; plus EvalKit's per-model efficiency block (D-67) where it applies. This is the input
  of `evalkit gate` (MON-01, `monitoring/gates.yaml`) and `evalkit compare` (MON-02), and later
  of EvalKit's run ledger and cross-project index;
- `metrics/cases.jsonl`: the per-question, per-trial verdicts behind `accuracy` and
  `accuracy_strict` (EvalKit's per-case sidecar), so `evalkit compare` pairs two runs on their
  shared questions.

A folder of its own because EvalKit reads a folder that holds `run.json` or `summary.json` as an
EvalKit run (whose `cases.jsonl` holds case definitions): in the run folder itself the sidecar
would be ignored and the summary misread. So:

    evalkit gate results/<run>/metrics/metrics.json --config monitoring/gates.yaml
    evalkit compare results/<old>/metrics results/<new>/metrics

Labels: every per-configuration metric carries `model` (the backend, the system under test)
and `fixes` (`none`, or the fixes joined by `+`); the efficiency block adds `mode: live` and,
for a backend with a model price, `price_date` (the price table's `checked_on`). A gate on
`accuracy` with `labels: {model: "*", fixes: "none"}` therefore checks each backend's no-fix
configuration. Metrics that only exist per category, per rule, per error kind, per fix or per
engine are named apart (`accuracy_by_category`, ...), so a gate on the headline never matches
them by accident (a gate matches every metric that carries the labels it lists).

Names: a summary metric keeps its §6.3 name, except where EvalKit has a standard efficiency
name for it (D-67: `cost_per_correct`, `latency_p50`, `latency_p95`); `SOURCES` maps every name
to the summary field it comes from, or to `efficiency` for the block computed from the records.

What the efficiency block leaves out: `retry_rate` (retries happen inside the model clients and
are not recorded per call, as in EvalKit's own block) and `eval_overhead_cost` (this benchmark
has no LLM judge; scoring re-executes the answer's SQL, whose warehouse time is part of the
answer's own cost). Cortex answers report no tokens, so Cortex gets no token means or
cache-hit share. Nothing here makes a call.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evalkit import __version__ as evalkit_version
from evalkit.metrics import SCHEMA, Metric, MetricsSummary, Provenance
from evalkit.stats import percentile_ms

from agentic_analytics.aggregate import (
    Rates,
    clarification_ci_method,
    correct_strict,
    group_trials,
    kept_ci_method,
    latency_ms,
    records_cost_usd,
)
from agentic_analytics.schemas import Interval, Question, Record, RunSummary

PROJECT = "agentic-analytics"
METRICS_DIR = "metrics"
METRICS_FILE = "metrics.json"
CASES_FILE = "cases.jsonl"
MODE = "live"  # every ask is a live call; nothing is batched
NO_FIXES = "none"

# metrics-1 name -> the summary field it comes from ("efficiency": computed from the records)
SOURCES: dict[str, str] = {
    "accuracy": "accuracy",
    "accuracy_strict": "accuracy_strict",
    "accuracy_by_category": "by_category",
    "accuracy_strict_by_category": "by_category",
    "equivalence_passes": "equivalence_passes",
    "clarification_rate": "clarification_rate",
    "false_clarification_rate": "false_clarification_rate",
    "pass_all_trials": "pass_all_trials",
    "trial_agreement": "trial_agreement",
    "answer_from_rows_violations": "answer_from_rows_violations",
    "policy_violations": "policy_violations",
    "errors": "errors",
    "cost_per_correct": "cost_per_correct_usd",
    "latency_p50": "latency_p50_ms",
    "latency_p95": "latency_p95_ms",
    "step_efficiency.tool_calls_mean": "step_efficiency",
    "step_efficiency.min_tool_calls_mean": "step_efficiency",
    "step_efficiency.excess_mean": "step_efficiency",
    "step_efficiency.at_minimum": "step_efficiency",
    "uplift": "uplifts",
    "uplift_p_value": "uplifts",
    "uplift_separated": "uplifts",
    "probes_passed": "probes",
    "total_cost_usd": "total_cost_usd",
    "total_credits": "total_credits",
    # EvalKit D-67's standard efficiency block, from the records
    "tokens_in_per_case": "efficiency",
    "tokens_out_per_case": "efficiency",
    "tokens_reasoning_per_case": "efficiency",
    "tokens_cached_per_case": "efficiency",
    "cache_hit_share": "efficiency",
    "cost_per_case": "efficiency",
    "latency_p99": "efficiency",
    "error_rate": "efficiency",
    "system_cost": "efficiency",
}


def config_labels(backend: str, fixes: Sequence[str]) -> dict[str, str]:
    return {"model": backend, "fixes": "+".join(fixes) or NO_FIXES}


def _methods(n_resamples: int, seed: int) -> tuple[str, str]:
    tail = f"{n_resamples} resamples, seed {seed}"
    return (
        f"two-level percentile bootstrap over questions and trials, {tail}",
        f"percentile bootstrap over questions, {tail}",
    )


def _share(name: str, iv: Interval, n: int, method: str, labels: Mapping[str, Any]) -> Metric:
    return Metric(
        name=name,
        unit="rate",
        value=iv.point,
        n=n,
        ci_low=iv.lo,
        ci_high=iv.hi,
        ci_method=method,
        labels=dict(labels),
    )


def _plain(name: str, unit: str, value: Any, labels: Mapping[str, Any], **kw: Any) -> Metric:
    return Metric(name=name, unit=unit, value=value, labels=dict(labels), **kw)


def _efficiency(
    flat: Sequence[Record],
    labels: Mapping[str, Any],
    rates: Rates,
    price_date: str | None,
) -> list[Metric]:
    """EvalKit D-67's block for one configuration's benchmark answers."""
    n = len(flat)
    eff = {**labels, "mode": MODE}
    if price_date is not None:
        eff["price_date"] = price_date
    out: list[Metric] = []
    tokens = [r.tokens for r in flat]
    if any(tokens):  # Cortex reports no tokens
        for key, name in (
            ("in", "tokens_in_per_case"),
            ("out", "tokens_out_per_case"),
            ("reasoning", "tokens_reasoning_per_case"),
            ("cached", "tokens_cached_per_case"),
        ):
            mean = math.fsum(float(t.get(key, 0)) for t in tokens) / n
            out.append(_plain(name, "count", mean, eff, n=n))
        tin = sum(t.get("in", 0) for t in tokens)
        cached = sum(t.get("cached", 0) for t in tokens)
        if tin:
            out.append(_plain("cache_hit_share", "rate", cached / tin, eff, n=tin, k=cached))
    total = records_cost_usd(flat, rates)
    out.append(_plain("cost_per_case", "USD", total / n, eff, n=n))
    out.append(_plain("system_cost", "USD", total, eff, n=n))
    lat = [latency_ms(r) for r in flat]
    out.append(_plain("latency_p99", "ms", percentile_ms(lat, 0.99), eff, n=n))
    errored = sum(r.answer.error is not None for r in flat)
    out.append(_plain("error_rate", "rate", errored / n, eff, n=n, k=errored))
    return out


def to_metrics(
    summary: RunSummary,
    records: Iterable[Record],
    questions: Mapping[str, Question],
    rates: Rates,
    *,
    n_resamples: int,
    seed: int,
    code_version: str | None = None,
    price_dates: Mapping[str, str] | None = None,
) -> MetricsSummary:
    """The summary (and the efficiency block from its records) as one `metrics-1` file.

    `price_dates` maps a backend to its model price's `checked_on` date; `n_resamples` and
    `seed` are the run's, named in each interval's `ci_method`."""
    records = list(records)
    groups = group_trials(records, questions)
    two_level, over_questions = _methods(n_resamples, seed)
    dates = price_dates or {}
    metrics: list[Metric] = []
    for b in summary.backends:
        by_q = groups[(b.backend, b.fixes)]
        flat = [r for t in by_q.values() for r in t]
        nq, na = len(by_q), len(flat)
        lab = config_labels(b.backend, b.fixes)
        metrics.append(_share("accuracy", b.accuracy, nq, two_level, lab))
        if b.accuracy_strict is not None:
            metrics.append(_share("accuracy_strict", b.accuracy_strict, nq, two_level, lab))
        for c in b.by_category:
            cl = {**lab, "category": c.category}
            metrics.append(_share("accuracy_by_category", c.accuracy, c.n, two_level, cl))
            if c.accuracy_strict is not None:
                metrics.append(
                    _share("accuracy_strict_by_category", c.accuracy_strict, c.n, two_level, cl)
                )
        for rule, count in sorted(b.equivalence_passes.items()):
            metrics.append(_plain("equivalence_passes", "count", count, {**lab, "rule": rule}))
        n_amb = sum(questions[q].expected == "clarify" for q in by_q)
        n_ans = sum(questions[q].expected == "answer" for q in by_q)
        # MON-09, D-119: EvalKit's method, from the call that drew the summary's intervals
        clustered = kept_ci_method(b, n_resamples, seed) or clarification_ci_method(
            by_q, questions, n_resamples, seed
        )
        if n_amb:
            metrics.append(
                _share("clarification_rate", b.clarification_rate, n_amb, clustered, lab)
            )
        if n_ans:
            metrics.append(
                _share(
                    "false_clarification_rate",
                    b.false_clarification_rate,
                    n_ans,
                    clustered,
                    lab,
                )
            )
        full = sum(len(t) == b.trials for t in by_q.values())
        metrics.append(_share("pass_all_trials", b.pass_all_trials, full, over_questions, lab))
        agree = round(b.trial_agreement * nq)
        metrics.append(_plain("trial_agreement", "rate", agree / nq, lab, n=nq, k=agree))
        for name in ("answer_from_rows_violations", "policy_violations"):
            metrics.append(_plain(name, "count", getattr(b, name), lab, n=na))
        for kind, count in sorted(b.errors.items()):
            metrics.append(_plain("errors", "count", count, {**lab, "kind": kind}, n=na))
        if b.cost_per_correct_usd is not None:
            metrics.append(_plain("cost_per_correct", "USD", b.cost_per_correct_usd, lab))
        metrics.append(_plain("latency_p50", "ms", b.latency_p50_ms, lab, n=na))
        metrics.append(_plain("latency_p95", "ms", b.latency_p95_ms, lab, n=na))
        se = b.step_efficiency
        if se is not None:  # the answers it counts are each metric's n
            n = se.answers
            metrics += [
                _plain("step_efficiency.tool_calls_mean", "ratio", se.tool_calls_mean, lab, n=n),
                _plain(
                    "step_efficiency.min_tool_calls_mean",
                    "ratio",
                    se.min_tool_calls_mean,
                    lab,
                    n=n,
                ),
                _plain("step_efficiency.excess_mean", "ratio", se.excess_mean, lab, n=n),
                _plain("step_efficiency.at_minimum", "rate", se.at_minimum, lab, n=n),
            ]
        metrics += _efficiency(flat, lab, rates, dates.get(b.backend))
    for u in summary.uplifts:
        ul = {"model": u.backend, "fix": u.fix}
        iv = u.delta_points
        metrics.append(
            _plain(
                "uplift",
                "PP",  # percentage points of accuracy
                iv.point,
                ul,
                ci_low=iv.lo,
                ci_high=iv.hi,
                ci_method=f"paired percentile bootstrap over shared questions, "
                f"{n_resamples} resamples, seed {seed}; Holm across fixes",
            )
        )
        metrics.append(_plain("uplift_p_value", "ratio", u.p_value, ul))
        metrics.append(_plain("uplift_separated", "bool", u.separated, ul))
    per_backend: dict[str, list[bool]] = {}
    for per in summary.probes.values():
        for backend, ok in per.items():
            per_backend.setdefault(backend, []).append(ok)
    for backend, oks in per_backend.items():
        k, n = sum(oks), len(oks)
        metrics.append(_plain("probes_passed", "rate", k / n, {"model": backend}, n=n, k=k))
    metrics.append(_plain("total_cost_usd", "USD", summary.total_cost_usd, {}))
    for engine, credits in sorted(summary.total_credits.items()):
        metrics.append(_plain("total_credits", "CREDITS", credits, {"engine": engine}))
    created = summary.finished_at or summary.started_at
    return MetricsSummary(
        schema_version=SCHEMA,
        project=PROJECT,
        run_id=summary.run_id,
        created_at=created.isoformat(),
        simulated=False,
        provenance=Provenance(
            code_version=code_version,
            snapshot=summary.snapshot_sha256,
            config_hash=summary.config_hash,
            evalkit=evalkit_version,
        ),
        metrics=metrics,
    )


def case_rows(records: Iterable[Record], questions: Mapping[str, Question]) -> list[dict[str, Any]]:
    """EvalKit's per-case sidecar for `accuracy` and `accuracy_strict`: one row per question,
    trial and configuration (`case_id` is the question id, never a probe; probes are not in
    the accuracy). The mean of each question's trial means is the summary's point exactly."""
    rows: list[dict[str, Any]] = []
    groups = group_trials(records, questions)
    for (backend, fixes), by_q in groups.items():
        lab = config_labels(backend, fixes)
        strict = all(r.scores.correct_strict is not None for t in by_q.values() for r in t)
        for q in sorted(by_q):
            for r in by_q[q]:
                rows.append(
                    {
                        "case_id": q,
                        "trial": r.trial,
                        "metric": "accuracy",
                        "labels": lab,
                        "value": int(r.scores.correct),
                    }
                )
                if strict:
                    rows.append(
                        {
                            "case_id": q,
                            "trial": r.trial,
                            "metric": "accuracy_strict",
                            "labels": lab,
                            "value": int(correct_strict(r)),
                        }
                    )
    return rows


def metrics_json(metrics: MetricsSummary) -> str:
    data = metrics.model_dump(mode="json")
    data["provenance"] = {k: v for k, v in data["provenance"].items() if v is not None}
    data["metrics"] = [{k: v for k, v in m.items() if v is not None} for m in data["metrics"]]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_metrics(
    run_dir: Path,
    summary: RunSummary,
    records: Iterable[Record],
    questions: Mapping[str, Question],
    rates: Rates,
    *,
    n_resamples: int,
    seed: int,
    code_version: str | None = None,
    price_dates: Mapping[str, str] | None = None,
) -> MetricsSummary:
    """Write `metrics/metrics.json` and `metrics/cases.jsonl` in the run folder, each
    atomically."""
    records = list(records)
    out = Path(run_dir) / METRICS_DIR
    out.mkdir(exist_ok=True)
    m = to_metrics(
        summary,
        records,
        questions,
        rates,
        n_resamples=n_resamples,
        seed=seed,
        code_version=code_version,
        price_dates=price_dates,
    )
    _atomic(out / METRICS_FILE, metrics_json(m))
    lines = "".join(
        json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
        for row in case_rows(records, questions)
    )
    _atomic(out / CASES_FILE, lines)
    return m


def price_dates(models: Mapping[str, str], prices: Mapping[str, Any]) -> dict[str, str]:
    """{backend: the price table's `checked_on` for its model}, for backends with a price."""
    out: dict[str, str] = {}
    for backend, model in sorted(models.items()):
        price = prices.get(model)
        checked = getattr(price, "checked_on", None)
        if checked is not None:
            out[backend] = checked.isoformat()
    return out


__all__ = [
    "CASES_FILE",
    "METRICS_DIR",
    "METRICS_FILE",
    "PROJECT",
    "SOURCES",
    "case_rows",
    "config_labels",
    "metrics_json",
    "price_dates",
    "to_metrics",
    "write_metrics",
]
