"""MON-06 (and MON-03's record): EvalKit's upstreamed scorers score every recorded answer as
this project's stored scores do, and the answer-from-rows check now runs from EvalKit.
V5.1 MON-08 and MON-09: rescoring every stored run through EvalKit 0.4.0's shape equivalences
moves only the verdict OWNER D-118 recorded, and the clustered clarification intervals keep
every point rate."""

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
PARITY_03 = MEASUREMENTS / "evalkit-parity-2026-11-17.json"  # M4.1 on 0.3.0, the history
PARITY = MEASUREMENTS / "evalkit-0.4-parity-2026-11-18.json"  # V5.1 on the current pin


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
    """docs/measurements/evalkit-0.4-parity-2026-11-18.json: EvalKit v0.4.0 against every
    record of the first live smoke, the latest baseline rescore and the fix-run rescore (D-92's
    5,010 records and the smoke's 36), offline. Every answer-from-rows list and verdict, point
    rate and probe verdict is identical; the records are those M4.1 measured on 0.3.0."""
    m = json.loads(PARITY.read_text("utf-8"))
    assert m["kind"] == "evalkit_parity" and m["node"] == "V5.1" and m["identical"] is True
    assert m["evalkit"] == evalkit.__version__  # a pin move needs a new measurement
    runs = {r["run_id"]: r for r in m["runs"]}
    fixruns = json.loads((MEASUREMENTS / "fixruns-2026-11-17.json").read_text("utf-8"))
    baseline = json.loads((MEASUREMENTS / "baseline-2026-11-17.json").read_text("utf-8"))
    smoke = json.loads((MEASUREMENTS / "smoke-2026-11-17.json").read_text("utf-8"))
    assert fixruns["rescored_run_id"] in runs and smoke["run_id"] in runs
    base = [r for r in runs if r.startswith("rescore-") and r.endswith(baseline["run_id"][-6:])]
    assert len(base) == 1 and runs[base[0]]["records"] == baseline["jobs"] == 1065
    assert sum(r["records"] for r in m["runs"]) == 5010 + 36
    old = {r["run_id"]: r for r in json.loads(PARITY_03.read_text("utf-8"))["runs"]}
    assert set(old) == set(runs)
    for r in m["runs"]:
        assert r["records_sha256"] == old[r["run_id"]]["records_sha256"]  # the same records
        assert r["answer_from_rows"]["records"] == r["records"]
        assert r["answer_from_rows"]["differing"] == r["answer_from_rows"]["verdicts_differing"]
        assert r["answer_from_rows"]["differing"] == 0 and not r["answer_from_rows"]["differences"]
        assert r["clarification"]["differing"] == 0 and r["clarification"]["configurations"] >= 3
        assert r["probes"]["disagreements"] == 0
        assert sum(r["probes"]["outcomes"].values()) == r["probes"]["records"]
    assert sum(r["probes"]["records"] for r in m["runs"]) == 810


def test_the_recorded_rescore_moves_only_the_verdict_owner_d118_recorded():
    """MON-08: every stored record rescored through EvalKit's equivalences; one verdict moves
    (OWNER D-118), and the record names it, before and after and its cause."""
    m = json.loads(PARITY.read_text("utf-8"))
    assert m["verdicts_changed"] == 1 and m["unrecorded_changes"] == 0 and m["as_recorded"]
    changes = [c for r in m["runs"] for c in r["verdicts"]["changes"]]
    assert [
        (c["run_id"], c["question_id"], c["backend"], c["fixes"], c["trial"]) for c in changes
    ] == list(parity.RECORDED_CHANGES)
    (c,) = changes
    assert c["before"] == {"correct": True, "correct_strict": False,
                           "equivalences": ["empty_check"], "result_match": True}  # fmt: skip
    assert c["after"] == {"correct": False, "correct_strict": False, "equivalences": [],
                          "result_match": False}  # fmt: skip
    assert c["recorded"] and "count" in c["cause"]
    v = {r["run_id"]: r["verdicts"] for r in m["runs"]}
    assert [v[k]["records"] for k in v] == [r["records"] for r in m["runs"]]
    smoke = json.loads((MEASUREMENTS / "smoke-2026-11-17.json").read_text("utf-8"))["run_id"]
    assert v[smoke]["scored_before_d81"] == 36 and v[smoke]["verdicts_changed"] == 0
    fix = json.loads((MEASUREMENTS / "fixruns-2026-11-17.json").read_text("utf-8"))
    assert v[fix["rescored_run_id"]]["rescued_by"]["empty_check"] == 19  # 20 before D-118
    assert m["local_module_comparison"]["differing_from_evalkit"] == 1


def test_the_recorded_clarification_intervals_keep_every_point():
    """MON-09: EvalKit's clustered interval gives each stored point, sits within the allowed
    lattice steps of `aggregate.two_level_interval`, and is the exact Clopper-Pearson interval
    for one trial per question (the smoke)."""
    m = json.loads(PARITY.read_text("utf-8"))
    assert m["clarification_points_differing"] == 0
    assert m["max_steps_from_two_level"] <= m["lattice_steps_allowed"] == parity.LATTICE_STEPS
    rows = [x for r in m["runs"] for x in r["clarification_intervals"]["intervals"]]
    assert len(rows) == 6 + 6 + 22 and all(x["point_equal"] for x in rows)
    for x in rows:
        ek = x["evalkit"]
        assert ek["lo"] <= ek["point"] <= ek["hi"]
        method = "clopper-pearson" if x["trials"] == 1 else "cluster-bootstrap"
        assert x["method"] == method, x


@pytest.mark.golden
def test_rescoring_the_golden_run_moves_no_verdict(golden):
    _, records, questions, summary = golden
    probes = golden_probes()
    gold = parity.pack_gold(load_pack(GOLDEN / "pack"), questions.values(), probes.values())
    items = {**probes, **questions}
    v = parity.verdict_differences(records, items, gold, run_id="golden", max_tool_calls=6)
    assert v["records"] == len(records) and v["verdicts_changed"] == 0 and v["changes"] == []
    c = parity.clarification_intervals(records, questions, summary, seed=7, n_resamples=2000)
    assert c["points_differing"] == 0 and c["rows"] >= 5
    # the summary now reports EvalKit's interval (D-119), so stored and EvalKit agree exactly
    assert all(x["evalkit"] == x["stored"] for x in c["intervals"])


def test_the_rescore_measurement_repeats_byte_for_byte(golden):
    run_dir, *_ = golden
    probes = golden_probes()
    pack = load_pack(GOLDEN / "pack")
    _, _, questions = load_run(run_dir)
    gold = parity.pack_gold(pack, questions.values(), probes.values())
    a = json.dumps(parity.measure([run_dir], probes, gold), sort_keys=True)
    b = json.dumps(parity.measure([run_dir], probes, gold), sort_keys=True)
    assert a == b


def test_verdict_parity_compares_the_equivalences_too():
    """A stored rescue under another rule is a change even when `correct` agrees."""
    from agentic_analytics.schemas import ResultSet

    gold = ResultSet(columns=("duration", "sales"), rows=(("freehold", 10), ("leasehold", 5)))
    item = f.question(text="How many sales for each tenure?", category="aggregation")
    run = f.execution(
        result=ResultSet(columns=("freehold", "leasehold"), rows=((10, 5),)), rows_total=1
    )
    ans = f.answer(sql=run.sql, answer="See the table.")
    scores = score_record(item, ans, run, gold)
    assert scores.equivalences == ("pivot",) and scores.correct
    planted = scores.model_copy(update={"equivalences": ("percent_scale",)})
    rec = f.record(question_id=item.id, answer=ans, execution=run, scores=planted)
    v = parity.verdict_differences(
        [rec], {item.id: item}, {(item.id, "clickhouse"): gold}, run_id="x", max_tool_calls=6
    )
    assert v["verdicts_changed"] == 1 and v["unrecorded_changes"] == 1
    (c,) = v["changes"]
    assert c["before"]["correct"] == c["after"]["correct"] is True
    assert (c["before"]["equivalences"], c["after"]["equivalences"]) == (
        ["percent_scale"],
        ["pivot"],
    )


def test_an_unrecorded_change_fails_the_command(golden, tmp_path, capsys):
    """`--rescore` exits 1 (and `as_recorded` is false) when a stored verdict moves that
    `RECORDED_CHANGES` does not name."""
    import shutil

    run_dir, records, *_ = golden
    bad = tmp_path / "results" / run_dir.name
    shutil.copytree(run_dir, bad)
    i = next(k for k, r in enumerate(records) if r.scores.correct and r.scores.result_match)
    lines = (bad / "records.jsonl").read_text("utf-8").splitlines()
    r = records[i]
    lines[i] = r.model_copy(
        update={
            "scores": r.scores.model_copy(
                update={"correct": False, "result_match": False, "correct_strict": False}
            )
        }
    ).model_dump_json()
    (bad / "records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "m.json"
    code = parity.main(
        ["--results", str(bad.parent), "--run", bad.name, "--pack", str(GOLDEN / "pack"),
         "--rescore", "--out", str(out)]
    )  # fmt: skip
    m = json.loads(out.read_text("utf-8"))
    assert code == parity.EXIT_DIFFERS and m["as_recorded"] is False
    assert m["verdicts_changed"] == 1 and m["unrecorded_changes"] == 1
    capsys.readouterr()


def test_the_rescore_write_path_regenerates_a_run_offline(golden, tmp_path, capsys):
    """S2: `--rescore --write DIR [--credits JSON]` rescored the stored fix-run copy into the
    published v0.1.1 record (fixruns-evalkit-0.4-2026-11-18.json names the command). On the
    golden run it leaves the source untouched, reproduces the summary a reconcile of an
    untouched copy gives (no verdict moves), and stamps run.json from code."""
    import shutil

    from agentic_analytics.run.runner import reconcile_run

    run_dir, *_ = golden
    before = (run_dir / "records.jsonl").read_bytes()
    credits = '{"snowflake": 0.25, "clickhouse": 0.0}'
    code = parity.main(
        ["--results", str(run_dir.parent), "--run", run_dir.name, "--pack", str(GOLDEN / "pack"),
         "--rescore", "--write", str(tmp_path / "w"), "--credits", credits]
    )  # fmt: skip
    capsys.readouterr()
    assert code == parity.EXIT_OK and (run_dir / "records.jsonl").read_bytes() == before
    copy = tmp_path / "plain" / run_dir.name
    shutil.copytree(run_dir, copy)
    reconcile_run(copy, json.loads(credits), render=False)
    w = tmp_path / "w" / run_dir.name
    assert (w / "summary.json").read_bytes() == (copy / "summary.json").read_bytes()
    stamp = json.loads((w / "run.json").read_text("utf-8"))["rescored_offline"]
    sha = parity._sha256(run_dir / "records.jsonl")
    want = {**parity.RESCORE_STAMP, "evalkit": evalkit.__version__, "stored_records_sha256": sha}
    assert stamp == want
    with pytest.raises(SystemExit):  # --write needs --rescore
        parity.main(["--run", "a", "--write", str(tmp_path)])


def test_the_published_record_names_its_regeneration_command():
    """The full regeneration reads results/ (the stored runs are not committed: about 30 MB
    with traces), so CI checks its command and stamp; the lane ran it and got
    docs/results/ byte for byte."""
    rec = json.loads((MEASUREMENTS / "fixruns-evalkit-0.4-2026-11-18.json").read_text("utf-8"))
    cmds = rec["regenerate"]["commands"]
    assert "agentic_analytics.monitoring.parity --rescore" in cmds[0] and "--write" in cmds[0]
    assert '{"snowflake": 0.313194458, "clickhouse": 0.0}' in cmds[0]
    assert cmds[1].startswith("uv run python scripts/publish_results.py ")
    published = json.loads((ROOT / "docs/results/run.json").read_text("utf-8"))
    assert {k: published["rescored_offline"][k] for k in parity.RESCORE_STAMP} == (
        parity.RESCORE_STAMP
    )


def test_verdict_parity_catches_a_planted_change(golden):
    _, records, questions, _ = golden
    probes = golden_probes()
    gold = parity.pack_gold(load_pack(GOLDEN / "pack"), questions.values(), probes.values())
    rec = next(r for r in records if r.scores.correct and r.scores.result_match)
    planted = rec.model_copy(update={"scores": rec.scores.model_copy(update={"correct": False})})
    items = {**probes, **questions}
    v = parity.verdict_differences([planted], items, gold, run_id="x", max_tool_calls=6)
    assert v["verdicts_changed"] == 1 and v["unrecorded_changes"] == 1
    assert v["changes"][0]["before"]["correct"] is False
    assert v["changes"][0]["after"]["correct"] is True


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
    again = tmp_path / "rescore.json"
    code = parity.main(
        ["--results", str(run_dir.parent), "--run", run_dir.name, "--pack", str(GOLDEN / "pack"),
         "--rescore", "--out", str(again)]
    )  # fmt: skip
    r = json.loads(again.read_text("utf-8"))
    assert code == parity.EXIT_OK and r["as_recorded"] and r["verdicts_changed"] == 0
    capsys.readouterr()
