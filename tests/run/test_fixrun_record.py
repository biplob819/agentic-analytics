"""F3.2: the fix-run record covers each fix alone on the MCP agents and its uplifts hold together
(FXR-01, FXR-02); the failure taxonomy labels 30 baseline failures (FXR-03)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEAS = ROOT / "docs/measurements"
FIXES = ("descriptions", "examples", "self_check")
MCP = ("claude_mcp", "gpt_mcp")
TAXONOMY = {
    "wrong_filter",
    "wrong_column_or_join",
    "wrong_aggregation",
    "literal_case",
    "time_anchoring",
    "missed_clarification",
    "other",
}


# v0.1.0's record and v0.1.1's (the same answers rescored on EvalKit 0.4.0, OWNER D-118):
# every check below holds for both
RECORDS = ("fixruns-2026-11-17.json", "fixruns-evalkit-0.4-2026-11-18.json")


@pytest.fixture(params=RECORDS)
def record_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def fixruns(record_name: str):
    data = json.loads((MEAS / record_name).read_text(encoding="utf-8"))
    return lambda: data


def test_each_fix_runs_alone_on_the_mcp_agents_and_cortex_only_at_baseline(fixruns) -> None:
    r = fixruns()
    configs = set(r["per_config"])
    sets = ("baseline", *FIXES, "+".join(FIXES))  # each fix alone, then all together (§7)
    assert configs == {f"{b}/{f}" for b in MCP for f in sets} | {"cortex_analyst/baseline"}
    assert r["new_answers"] == len(MCP) * (len(FIXES) + 1) * 3 * 120  # 100 questions + 20 probes
    pack = ROOT / "src/agentic_analytics/packs/uk_price_paid"
    frozen = json.loads((pack / "fixes/FROZEN.json").read_text(encoding="utf-8"))
    assert r["fix_hash"] == frozen["fix_hash"]


def test_uplifts_cover_every_fix_and_agree_with_the_accuracies(fixruns) -> None:
    r = fixruns()
    ups = {(u["backend"], u["fix"]): u for u in r["uplifts"]}
    assert set(ups) == {(b, f) for b in MCP for f in (*FIXES, "all")}
    for (b, f), u in ups.items():
        d = u["delta_points"]
        cfg = "+".join(FIXES) if f == "all" else f
        gap = 100 * (r["per_config"][f"{b}/{cfg}"]["accuracy"]["point"]
                     - r["per_config"][f"{b}/baseline"]["accuracy"]["point"])  # fmt: skip
        assert abs(d["point"] - gap) < 0.05, (b, f)
        if u["separated"]:
            assert not d["lo"] <= 0 <= d["hi"], (b, f)
    for c in r["per_config"].values():
        assert c["accuracy_strict"]["point"] <= c["accuracy"]["point"]


def test_fix_run_spend_is_in_the_ledger(fixruns) -> None:
    r = fixruns()
    rows = [
        json.loads(x)
        for x in (MEAS / "spend-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if x
    ]
    lines = [x for x in rows if x["run_id"] in r["spend"]["ledger_run_ids"]]
    assert len(lines) == len(r["spend"]["ledger_run_ids"]) == 2
    assert abs(sum(x["spent_usd"] for x in lines) - r["spend"]["model_usd"]) < 1e-6
    assert r["spend"]["model_usd"] <= 35  # OWNER D-80
    assert not any(x["credits"] for x in lines)  # the MCP agents only: no Cortex messages


def test_the_self_check_caveat_and_uplift_sources_are_stated(fixruns) -> None:
    r = fixruns()
    assert "never fired" in r["self_check_note"]
    for key, src in r["uplift_sources"].items():
        backend, fix = key.split("/")
        label = "all" if "+" in fix else fix
        (u,) = [u for u in r["uplifts"] if u["backend"] == backend and u["fix"] == label]
        total = src["from_ambiguous_points"] + src["from_other_points"]
        assert abs(total - u["delta_points"]["point"]) < 0.05, key


def test_thirty_baseline_failures_are_hand_labelled() -> None:
    t = json.loads((MEAS / "failure-taxonomy-2026-11-17.json").read_text(encoding="utf-8"))
    items = t["items"]
    assert len(items) == 30 and set(t["taxonomy"]) <= TAXONOMY
    assert {i["backend"] for i in items} == {*MCP, "cortex_analyst"}
    assert all(i["primary"] in TAXONOMY and i["evidence"].strip() for i in items)
    assert all(not i["question"].startswith("p-") for i in items)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["backend"]] = counts.get(i["backend"], 0) + 1
    assert min(counts.values()) >= 6


def test_separation_follows_holm_step_down_per_backend(fixruns) -> None:
    """§6.2 / D-34: per backend, sort p ascending; reject while p <= 0.05 / (m - rank); a fix
    is separated only if Holm rejects it and its interval excludes zero."""
    r = fixruns()
    for backend in MCP:
        family = sorted(
            (u for u in r["uplifts"] if u["backend"] == backend), key=lambda u: u["p_value"]
        )
        m, rejecting = len(family), True
        for rank, u in enumerate(family):
            rejecting = rejecting and u["p_value"] <= 0.05 / (m - rank)
            d = u["delta_points"]
            assert u["separated"] == (rejecting and not d["lo"] <= 0 <= d["hi"]), (
                backend,
                u["fix"],
            )
