"""V5.1 MON-07: run history, the trend page and drift flags through EvalKit's run ledger
(EvalKit B5.7; OWNER D-74, OWNER D-98, OWNER D-115, D-121).

A band point is one distinct set of answers, compared only with comparable runs: a run
appends a ledger view (`history/metrics.json`) of the configurations whose answers it asked,
every row labelled `scope` (smoke or full) and every cost row `cost_status` (estimated by the
run, reconciled by a reconcile, which appends nothing else). Nothing here makes a call.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evalkit.ledger import LedgerError, check_run, history, verify_ledger
from evalkit.metrics import load_metrics

from agentic_analytics.monitoring import history as hist
from agentic_analytics.monitoring.metrics import METRICS_DIR, METRICS_FILE
from agentic_analytics.run.runner import reconcile_run
from secret_scan import scan
from tests.golden.record import GOLDEN, golden_run

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs" / "measurements" / "runs.jsonl"
RECORD = ROOT / "docs" / "results"
GATES = ROOT / "monitoring" / "gates.yaml"
CLAUDE = {"model": "claude_mcp", "fixes": "none"}
GOLDEN_CONFIGS = [
    ("claude_mcp", "none"),
    ("claude_mcp", "examples"),
    ("gpt_mcp", "none"),
    ("gpt_mcp", "examples"),
    ("cortex_analyst", "none"),
]
CREDITS = {"snowflake": 0.5, "clickhouse": 0.0}


def _run(dst: Path, run_id: str, day: int, accuracy: float | None = None) -> Path:
    """A run folder holding the golden metrics-1 export, renamed, re-dated and, with
    `accuracy`, with claude_mcp's no-fix accuracy (and its interval) moved to that value."""
    m = json.loads((GOLDEN / METRICS_DIR / METRICS_FILE).read_text("utf-8"))
    m["run_id"] = run_id
    m["created_at"] = f"2026-12-{day:02d}T06:17:00+00:00"
    if accuracy is not None:
        for row in m["metrics"]:
            if row["name"] == "accuracy" and row["labels"] == CLAUDE:
                lo, hi = max(0.0, accuracy - 0.02), min(1.0, accuracy + 0.02)
                row.update(value=accuracy, ci_low=lo, ci_high=hi)
    run = dst / run_id
    (run / METRICS_DIR).mkdir(parents=True)
    (run / METRICS_DIR / METRICS_FILE).write_text(json.dumps(m), encoding="utf-8")
    return run  # no per-case sidecar: the moved accuracy would contradict it


def _view(
    run: Path,
    *,
    scope: hist.Scope = "full",
    status: hist.CostStatus = "estimated",
    new=GOLDEN_CONFIGS,
    costs_only=False,
):
    view = hist.write_ledger_view(
        run,
        scope=scope,
        new_configs=new,
        all_configs=GOLDEN_CONFIGS,
        cost_status=status,
        costs_only=costs_only,
    )
    assert view is not None
    return view


def _band(check, metric="accuracy", **labels):
    want = {**CLAUDE, **labels}
    (b,) = [x for x in check.bands if x.metric == metric and dict(x.labels) == want]
    return b


def _rows(ledger: Path) -> list[dict]:
    return [json.loads(x) for x in ledger.read_text("utf-8").splitlines()]


def _names(entry: dict, name: str) -> list[dict]:
    return [m for m in entry["metrics"] if m["name"] == name]


# logical


def test_a_run_outside_its_alarm_band_is_flagged(tmp_path):
    """Five steady runs build the band; a sixth far below it alarms (gated: low side), one
    inside it is ok; with fewer than five the row is learning and never flagged."""
    ledger = tmp_path / "runs.jsonl"
    for i, acc in enumerate((0.70, 0.72, 0.71, 0.69, 0.70), start=1):
        c = hist.record(ledger, _view(_run(tmp_path, f"r-{i}", i, acc)), gates=GATES)
        assert _band(c, scope="full").status == "learning" and not hist.flags(c)
    ok = check_run(ledger, _view(_run(tmp_path, "r-ok", 6, 0.71)), gates=GATES)
    assert _band(ok, scope="full").status == "ok" and ok.exit_code == 0
    low = hist.record(ledger, _view(_run(tmp_path, "r-low", 7, 0.30)), gates=GATES)
    assert _band(low, scope="full").status == "alarm" and low.exit_code == 1
    (f,) = hist.flags(low)
    assert f["metric"] == "accuracy" and f["status"] == "alarm"
    assert f["labels"] == {**CLAUDE, "scope": "full"}


def test_the_runner_passes_the_gates_so_a_gated_rise_is_improved(tmp_path):
    """S1: `record_quietly` passes monitoring/gates.yaml, so a gated accuracy alarms only
    downward: 0.70 five times, then 0.99, reads `improved`; without the gates it alarms."""
    with_gates, without = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for i in range(1, 6):
        for led in (with_gates, without):
            hist.record(led, _view(_run(tmp_path / led.stem, f"r-{i}", i, 0.70)))
    high = _view(_run(tmp_path, "r-high", 6, 0.99))
    got = hist.record_quietly(with_gates, high, note="run", gates=GATES)
    assert got["recorded"] and got["gates"] == str(GATES) and got["alarms"] == 0
    assert got["statuses"].get("improved", 0) >= 1 and not got["flagged"]
    plain = hist.record(without, high)
    assert _band(plain, scope="full").status == "alarm"


def test_a_cache_only_configuration_adds_no_point():
    """Only the configurations a run asked are in its view; rows of the whole run (cost
    total, credits, probes) only when every configuration is new; an uplift with its fix."""
    m = load_metrics(GOLDEN / METRICS_DIR / METRICS_FILE)
    new = [("claude_mcp", "examples"), ("gpt_mcp", "examples")]
    view = hist.ledger_metrics(
        m, scope="full", new_configs=new, all_configs=GOLDEN_CONFIGS, cost_status="estimated"
    )
    assert view is not None
    acc = {(r.labels["model"], r.labels["fixes"]) for r in view.metrics if r.name == "accuracy"}
    assert acc == set(new)
    names = {r.name for r in view.metrics}
    assert not names & {"total_cost_usd", "total_credits", "probes_passed"}
    assert {(r.labels["model"], r.labels["fix"]) for r in view.metrics if r.name == "uplift"} == {
        ("claude_mcp", "examples"),
        ("gpt_mcp", "examples"),
    }
    assert all(r.labels["scope"] == "full" for r in view.metrics)
    none = hist.ledger_metrics(
        m, scope="full", new_configs=[], all_configs=GOLDEN_CONFIGS, cost_status="estimated"
    )
    assert none is None


# contract


def test_run_and_reconcile_add_one_accuracy_point_per_new_configuration(tmp_path):
    """The runner appends the view of the configurations it asked (every golden one), its
    costs estimated; the reconcile appends the reconciled cost rows only, never accuracy; a
    re-run answered from the cache appends nothing."""
    ledger = tmp_path / "docs" / "measurements" / "runs.jsonl"
    (tmp_path / "monitoring").mkdir()
    (tmp_path / "monitoring" / "gates.yaml").write_bytes(GATES.read_bytes())
    out = golden_run(tmp_path / "a", history_path=ledger)
    assert out.exit_code == 0 and out.run_dir is not None, out.message
    plan = json.loads((out.run_dir / "run.json").read_text("utf-8"))["history"]
    assert plan["scope"] == "full" and sorted(map(tuple, plan["new_configs"])) == sorted(
        GOLDEN_CONFIGS
    )
    reconcile_run(out.run_dir, CREDITS, render=False, history_path=ledger, history_gates=GATES)
    run, rec = _rows(ledger)
    assert (run["note"], rec["note"]) == ("run", "reconcile")
    acc = _names(run, "accuracy")
    assert sorted((m["labels"]["model"], m["labels"]["fixes"]) for m in acc) == sorted(
        GOLDEN_CONFIGS
    )
    assert not _names(rec, "accuracy") and rec["metrics"]
    assert {m["name"] for m in rec["metrics"]} <= hist.COST_METRICS
    assert {m["labels"]["cost_status"] for m in rec["metrics"]} == {"reconciled"}
    costs = [m for m in run["metrics"] if m["name"] in hist.COST_METRICS]
    assert costs and {m["labels"]["cost_status"] for m in costs} == {"estimated"}
    assert all(m["labels"]["scope"] == "full" for e in (run, rec) for m in e["metrics"])
    created = datetime.fromisoformat(
        load_metrics(out.run_dir / METRICS_DIR / METRICS_FILE).created_at
    )
    stamp = created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run["at"] == rec["at"] == stamp  # stamped with the export's created_at
    log = [json.loads(x) for x in (out.run_dir / "run.log").read_text("utf-8").splitlines()]
    (h, hr) = [x for x in log if x["event"] == "history"]
    # the gates file beside the ledger's docs/ (the project root), not the working folder's
    assert (
        h["recorded"] and hr["recorded"] and h["gates"] == str(tmp_path / "monitoring/gates.yaml")
    )
    assert verify_ledger(ledger) == []
    again = golden_run(tmp_path / "b", history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
    assert again.exit_code == 0 and again.run_dir is not None
    assert (
        json.loads((again.run_dir / "run.json").read_text("utf-8"))["history"]["new_configs"] == []
    )
    assert not (again.run_dir / hist.HISTORY_DIR).exists()
    assert len(_rows(ledger)) == 2  # the cache-only re-run appended nothing


def test_smoke_and_full_runs_never_compare(tmp_path):
    ledger = tmp_path / "runs.jsonl"
    for i in range(1, 6):
        hist.record(ledger, _view(_run(tmp_path, f"full-{i}", i, 0.70)))
    smoke = check_run(ledger, _view(_run(tmp_path, "smoke-1", 6, 0.70), scope="smoke"))
    assert _band(smoke, scope="smoke").n_history == 0
    full = check_run(ledger, _view(_run(tmp_path, "full-6", 7, 0.70)))
    assert _band(full, scope="full").n_history == 5


def test_three_reconciled_runs_are_three_points_not_six(tmp_path):
    """The verifier's case: three runs, each reconciled, are three points of accuracy and
    three of each cost series, never six, and a reconcile is not judged against its own run."""
    ledger = tmp_path / "runs.jsonl"
    for i in range(1, 4):
        run = _run(tmp_path, f"r-{i}", i, 0.70)
        hist.record(ledger, _view(run))
        rec = hist.record(ledger, _view(run, status="reconciled", costs_only=True))
        assert _band(rec, "cost_per_correct", scope="full", cost_status="reconciled").n_history == (
            i - 1
        )
    fourth = _run(tmp_path, "r-4", 4, 0.70)
    c = check_run(ledger, _view(fourth))
    assert _band(c, scope="full").n_history == 3
    assert _band(c, "cost_per_correct", scope="full", cost_status="estimated").n_history == 3
    r = check_run(ledger, _view(fourth, status="reconciled", costs_only=True))
    assert _band(r, "cost_per_correct", scope="full", cost_status="reconciled").n_history == 3


def test_the_committed_ledger_is_seeded_under_these_rules():
    """docs/measurements/runs.jsonl: the smoke (smoke scope), the baseline and the fix runs'
    8 fix configurations (full scope), each with its reconcile's cost rows; every line names
    the code that produced its metrics; the published record's accuracies are the fix line's.
    The seed is written from the stored records (a rescore), not by live runs, so the runner's
    cache rule does not apply to it; it meets the invariant the rule protects directly: in each
    scope a configuration has at most one accuracy point, so no answer counts twice (D-125)."""
    assert verify_ledger(LEDGER) == []
    rows = _rows(LEDGER)
    points = [
        (m["labels"]["scope"], m["labels"]["model"], m["labels"]["fixes"])
        for r in rows
        for m in _names(r, "accuracy")
    ]
    assert len(points) == len(set(points)) == 3 + 3 + 8  # smoke: 3; full: 3 baselines, 8 fixes
    assert [(r["run_id"][-6:], r["metrics"][0]["labels"]["scope"]) for r in rows] == [
        ("769d2c", "smoke"),
        ("c89eaa", "full"),
        ("c89eaa", "full"),
        ("6ccebe", "full"),
        ("6ccebe", "full"),
    ]
    assert all("seed (V5.1)" in r["note"] and "EvalKit 0.4.0" in r["note"] for r in rows)
    assert all("monitoring.parity --rescore --write" in r["note"] for r in rows)
    assert [r["at"] for r in rows] == sorted(r["at"] for r in rows)
    for i in (2, 4):  # the reconciles: reconciled cost rows only
        assert {m["name"] for m in rows[i]["metrics"]} <= hist.COST_METRICS
        assert {m["labels"]["cost_status"] for m in rows[i]["metrics"]} == {"reconciled"}
    fix = {
        (m["labels"]["model"], m["labels"]["fixes"]): m["value"]
        for m in _names(rows[3], "accuracy")
    }
    assert len(fix) == 8 and all(f != "none" for _, f in fix)
    summary = json.loads((RECORD / "summary.json").read_text("utf-8"))
    for b in summary["backends"]:
        key = (b["backend"], "+".join(b["fixes"]) or "none")
        if key in fix:
            assert fix[key] == b["accuracy"]["point"]
    base = {(m["labels"]["model"], m["labels"]["fixes"]) for m in _names(rows[1], "accuracy")}
    assert base == {("claude_mcp", "none"), ("gpt_mcp", "none"), ("cortex_analyst", "none")}
    h = history(LEDGER, gates=str(GATES))
    assert h.runs == 5 and h.project == "agentic-analytics"
    n = {(s.metric, tuple(sorted(s.labels.items()))): len(s.points) for s in h.series}
    assert n[("accuracy", (("fixes", "none"), ("model", "claude_mcp"), ("scope", "full")))] == 1


# golden


def test_the_trend_page_shows_the_three_series_with_bands(tmp_path):
    page = hist.trend_page(LEDGER, tmp_path / "trend.html", gates=GATES)
    text = page.read_text("utf-8")
    t = hist.trend(LEDGER, gates=GATES)
    assert {s.metric for s in t.series} == set(hist.TREND_METRICS)
    models = {dict(s.labels)["model"] for s in t.series if s.metric == "accuracy"}
    assert models == {"claude_mcp", "gpt_mcp", "cortex_analyst"}
    acc = [s for s in t.series if s.metric == "accuracy"]
    assert all(p.ci_low is not None and p.ci_high is not None for s in acc for p in s.points)
    assert hist.TREND_TITLE in text and "<svg" in text and "learning" in text
    assert "tokens_in_per_case" not in text  # only the three series
    assert hist.main(["--ledger", str(LEDGER), "--out", str(tmp_path / "ci.html")]) == 0
    assert (tmp_path / "ci.html").read_bytes() == page.read_bytes()  # the CI page is this one


# repro


def test_the_trend_page_and_the_ledger_repeat_byte_for_byte(tmp_path):
    a = hist.trend_page(LEDGER, tmp_path / "a.html", gates=GATES).read_bytes()
    b = hist.trend_page(LEDGER, tmp_path / "b.html", gates=GATES).read_bytes()
    assert a == b
    lines = []
    for sub in ("x", "y"):
        ledger = tmp_path / sub / "runs.jsonl"
        for i in (1, 2):
            hist.record(ledger, _view(_run(tmp_path / sub, f"r-{i}", i, 0.7)))
        lines.append(ledger.read_bytes())
    assert lines[0] == lines[1]
    assert [r["at"] for r in _rows(tmp_path / "x" / "runs.jsonl")] == [
        "2026-12-01T06:17:00Z",
        "2026-12-02T06:17:00Z",
    ]  # each line is stamped with its export's created_at


# safety


def test_the_ledger_holds_no_secret_question_or_answer_text():
    assert scan([LEDGER]) == []
    text = LEDGER.read_text("utf-8")
    bank = (ROOT / "src/agentic_analytics/packs/uk_price_paid/questions.jsonl").read_text("utf-8")
    for q in (json.loads(x)["text"] for x in bank.splitlines() if x.strip()):
        assert q not in text
    assert not re.search(r"/Users/|/private/|/home/[a-z]", text)
    keys = {k for r in map(json.loads, text.splitlines()) for k in r}
    assert keys <= {
        "at", "code_version", "config_hash", "evalkit", "heldout_rows_folded", "kind",
        "metrics", "note", "prev", "price_dates", "project", "recorded_with", "run_id",
        "schema", "settings_sha256", "simulated", "snapshot", "source", "status",
        "summary_sha256",
    }  # fmt: skip


def test_a_broken_ledger_is_refused_and_the_run_still_finishes(tmp_path):
    ledger = tmp_path / "runs.jsonl"
    hist.record(ledger, _view(_run(tmp_path, "r-1", 1, 0.7)))
    hist.record(ledger, _view(_run(tmp_path, "r-2", 2, 0.7)))
    rows = ledger.read_text("utf-8").splitlines()
    rows[0] = rows[0].replace('"r-1"', '"r-9"')  # an edited line breaks the chain
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert verify_ledger(ledger)
    with pytest.raises(LedgerError):
        hist.record(ledger, _view(_run(tmp_path, "r-4", 4, 0.7)))
    res = hist.record_quietly(ledger, _view(_run(tmp_path, "r-3", 3, 0.7)), note="run")
    assert res["recorded"] is False and res["error"]
    assert hist.record_quietly(ledger, None, note="run")["reason"] == "no new answers"


def test_a_smoke_run_is_scoped_smoke_and_others_full(tmp_path):
    assert hist.scope_of("smoke") == "smoke"
    assert hist.scope_of("run") == hist.scope_of("rescore") == "full"
    out = golden_run(tmp_path, history_path=tmp_path / "runs.jsonl")  # kind "run"
    assert out.run_dir is not None
    plan = json.loads((out.run_dir / "run.json").read_text("utf-8"))["history"]
    assert plan["scope"] == "full"
    view = load_metrics(out.run_dir / hist.HISTORY_DIR / METRICS_FILE)
    assert {m.labels["scope"] for m in view.metrics} == {"full"}


def test_the_all_uplift_lands_with_the_all_fixes_configuration():
    from evalkit.metrics import Metric

    m = load_metrics(GOLDEN / METRICS_DIR / METRICS_FILE)
    every = (*GOLDEN_CONFIGS, ("claude_mcp", hist.ALL_FIXES))
    up = Metric(name="uplift", unit="PP", value=2.0, labels={"model": "claude_mcp", "fix": "all"})
    m = m.model_copy(update={"metrics": [*m.metrics, up]})

    def uplifts(new):
        v = hist.ledger_metrics(
            m, scope="full", new_configs=new, all_configs=every, cost_status="estimated"
        )
        return [] if v is None else [r for r in v.metrics if r.name == "uplift"]

    assert hist.ALL_FIXES == "descriptions+examples+self_check"
    got = [r.labels["fix"] for r in uplifts([("claude_mcp", hist.ALL_FIXES)])]
    assert got == ["all"]
    assert "all" not in [r.labels["fix"] for r in uplifts([("claude_mcp", "examples")])]


def test_a_reconcile_adds_costs_only_for_the_runs_own_new_configurations(tmp_path):
    """A fix run's reconcile adds no cost point for the baseline answers it reused: the
    reconcile reads the run's `history.new_configs`, and no whole-run row is added."""
    ledger = tmp_path / "runs.jsonl"
    out = golden_run(tmp_path, history_path=ledger)
    assert out.run_dir is not None
    manifest = json.loads((out.run_dir / "run.json").read_text("utf-8"))
    fixes_only = [["claude_mcp", "examples"], ["gpt_mcp", "examples"]]
    manifest["history"]["new_configs"] = fixes_only  # as if the baselines were reused
    (out.run_dir / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    reconcile_run(out.run_dir, CREDITS, render=False, history_path=ledger)
    rec = _rows(ledger)[-1]
    assert rec["note"] == "reconcile" and rec["metrics"]
    cfgs = {(m["labels"].get("model"), m["labels"].get("fixes")) for m in rec["metrics"]}
    assert cfgs == {tuple(c) for c in fixes_only}  # no baseline and no whole-run row
    assert {m["labels"]["cost_status"] for m in rec["metrics"]} == {"reconciled"}


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_a_resumed_run_served_from_the_cache_appends_nothing(tmp_path):
    """D-121: a run that stopped before recording its history records nothing, and its
    answers served from the cache on resume add no point: a stated under-count, never a
    double-count. The resumed run writes nothing outside its own run folder (no other run's
    run.json, no ledger line)."""
    first = golden_run(tmp_path / "a")  # the earlier attempt's answers, cached
    assert first.run_dir is not None
    results = tmp_path / "b" / "results"
    stopped = results / "stopped-1"
    stopped.mkdir(parents=True)
    # an older run.json that still names pending configurations: nothing may act on it
    stopped_manifest = {"run_id": "stopped-1", "history_pending": [["claude_mcp", "none"]]}
    (stopped / "run.json").write_text(json.dumps(stopped_manifest) + "\n", encoding="utf-8")
    ledger = tmp_path / "docs" / "measurements" / "runs.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"")
    before = {
        **_tree(tmp_path / "a"),
        **{f"ledger/{k}": v for k, v in _tree(ledger.parent).items()},
    }
    stopped_before = _tree(stopped)
    resumed = golden_run(tmp_path / "b", history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
    assert resumed.exit_code == 0 and resumed.run_dir is not None
    manifest = json.loads((resumed.run_dir / "run.json").read_text("utf-8"))
    assert manifest["history"]["new_configs"] == []
    assert "history_pending" not in manifest and "history_carried_to" not in manifest
    assert not (resumed.run_dir / hist.HISTORY_DIR).exists()
    after = {**_tree(tmp_path / "a"), **{f"ledger/{k}": v for k, v in _tree(ledger.parent).items()}}
    assert after == before and _tree(stopped) == stopped_before
    outside = {p.parent for p in results.glob("*/*")} - {resumed.run_dir, stopped}
    assert outside == set()  # only its own folder (and results/latest) was written


def test_the_kept_clarification_method_is_keyed_by_seed_and_resamples():
    """kept_ci_method finds the method `backend_summary` drew for that configuration, seed
    and resample count, and nothing for another seed."""
    from agentic_analytics import aggregate
    from agentic_analytics.schemas import RunSummary

    summary = RunSummary.model_validate_json((GOLDEN / "summary.json").read_text("utf-8"))
    b = summary.backends[0]
    aggregate._CI_METHODS.clear()
    assert aggregate.kept_ci_method(b, 2000, 7) is None
    aggregate._CI_METHODS[aggregate._ci_key(b, 2000, 7)] = "drawn with seed 7"
    assert aggregate.kept_ci_method(b, 2000, 7) == "drawn with seed 7"
    assert aggregate.kept_ci_method(b, 2000, 8) is None
    assert aggregate.kept_ci_method(b, 1000, 7) is None
    aggregate._CI_METHODS.clear()


def test_the_clarification_method_comes_from_the_one_call(monkeypatch, tmp_path):
    """MON-09: metrics-1 reads `ci_method` from the call that drew the summary's intervals,
    not from a second bootstrap."""
    from agentic_analytics import aggregate

    calls = []
    real = aggregate.evalkit_clarification

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(aggregate, "evalkit_clarification", counted)
    out = golden_run(tmp_path)
    assert out.exit_code == 0 and out.run_dir is not None
    assert len(calls) == 5  # one per configuration, none from the metrics export
    m = load_metrics(out.run_dir / METRICS_DIR / METRICS_FILE)
    methods = [x.ci_method or "" for x in m.metrics if x.name == "clarification_rate"]
    assert methods and all("cluster bootstrap" in x for x in methods)


def test_a_rerun_that_retries_one_answer_appends_nothing(tmp_path):
    """D-125 (verifier round 4, M1): a transient answer is never cached, so a rerun asks it
    again and serves the rest from the cache. Its configuration was only partly asked, so it
    adds no band point: the earlier run's answers are never counted twice."""
    first = golden_run(tmp_path / "a")
    assert first.run_dir is not None
    log = [json.loads(line) for line in (first.run_dir / "run.log").read_text("utf-8").splitlines()]
    asks = [e for e in log if e.get("event") == "ask" and not e.get("cache_hit")]
    key = asks[0]["cache_key"]
    gone = [p for p in (tmp_path / "a" / ".cache").rglob("*") if p.is_file() and key in p.name]
    assert gone  # the one answer the rerun has to ask again
    for p in gone:
        p.unlink()
    ledger = tmp_path / "runs.jsonl"
    rerun = golden_run(tmp_path / "b", history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
    assert rerun.exit_code == 0 and rerun.run_dir is not None
    relog = (rerun.run_dir / "run.log").read_text("utf-8").splitlines()
    asked = [e for e in map(json.loads, relog) if e.get("event") == "ask" and not e["cache_hit"]]
    assert [e["cache_key"] for e in asked] == [key]
    manifest = json.loads((rerun.run_dir / "run.json").read_text("utf-8"))
    assert manifest["history"]["new_configs"] == []
    assert not ledger.exists() or ledger.read_text("utf-8").strip() == ""


def test_a_run_that_asks_every_answer_of_a_configuration_records_it(tmp_path):
    """The other side of D-125: a configuration whose answers this run asked, all of them, is
    new, so a first run records every configuration it ran."""
    ledger = tmp_path / "runs.jsonl"
    out = golden_run(tmp_path, history_path=ledger)
    assert out.run_dir is not None
    manifest = json.loads((out.run_dir / "run.json").read_text("utf-8"))
    assert sorted(map(tuple, manifest["history"]["new_configs"])) == sorted(
        (b, f) for b, f in GOLDEN_CONFIGS
    )
    assert len(_rows(ledger)) == 1


def _restamp(cache: Path, scope: str | None) -> int:
    """Rewrite every cache entry's stored scope (None: drop it, as before D-125)."""
    n = 0
    for p in cache.rglob("*.json"):
        entry = json.loads(p.read_text("utf-8"))
        entry.pop("scope", None)
        if scope is not None:
            entry["scope"] = scope
        p.write_text(json.dumps(entry, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        n += 1
    return n


def test_a_full_run_on_answers_a_smoke_run_asked_records_every_configuration(tmp_path):
    """D-125: smoke and full points never share a band, so answers a smoke run asked count
    again in a full run that reuses them from the cache (the usual smoke-then-full flow)."""
    first = golden_run(tmp_path / "a")
    assert first.run_dir is not None
    assert _restamp(tmp_path / "a" / ".cache", "smoke") > 0  # as if the smoke had asked them
    entries = sorted((tmp_path / "a" / ".cache").rglob("*.json"))
    for p in entries[::2]:  # the full run asks these itself (it selects more than the smoke)
        p.unlink()
    ledger = tmp_path / "runs.jsonl"
    full = golden_run(tmp_path / "b", history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
    assert full.exit_code == 0 and full.run_dir is not None
    manifest = json.loads((full.run_dir / "run.json").read_text("utf-8"))
    assert manifest["history"]["scope"] == "full"
    assert sorted(map(tuple, manifest["history"]["new_configs"])) == sorted(GOLDEN_CONFIGS)
    assert len(_rows(ledger)) == 1


def test_cached_answers_with_no_stored_scope_add_no_point(tmp_path):
    """An answer cached before scopes were stored may already be in a point of this scope,
    so its configuration adds none (the safe under-count)."""
    first = golden_run(tmp_path / "a")
    assert first.run_dir is not None
    assert _restamp(tmp_path / "a" / ".cache", None) > 0
    ledger = tmp_path / "runs.jsonl"
    again = golden_run(tmp_path / "b", history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
    assert again.run_dir is not None
    manifest = json.loads((again.run_dir / "run.json").read_text("utf-8"))
    assert manifest["history"]["new_configs"] == []
    assert not ledger.exists() or ledger.read_text("utf-8").strip() == ""


def test_a_second_reconcile_of_a_run_adds_no_second_cost_point(tmp_path):
    """D-125 (verifier round 5, S1): correcting a run's credits reconciles its folder again
    but records its reconciled costs once."""
    ledger = tmp_path / "runs.jsonl"
    out = golden_run(tmp_path, history_path=ledger)
    assert out.run_dir is not None
    reconcile_run(out.run_dir, CREDITS, render=False, history_path=ledger)
    after_first = _rows(ledger)
    assert after_first[-1]["note"] == "reconcile"
    assert json.loads((out.run_dir / "run.json").read_text("utf-8"))["history_reconciled"]
    corrected = {"snowflake": 0.7, "clickhouse": 0.0}
    reconcile_run(out.run_dir, corrected, render=False, history_path=ledger)
    assert _rows(ledger) == after_first
    log = (out.run_dir / "run.log").read_text("utf-8").splitlines()
    assert "already recorded" in json.loads(log[-1])["reason"]


def test_a_run_that_asks_nothing_adds_no_point_whoever_cached_the_answers(tmp_path):
    """D-125 (verifier round 6, R6-M1): a smoke after a full run, or a rescore, served wholly
    from cache entries of the other scope asks nothing, so it adds no point: otherwise every
    repeat would add the same answers again."""
    first = golden_run(tmp_path / "a")
    assert first.run_dir is not None
    assert _restamp(tmp_path / "a" / ".cache", "smoke") > 0  # the other scope for a full run
    ledger = tmp_path / "runs.jsonl"
    for n in ("b", "c"):
        again = golden_run(tmp_path / n, history_path=ledger, cache_dir=tmp_path / "a" / ".cache")
        assert again.run_dir is not None
        manifest = json.loads((again.run_dir / "run.json").read_text("utf-8"))
        assert manifest["history"]["new_configs"] == []
    assert not ledger.exists() or ledger.read_text("utf-8").strip() == ""
