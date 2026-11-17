"""Parity of EvalKit's scorers with this project's stored scores (M4.1 MON-03, MON-06;
OWNER D-75, D-2).

Reads finished run folders (records, questions, summary) and makes no call. For each run:

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

from agentic_analytics.aggregate import group_trials
from agentic_analytics.monitoring.probes import probe_parity
from agentic_analytics.schemas import Probe, Question, Record, RunSummary
from agentic_analytics.scoring.numbers import REL_TOL, YEAR_RANGE

EXIT_OK, EXIT_DIFFERS, EXIT_ERROR = 0, 1, 2


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure_run(run_dir: Path, probes: Mapping[str, Probe]) -> dict[str, Any]:
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
    }


def measure(run_dirs: Sequence[Path], probes: Mapping[str, Probe]) -> dict[str, Any]:
    runs = [measure_run(d, probes) for d in run_dirs]
    return {
        "schema_version": "1.0",
        "kind": "evalkit_parity",
        "node": "M4.1",
        "evalkit": evalkit_version,
        "year_range": list(YEAR_RANGE),
        "runs": runs,
        "identical": all(
            r["answer_from_rows"]["differing"] == 0
            and r["clarification"]["differing"] == 0
            and r["probes"]["disagreements"] == 0
            for r in runs
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m agentic_analytics.monitoring.parity",
        description="EvalKit scorer parity on finished runs (M4.1 MON-03, MON-06); no calls.",
    )
    ap.add_argument("--run", action="append", required=True, help="run id (repeatable)")
    ap.add_argument("--results", default="results", help="results folder (default: results)")
    ap.add_argument("--pack", default="uk_price_paid", help="pack holding the probes")
    ap.add_argument("--out", default=None, help="write the measurement here")
    ns = ap.parse_args(argv)
    from agentic_analytics.packs import load_pack
    from agentic_analytics.questions import load_probes
    from agentic_analytics.run.runner import resolve_run

    try:
        probes = {p.id: p for p in load_probes(load_pack(ns.pack))}
        dirs = [resolve_run(Path(ns.results), r) for r in ns.run]
        result = measure(dirs, probes)
    except (OSError, ValueError) as exc:  # RunConfigError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if ns.out:
        Path(ns.out).write_text(text, encoding="utf-8")
    print(text, end="")
    return EXIT_OK if result["identical"] else EXIT_DIFFERS


if __name__ == "__main__":
    sys.exit(main())
