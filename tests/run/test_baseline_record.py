"""R3.2: the baseline record covers the run once and its numbers are the run's (BAS-01..04)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/measurements/baseline-2026-11-17.json"
LEDGER = ROOT / "docs/measurements/spend-ledger.jsonl"
BACKENDS = {"claude_mcp", "gpt_mcp", "cortex_analyst"}


def record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_the_baseline_covers_every_job_of_the_plan() -> None:
    r = record()
    assert r["trials"] == 3 and r["jobs"] == 1065 == r["called"] + r["cached_from_smoke"]
    assert set(r["per_backend"]) == BACKENDS
    for b in r["per_backend"].values():
        assert set(b["by_category"]) == {
            "aggregation",
            "time",
            "geography",
            "top_n",
            "yoy",
            "ambiguous",
        }
        assert b["accuracy_strict"] is not None  # rescored under OWNER D-81
        assert b["accuracy_strict"]["point"] <= b["accuracy"]["point"]


def test_the_spend_is_in_the_ledger() -> None:
    rows = [json.loads(x) for x in LEDGER.read_text(encoding="utf-8").splitlines() if x]
    (line,) = [x for x in rows if x["run_id"] == record()["spend"]["ledger_run_id"]]
    assert abs(line["spent_usd"] - record()["spend"]["model_usd"]) < 1e-6
    assert line["credits"]["snowflake"] > 0  # Cortex trial credits (D-78)


def test_each_trial_set_finished_inside_45_minutes() -> None:
    walls = record()["trial_wall_minutes"]
    assert all(walls[t] < 45 for t in ("1", "2", "3"))


def test_credits_are_reconciled_from_the_metered_usage() -> None:
    r = record()["reconciled"]
    metered = json.loads((ROOT / r["metered_record"]).read_text(encoding="utf-8"))
    assert r["cost_status"] == "reconciled"
    assert r["warehouse_credits_passed"] == metered["baseline_reconcile_credits"]
    hour = metered["hours"]["2026-11-17T08:00Z"]
    assert (
        abs(
            r["total_credits"]["snowflake"]
            - (hour["cortex_credits"] + hour["warehouse_credits"] + 0.804)
        )
        < 1e-6
    )


def test_ten_failures_were_checked_by_hand_across_backends_and_categories() -> None:
    checks = record()["hand_check"]
    assert len(checks) >= 10 and {c["backend"] for c in checks} == BACKENDS
    assert all(c["verdict"] and c["reason"] for c in checks)
    assert record()["scorer_defects_found"] == 0


def test_the_record_is_stamped_with_the_bundled_gold_check_and_frozen_fixes() -> None:
    r = record()
    pack = ROOT / "src/agentic_analytics/packs/uk_price_paid"
    gold = json.loads((pack / "gold/gold_check.json").read_text(encoding="utf-8"))
    frozen = json.loads((pack / "fixes/FROZEN.json").read_text(encoding="utf-8"))
    assert r["gold_check_id"] == gold["gold_check_id"] and r["as_of"] == gold["as_of"]
    assert r["fix_hash"] == frozen["fix_hash"]


def test_the_hand_check_spans_every_category_including_ambiguous() -> None:
    checks = record()["hand_check"]
    cats = {
        json.loads(x)["id"]: json.loads(x)["category"]
        for x in (ROOT / "src/agentic_analytics/packs/uk_price_paid/questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    assert all(c["category"] == cats[c["question"]] for c in checks)
    assert {c["category"] for c in checks} == set(cats.values())
    assert len({c["question"] for c in checks}) >= 10


def test_headline_accuracy_is_never_below_strict_and_the_run_bounds_each_trial() -> None:
    r = record()
    for b in r["per_backend"].values():
        assert b["accuracy_strict"]["point"] <= b["accuracy"]["point"] <= 1
        for c in b["by_category"].values():
            assert c["accuracy_strict"]["point"] <= c["accuracy"]["point"]
    walls = r["trial_wall_minutes"]
    assert all(walls[t] <= walls["run"] for t in ("1", "2", "3"))


def test_every_probe_failure_has_a_hand_label() -> None:
    r = record()
    for probe, by_backend in r["probes"].items():
        for backend, passed in by_backend.items():
            if not passed:
                assert probe in r["probe_hand_labels"][backend], (probe, backend)


QUESTIONS_PER_BACKEND = 100
TRIALS = 3


def test_headline_minus_strict_is_exactly_the_equivalence_passes() -> None:
    """OWNER D-81/D-89: each rescued trial lifts the headline by one trial in 300."""
    for backend, b in record()["per_backend"].items():
        gap = (
            (b["accuracy"]["point"] - b["accuracy_strict"]["point"])
            * QUESTIONS_PER_BACKEND
            * TRIALS
        )
        assert round(gap) == sum(b["equivalence_passes"].values()), backend


def test_category_accuracies_weighted_by_questions_give_the_overall() -> None:
    weights = {
        "aggregation": 17,
        "time": 17,
        "geography": 17,
        "top_n": 17,
        "yoy": 16,
        "ambiguous": 16,
    }
    for backend, b in record()["per_backend"].items():
        for key in ("accuracy", "accuracy_strict"):
            pooled = sum(weights[c] * v[key]["point"] for c, v in b["by_category"].items())
            assert abs(pooled / sum(weights.values()) - b[key]["point"]) < 1e-3, (backend, key)


def test_labels_and_disputes_agree_with_the_tables() -> None:
    r = record()
    for backend, labels in r["probe_hand_labels"].items():
        assert all(r["probes"][probe][backend] is False for probe in labels), backend
    disputed = {c["question"] for c in r["hand_check"] if c["verdict"].startswith("disputed")}
    assert disputed == {d["question"] for d in r["scorer_disputes"]}
    assert r["trial_wall_minutes"]["run"] < 45
