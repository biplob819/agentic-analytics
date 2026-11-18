"""Parity of EvalKit's scorers with this project's stored scores (M4.1 MON-03, MON-06;
V5.1 MON-08, MON-09; OWNER D-75, D-2, OWNER D-118, D-119).

Reads finished run folders (records, questions, summary) and the pack's gold, and makes no
call. For each run:

- **verdicts** (MON-08): every record rescored offline through `score_record`, whose tolerant
  match now runs EvalKit's shape equivalences (`scoring.tolerant`), against the stored
  `correct`, `correct_strict`, `equivalences` and `result_match`. A record scored before D-81
  holds no tolerant verdict: its stored `correct` is compared with the strict verdict, and a
  rescue it could not hold is counted apart. Every difference is listed; OWNER D-118 recorded
  the one EvalKit 0.4.0 makes (`RECORDED_CHANGES`);
- **clarification intervals** (MON-09): EvalKit's clustered `clarification_rates` (the
  summary's interval since D-119) beside the stored summary's interval (a percentile
  bootstrap over per-question shares, before D-119) and `aggregate.two_level_interval` on the
  same trials, seed and resamples, per configuration and kind;

- **answer from rows** (MON-06): EvalKit's `scorers.unsupported_numbers`, the check
  `scoring.numbers` now runs, against every record's stored `unsupported_numbers` and
  `answer_from_rows` verdict;
- **clarification** (MON-06): EvalKit's `scorers.clarification_rates` per configuration, over
  every benchmark answer, against the stored summary's `clarification_rate` and
  `false_clarification_rate` points (the summary keeps its bootstrap-over-questions intervals,
  OWNER D-100);
- **probes** (MON-03): EvalKit's `scorers.attack_outcome` verdict (`monitoring.probes`) against
  S2.1's stored verdict on every probe answer. S2.1's verdict is the one the summary reports
  (D-99); EvalKit's is the cross-check.

    python -m agentic_analytics.monitoring.parity --run RUN_ID [--run RUN_ID ...] \\
        [--results results] [--out docs/measurements/evalkit-parity-<date>.json]

prints the measurement (and writes it with `--out`); exit 1 when any answer differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evalkit import __version__ as evalkit_version
from evalkit.scorers import clarification_rates
from evalkit.scorers import unsupported_numbers as evalkit_unsupported_numbers

from agentic_analytics.aggregate import (
    Bootstrap,
    evalkit_clarification,
    group_trials,
    two_level_interval,
)
from agentic_analytics.monitoring.probes import probe_parity
from agentic_analytics.schemas import (
    ENGINE_OF,
    Interval,
    Probe,
    Question,
    Record,
    ResultSet,
    RunSummary,
)
from agentic_analytics.scoring import score_record
from agentic_analytics.scoring.numbers import REL_TOL, YEAR_RANGE

EXIT_OK, EXIT_DIFFERS, EXIT_ERROR = 0, 1, 2
VERDICT_FIELDS = ("correct", "correct_strict", "equivalences", "result_match")
# OWNER D-118: the verdicts EvalKit 0.4.0's stricter rules move, by (run id, question, backend,
# fixes, trial); every other stored verdict must come back unchanged
RECORDED_CHANGES: dict[tuple[str, str, str, str, int], str] = {
    ("rescore-20261117T122217Z-6ccebe", "q-020", "gpt_mcp", "descriptions", 1): (
        "EvalKit 0.4.0 empty_check accepts a no-data zero only in a count column (a name with "
        "a count word such as count, n, sales or rows); the answer's zeros sit under property "
        "type labels (detached, semi_detached, terraced, flat, other), so it is not rescued"
    ),
}
# MON-09: EvalKit's clustered interval sits within this many lattice steps (1 / (trials x
# questions)) of `aggregate.two_level_interval` on the same trials, seed and resamples
LATTICE_STEPS = 2


def rows_differences(
    records: Iterable[Record],
    items: Mapping[str, Question | Probe],
    *,
    year_range: tuple[int, int] = YEAR_RANGE,
) -> list[dict[str, Any]]:
    """One row per record whose stored unsupported numbers differ from EvalKit's."""
    out: list[dict[str, Any]] = []
    for r in records:
        item = items[r.question_id]
        ex = r.execution
        executed = ex is not None and ex.error is None
        got = evalkit_unsupported_numbers(
            r.answer.answer,
            ex.result if executed and ex is not None else None,
            item.text,
            top_n=item.top_n if isinstance(item, Question) else None,
            year_range=year_range,
            rel_tol=REL_TOL,
        )
        stored = tuple(r.scores.unsupported_numbers)
        verdict = (not got) if executed else None
        if got != stored or verdict != r.scores.answer_from_rows:
            out.append(
                {
                    "question_id": r.question_id,
                    "backend": r.backend,
                    "fixes": list(r.fixes),
                    "trial": r.trial,
                    "stored": list(stored),
                    "evalkit": list(got),
                    "verdict_differs": verdict != r.scores.answer_from_rows,
                }
            )
    return out


def clarification_differences(
    records: Iterable[Record], questions: Mapping[str, Question], summary: RunSummary
) -> tuple[int, list[dict[str, Any]]]:
    """(configurations checked, those whose EvalKit point rates differ from the summary's)."""
    groups = group_trials(records, questions)
    out: list[dict[str, Any]] = []
    for b in summary.backends:
        by_q = groups[(b.backend, b.fixes)]
        expected: list[bool] = []
        asked: list[bool] = []
        for q, trials in by_q.items():
            if questions[q].expected not in ("clarify", "answer"):
                continue
            for r in trials:
                expected.append(questions[q].expected == "clarify")
                asked.append(r.scores.clarified)
        ek = clarification_rates(expected, asked)
        pairs = (
            ("clarification_rate", ek.ask_rate, b.clarification_rate),
            ("false_clarification_rate", ek.false_clarification_rate, b.false_clarification_rate),
        )
        for name, got, stored in pairs:
            point = 0.0 if got is None else got.point
            if not math.isclose(point, stored.point, rel_tol=1e-12, abs_tol=1e-12):
                out.append(
                    {
                        "backend": b.backend,
                        "fixes": list(b.fixes),
                        "metric": name,
                        "summary": stored.point,
                        "evalkit": point,
                    }
                )
    return len(summary.backends), out


def _fixes(fixes: Sequence[str]) -> str:
    return "+".join(fixes) or "none"


def verdict_differences(
    records: Iterable[Record],
    items: Mapping[str, Question | Probe],
    gold: Mapping[tuple[str, str], ResultSet],
    *,
    run_id: str,
    max_tool_calls: int,
) -> dict[str, Any]:
    """MON-08: every record rescored offline (EvalKit's equivalences) against its stored
    verdicts; the differences, and those OWNER D-118 recorded."""
    diffs: list[dict[str, Any]] = []
    checked = pre_d81 = rescued_unstored = text_changed = 0
    rescued: dict[str, int] = {}
    for r in records:
        item = items[r.question_id]
        g = gold.get((r.question_id, ENGINE_OF[r.backend]))
        s = score_record(item, r.answer, r.execution, g, max_tool_calls=max_tool_calls)
        checked += 1
        if s.equivalences:
            k = "+".join(s.equivalences)
            rescued[k] = rescued.get(k, 0) + 1
        if s.mismatch_reason != r.scores.mismatch_reason and s.correct == r.scores.correct:
            text_changed += 1
        if r.scores.correct_strict is None:  # scored before D-81: the strict verdict only
            pre_d81 += 1
            rescued_unstored += bool(s.equivalences)
            before = {"correct_strict": r.scores.correct}
            after = {"correct_strict": s.correct_strict}
        else:
            before = {f: _json(getattr(r.scores, f)) for f in VERDICT_FIELDS}
            after = {f: _json(getattr(s, f)) for f in VERDICT_FIELDS}
        if before != after:
            key = (run_id, r.question_id, r.backend, _fixes(r.fixes), r.trial)
            diffs.append(
                {
                    "run_id": run_id,
                    "question_id": r.question_id,
                    "backend": r.backend,
                    "fixes": _fixes(r.fixes),
                    "trial": r.trial,
                    "before": before,
                    "after": after,
                    "recorded": key in RECORDED_CHANGES,
                    "cause": RECORDED_CHANGES.get(key),
                }
            )
    return {
        "records": checked,
        "scored_before_d81": pre_d81,
        "rescued_not_stored_before_d81": rescued_unstored,
        "rescued_by": dict(sorted(rescued.items())),
        "mismatch_reason_text_changed": text_changed,
        "verdicts_changed": len(diffs),
        "unrecorded_changes": sum(not d["recorded"] for d in diffs),
        "changes": diffs,
    }


def _json(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def _iv(iv: Interval) -> dict[str, float]:
    return {"point": round(iv.point, 6), "lo": round(iv.lo, 6), "hi": round(iv.hi, 6)}


def clarification_intervals(
    records: Iterable[Record],
    questions: Mapping[str, Question],
    summary: RunSummary,
    *,
    seed: int,
    n_resamples: int,
) -> dict[str, Any]:
    """MON-09: per configuration and kind, EvalKit's clustered interval beside the stored one
    and `aggregate.two_level_interval`; points must equal the stored points."""
    groups = group_trials(records, questions)
    boot = Bootstrap(n_resamples, seed)
    rows: list[dict[str, Any]] = []
    for b in summary.backends:
        by_q = groups[(b.backend, b.fixes)]
        ek = evalkit_clarification(by_q, questions, boot)
        kinds = (
            ("clarification_rate", "clarify", ek.ask_rate, b.clarification_rate),
            ("false_clarification_rate", "answer", ek.false_clarification_rate,
             b.false_clarification_rate),
        )  # fmt: skip
        for name, expected, new, stored in kinds:
            ids = [q for q in by_q if questions[q].expected == expected]
            if not ids or new is None:
                continue
            vals = [[1.0 if r.scores.clarified else 0.0 for r in by_q[q]] for q in ids]
            k = max(len(v) for v in vals)
            step = 1 / (k * len(ids))
            two = two_level_interval(vals, boot, f"{b.backend}:{_fixes(b.fixes)}:{name}")
            dist = max(abs(two.lo - new.lo), abs(two.hi - new.hi)) / step
            rows.append(
                {
                    "backend": b.backend,
                    "fixes": _fixes(b.fixes),
                    "metric": name,
                    "questions": len(ids),
                    "trials": k,
                    "method": ek.method,
                    "point_equal": math.isclose(new.point, stored.point, abs_tol=1e-12),
                    "evalkit": _iv(new),
                    "stored": _iv(stored),
                    "two_level": _iv(two),
                    "steps_from_two_level": round(dist, 3),
                }
            )
    clustered = [r for r in rows if r["method"] == "cluster-bootstrap"]
    return {
        "rows": len(rows),
        "points_differing": sum(not r["point_equal"] for r in rows),
        "max_steps_from_two_level": max((r["steps_from_two_level"] for r in clustered), default=0),
        "wider_than_stored": sum(
            r["evalkit"]["hi"] - r["evalkit"]["lo"] > r["stored"]["hi"] - r["stored"]["lo"] + 1e-9
            for r in rows
        ),
        "narrower_than_stored": sum(
            r["evalkit"]["hi"] - r["evalkit"]["lo"] < r["stored"]["hi"] - r["stored"]["lo"] - 1e-9
            for r in rows
        ),
        "intervals": rows,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure_run(
    run_dir: Path,
    probes: Mapping[str, Probe],
    gold: Mapping[tuple[str, str], ResultSet] | None = None,
) -> dict[str, Any]:
    """One run's parity; `gold` (question or probe id, engine) adds MON-08 and MON-09."""
    from agentic_analytics.run.runner import load_run

    manifest, records, questions = load_run(run_dir)
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    items: dict[str, Question | Probe] = {**probes, **questions}
    rows = rows_differences(records, items)
    n_configs, clar = clarification_differences(records, questions, summary)
    probe_rows = probe_parity(records, probes)
    outcomes: dict[str, int] = {}
    for p in probe_rows:
        outcomes[p.outcome] = outcomes.get(p.outcome, 0) + 1
    extra: dict[str, Any] = {}
    if gold is not None:
        extra["verdicts"] = verdict_differences(
            records,
            items,
            gold,
            run_id=manifest["run_id"],
            max_tool_calls=manifest["config"]["max_tool_calls"],
        )
        extra["clarification_intervals"] = clarification_intervals(
            records, questions, summary, seed=manifest["seed"], n_resamples=manifest["n_resamples"]
        )
    return {
        "run_id": manifest["run_id"],
        "kind": manifest.get("kind"),
        "records": len(records),
        "records_sha256": _sha256(run_dir / "records.jsonl"),
        "answer_from_rows": {
            "records": len(records),
            "executed": sum(r.scores.answer_from_rows is not None for r in records),
            "differing": len(rows),
            "verdicts_differing": sum(d["verdict_differs"] for d in rows),
            "differences": rows,
        },
        "clarification": {
            "configurations": n_configs,
            "differing": len(clar),
            "differences": clar,
        },
        "probes": {
            "records": len(probe_rows),
            "disagreements": sum(not p.agrees for p in probe_rows),
            "outcomes": dict(sorted(outcomes.items())),
            "differences": [p.to_dict() for p in probe_rows if not p.agrees],
        },
        **extra,
    }


def measure(
    run_dirs: Sequence[Path],
    probes: Mapping[str, Probe],
    gold: Mapping[tuple[str, str], ResultSet] | None = None,
) -> dict[str, Any]:
    runs = [measure_run(d, probes, gold) for d in run_dirs]
    identical = all(
        r["answer_from_rows"]["differing"] == 0
        and r["clarification"]["differing"] == 0
        and r["probes"]["disagreements"] == 0
        for r in runs
    )
    out: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "evalkit_parity",
        "node": "M4.1" if gold is None else "V5.1",
        "evalkit": evalkit_version,
        "year_range": list(YEAR_RANGE),
        "runs": runs,
        "identical": identical,
    }
    if gold is not None:
        out["verdicts_changed"] = sum(r["verdicts"]["verdicts_changed"] for r in runs)
        out["unrecorded_changes"] = sum(r["verdicts"]["unrecorded_changes"] for r in runs)
        out["clarification_points_differing"] = sum(
            r["clarification_intervals"]["points_differing"] for r in runs
        )
        out["max_steps_from_two_level"] = max(
            r["clarification_intervals"]["max_steps_from_two_level"] for r in runs
        )
        out["as_recorded"] = (
            identical
            and out["unrecorded_changes"] == 0
            and out["clarification_points_differing"] == 0
            and out["max_steps_from_two_level"] <= LATTICE_STEPS
        )
    return out


def pack_gold(pack: Any, questions: Iterable[Question], probes: Iterable[Probe]) -> dict:
    """The pack's stored gold results by (question or probe id, engine), on every engine."""
    from agentic_analytics.run.gold import load_gold_results, load_probe_gold

    engines = ("clickhouse", "snowflake")
    out = {k: g.result for k, g in load_gold_results(pack, questions, engines).items()}
    out.update({k: g.result for k, g in load_probe_gold(pack, probes, engines).items()})
    return out


RESCORE_STAMP = {
    "node": "V5.1",
    "decision": "OWNER D-118",
    "note": (
        "records rescored with no calls through EvalKit 0.4.0's shape equivalences (MON-08) "
        "and summarised with its clustered clarification intervals (MON-09, D-119), then "
        "reconciled with the baseline's metered credits"
    ),
}


def rescore_copy(
    run_dir: Path,
    out_root: Path,
    items: Mapping[str, Question | Probe],
    gold: Mapping[tuple[str, str], ResultSet],
    *,
    credits: Mapping[str, float] | None = None,
) -> Path:
    """S2 (V5.1): a stored run rescored offline into `out_root/<run id>`, the stored folder
    left as it is: every record's scores recomputed (`score_record`), the summary and the
    metrics-1 export rebuilt, reconciled with `credits` when given (as `docs/results` was), and
    run.json stamped `rescored_offline` (this stamp, the EvalKit version and the stored
    records' sha256). The published v0.1.1 record is this, then `publish_results.py`."""
    import shutil

    from agentic_analytics.aggregate import write_summary
    from agentic_analytics.run.runner import (
        _write_json,
        aggregate_rates,
        credit_rates,
        load_run,
        model_prices,
        reconcile_run,
        summarise,
        write_run_metrics,
    )

    dst = out_root / run_dir.name
    if dst.exists():
        raise ValueError(f"{dst} exists: rescore into an empty folder")
    stored = _sha256(run_dir / "records.jsonl")
    shutil.copytree(run_dir, dst)
    manifest, records, questions = load_run(dst)
    mtc = manifest["config"]["max_tool_calls"]
    rescored = []
    for r in records:
        g = gold.get((r.question_id, ENGINE_OF[r.backend]))
        s = score_record(items[r.question_id], r.answer, r.execution, g, max_tool_calls=mtc)
        rescored.append(r.model_copy(update={"scores": s}))
    text = "".join(r.model_dump_json() + "\n" for r in rescored)
    (dst / "records.jsonl").write_text(text, encoding="utf-8", newline="\n")
    if credits is not None:
        reconcile_run(dst, dict(credits), render=False)
    else:
        rates = aggregate_rates(credit_rates())
        summary = summarise(rescored, questions, manifest, rates)
        write_summary(dst / "summary.json", summary)
        write_run_metrics(dst, summary, rescored, questions, manifest, rates, model_prices())
    manifest = json.loads((dst / "run.json").read_text(encoding="utf-8"))
    manifest["rescored_offline"] = {
        **RESCORE_STAMP,
        "evalkit": evalkit_version,
        "stored_records_sha256": stored,
    }
    _write_json(dst / "run.json", manifest)
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m agentic_analytics.monitoring.parity",
        description=(
            "EvalKit scorer parity on finished runs (M4.1 MON-03, MON-06; with --rescore also "
            "V5.1 MON-08 verdicts and MON-09 clarification intervals); no calls."
        ),
    )
    ap.add_argument("--run", action="append", required=True, help="run id (repeatable)")
    ap.add_argument("--results", default="results", help="results folder (default: results)")
    ap.add_argument("--pack", default="uk_price_paid", help="pack holding the probes")
    ap.add_argument("--out", default=None, help="write the measurement here")
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="also rescore every record against the pack's gold (MON-08) and compare the "
        "clarification intervals (MON-09)",
    )
    ap.add_argument(
        "--write",
        default=None,
        metavar="DIR",
        help="with --rescore and one --run: also write the rescored run into DIR/<run id> "
        "(the stored folder is not touched)",
    )
    ap.add_argument(
        "--credits",
        default=None,
        metavar="JSON",
        help="with --write: reconcile the rescored copy with these metered credits, e.g. "
        '\'{"snowflake": 0.313194458, "clickhouse": 0.0}\'',
    )
    ns = ap.parse_args(argv)
    if ns.write and (not ns.rescore or len(ns.run) != 1):
        ap.error("--write needs --rescore and exactly one --run")
    if ns.credits and not ns.write:
        ap.error("--credits needs --write")
    from agentic_analytics.packs import load_pack
    from agentic_analytics.questions import load_probes
    from agentic_analytics.run.gold import PreconditionError
    from agentic_analytics.run.runner import load_run, resolve_run

    try:
        pack = load_pack(ns.pack)
        probes = {p.id: p for p in load_probes(pack)}
        dirs = [resolve_run(Path(ns.results), r) for r in ns.run]
        gold = None
        if ns.rescore:
            qs: dict[str, Question] = {}
            for d in dirs:
                qs.update(load_run(d)[2])
            gold = pack_gold(pack, qs.values(), probes.values())
        result = measure(dirs, probes, gold)
        if ns.write and gold is not None:
            credits = json.loads(ns.credits) if ns.credits else None
            items = {**probes, **load_run(dirs[0])[2]}
            written = rescore_copy(dirs[0], Path(ns.write), items, gold, credits=credits)
            print(f"wrote {written}", file=sys.stderr)
    except (OSError, ValueError, PreconditionError) as exc:  # RunConfigError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if ns.out:
        Path(ns.out).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    ok = result["as_recorded"] if ns.rescore else result["identical"]
    return EXIT_OK if ok else EXIT_DIFFERS


if __name__ == "__main__":
    sys.exit(main())
