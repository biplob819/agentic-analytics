"""MON-06 (and MON-03's record): EvalKit's upstreamed scorers score every recorded answer as
this project's stored scores do, and the answer-from-rows check now runs from EvalKit."""

from __future__ import annotations

import json
from pathlib import Path

import evalkit
import pytest

from agentic_analytics.monitoring import parity
from agentic_analytics.packs import load_pack
from agentic_analytics.questions import load_probes
from agentic_analytics.run.runner import load_run
from agentic_analytics.schemas import Probe, Question, Record, RunSummary
from agentic_analytics.scoring import numbers, score_record
from tests.golden.record import GOLDEN, golden_run
from tests.schemas import factories as f

ROOT = Path(__file__).resolve().parents[2]
MEASUREMENTS = ROOT / "docs" / "measurements"
PARITY = MEASUREMENTS / "evalkit-parity-2026-11-17.json"


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> tuple[Path, list[Record], dict[str, Question], RunSummary]:
    out = golden_run(tmp_path_factory.mktemp("golden"))
    assert out.exit_code == 0 and out.run_dir is not None, out.message
    _, records, questions = load_run(out.run_dir)
    summary = RunSummary.model_validate_json((out.run_dir / "summary.json").read_text("utf-8"))
    return out.run_dir, records, questions, summary


def golden_probes() -> dict[str, Probe]:
    return {p.id: p for p in load_probes(load_pack(GOLDEN / "pack"))}


def test_answer_from_rows_runs_from_evalkit(monkeypatch):
    """`score_record`'s unsupported numbers are EvalKit's, with this pack's year range."""
    seen: dict[str, object] = {}

    def fake(answer, rows, question, *, top_n, year_range, rel_tol):
        seen.update(answer=answer, year_range=year_range, rel_tol=rel_tol, top_n=top_n)
        return ("£999",)

    monkeypatch.setattr(numbers, "evalkit_unsupported_numbers", fake)
    q = f.question()
    a = f.answer(answer="The median was £999.")
    s = score_record(q, a, f.execution(sql=a.sql), f.execution().result)
    assert s.unsupported_numbers == ("£999",) and s.answer_from_rows is False
    assert seen == {
        "answer": "The median was £999.",
        "year_range": numbers.YEAR_RANGE,
        "rel_tol": numbers.REL_TOL,
        "top_n": q.top_n,
    }


def test_the_local_check_is_gone_and_the_reader_stays():
    """The local copy of the check is removed (OWNER D-75); the number reader stays, since the
    injection check reads markers with it and EvalKit keeps its reader internal."""
    for gone in ("_cells", "_matches_cells", "_in_text", "_same_value", "_in_years"):
        assert not hasattr(numbers, gone), gone
    assert [n.value for n in numbers.extract_numbers("£1.2m and 3,400")] == [1_200_000, 3400]


@pytest.mark.golden
def test_evalkit_scores_every_golden_answer_as_stored(golden):
    _, records, questions, summary = golden
    items = {**golden_probes(), **questions}
    assert parity.rows_differences(records, items) == []
    n, clar = parity.clarification_differences(records, questions, summary)
    assert n == len(summary.backends) == 5 and clar == []


def test_parity_catches_planted_differences(golden):
    _, records, questions, summary = golden
    items = {**golden_probes(), **questions}
    rec = next(r for r in records if r.scores.answer_from_rows is True)
    planted = rec.model_copy(
        update={"scores": rec.scores.model_copy(update={"unsupported_numbers": ("£1",)})}
    )
    [row] = parity.rows_differences([planted], items)
    assert row["stored"] == ["£1"] and row["evalkit"] == [] and not row["verdict_differs"]
    flipped = rec.model_copy(
        update={"scores": rec.scores.model_copy(update={"answer_from_rows": False})}
    )
    assert parity.rows_differences([flipped], items)[0]["verdict_differs"]
    b = summary.backends[0]
    off = b.clarification_rate.model_copy(update={"point": b.clarification_rate.point + 0.25})
    bad = summary.model_copy(
        update={"backends": (b.model_copy(update={"clarification_rate": off}),)}
    )
    _, clar = parity.clarification_differences(records, questions, bad)
    assert [c["metric"] for c in clar] == ["clarification_rate"]


def test_the_recorded_parity_covers_every_stored_answer():
    """docs/measurements/evalkit-parity-2026-11-17.json: EvalKit v0.3.0 against every record of
    the first live smoke, the latest baseline rescore and the fix-run rescore (D-92's 5,010
    records), offline. Every list, verdict, rate and probe verdict is identical."""
    m = json.loads(PARITY.read_text("utf-8"))
    assert m["kind"] == "evalkit_parity" and m["node"] == "M4.1" and m["identical"] is True
    assert m["evalkit"] == evalkit.__version__  # a pin move needs a new measurement
    runs = {r["run_id"]: r for r in m["runs"]}
    fixruns = json.loads((MEASUREMENTS / "fixruns-2026-11-17.json").read_text("utf-8"))
    baseline = json.loads((MEASUREMENTS / "baseline-2026-11-17.json").read_text("utf-8"))
    smoke = json.loads((MEASUREMENTS / "smoke-2026-11-17.json").read_text("utf-8"))
    assert fixruns["rescored_run_id"] in runs and smoke["run_id"] in runs
    base = [r for r in runs if r.startswith("rescore-") and r.endswith(baseline["run_id"][-6:])]
    assert len(base) == 1 and runs[base[0]]["records"] == baseline["jobs"] == 1065
    assert sum(r["records"] for r in m["runs"]) == 5010 + 36
    for r in m["runs"]:
        assert len(r["records_sha256"]) == 64
        assert r["answer_from_rows"]["records"] == r["records"]
        assert r["answer_from_rows"]["differing"] == r["answer_from_rows"]["verdicts_differing"]
        assert r["answer_from_rows"]["differing"] == 0 and not r["answer_from_rows"]["differences"]
        assert r["clarification"]["differing"] == 0 and r["clarification"]["configurations"] >= 3
        assert r["probes"]["disagreements"] == 0
        assert sum(r["probes"]["outcomes"].values()) == r["probes"]["records"]
    assert sum(r["probes"]["records"] for r in m["runs"]) == 810


def test_parity_command_measures_a_run_folder(golden, tmp_path, capsys):
    run_dir, *_ = golden
    out = tmp_path / "parity.json"
    code = parity.main(
        ["--results", str(run_dir.parent), "--run", run_dir.name, "--pack", str(GOLDEN / "pack"),
         "--out", str(out)]
    )  # fmt: skip
    assert code == parity.EXIT_OK
    m = json.loads(out.read_text("utf-8"))
    assert m["identical"] and m["runs"][0]["probes"]["records"] == 30
    assert json.loads(capsys.readouterr().out) == m
    assert parity.main(["--results", str(tmp_path), "--run", "nope"]) == parity.EXIT_ERROR
