"""RUN-04: a run refuses to start without a clean gold check and a parity record for the current
snapshot."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from agentic_analytics.packs import load_pack
from agentic_analytics.questions import load_questions
from agentic_analytics.run.gold import (
    GoldCheck,
    PreconditionError,
    compute_gold_check_id,
    current_snapshot,
    load_gold_check,
    require_gold,
    require_parity,
)
from agentic_analytics.run.runner import run_benchmark
from tests.run import world as w

pytestmark = pytest.mark.gold


def _require(pack_dir, engines=("clickhouse", "snowflake"), snapshot=w.SNAPSHOT):
    pack = load_pack(pack_dir)
    return require_gold(pack, load_questions(pack), engines, snapshot)


def test_clean_gold_check_returns_the_selection_and_gold(tmp_path):
    world = w.World.from_bundled()
    w.write_pack(tmp_path / "pack", world)
    check, selected, gold = _require(tmp_path / "pack")
    assert [q.id for q in selected] == sorted(q.id for q in world.questions)
    answer_qs = [q for q in world.questions if q.expected == "answer"]
    probe_gold = world.probe_gold()
    assert probe_gold and len(gold) == 2 * len(answer_qs) + len(probe_gold)
    listed = [p for p in world.probes if p.id in check.probes]
    assert check.gold_check_id == compute_gold_check_id(
        world.questions, gold, w.SNAPSHOT, as_of=w.AS_OF, probes=listed
    )


def test_gold_check_for_another_as_of_refuses(tmp_path):
    w.write_pack(tmp_path / "pack", w.World.from_bundled())
    pack = load_pack(tmp_path / "pack")
    bank = load_questions(pack)
    engines = ("clickhouse", "snowflake")
    require_gold(pack, bank, engines, w.SNAPSHOT, as_of=w.AS_OF)
    with pytest.raises(PreconditionError, match="as-of 2026-09-30, not the run's 2026-08-31"):
        require_gold(pack, bank, engines, w.SNAPSHOT, as_of=date(2026, 8, 31))


def test_editing_probe_gold_after_the_check_refuses(tmp_path):
    world = w.World.from_bundled()
    w.write_pack(tmp_path / "pack", world)
    (pid, engine) = next(iter(world.probe_gold()))
    p = tmp_path / "pack" / "gold" / engine / f"{pid}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["result"]["rows"] = [[1]]
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(tmp_path / "pack")


def test_missing_gold_check_refuses(tmp_path):
    w.write_pack(tmp_path / "pack", w.World.from_bundled(), with_check=False)
    assert load_gold_check(load_pack(tmp_path / "pack")) is None
    with pytest.raises(PreconditionError, match="no gold check"):
        _require(tmp_path / "pack")


def test_failed_gold_check_refuses(tmp_path):
    w.write_pack(tmp_path / "pack", w.World.from_bundled(), status="fail")
    with pytest.raises(PreconditionError, match="failed"):
        _require(tmp_path / "pack")


def test_gold_check_for_another_snapshot_refuses(tmp_path):
    w.write_pack(tmp_path / "pack", w.World.from_bundled())
    with pytest.raises(PreconditionError, match="not the current snapshot"):
        _require(tmp_path / "pack", snapshot="7" * 64)


def test_editing_a_gold_file_after_the_check_refuses(tmp_path):
    world = w.World.from_bundled()
    w.write_pack(tmp_path / "pack", world)
    q = next(q for q in world.questions if q.expected == "answer")
    p = tmp_path / "pack" / "gold" / "clickhouse" / f"{q.id}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["result"]["rows"] = [[1]]
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(tmp_path / "pack")


def test_editing_a_question_after_the_check_refuses(tmp_path):
    world = w.World.from_bundled()
    w.write_pack(tmp_path / "pack", world)
    p = tmp_path / "pack" / "questions.jsonl"
    p.write_text(p.read_text(encoding="utf-8").replace("2023", "2022", 1), encoding="utf-8")
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(tmp_path / "pack")


def test_missing_gold_result_refuses(tmp_path):
    world = w.World.from_bundled()
    w.write_pack(tmp_path / "pack", world)
    q = next(q for q in world.questions if q.expected == "answer")
    (tmp_path / "pack" / "gold" / "snowflake" / f"{q.id}.json").unlink()
    with pytest.raises(PreconditionError, match="no gold result"):
        _require(tmp_path / "pack")


def test_engine_not_covered_by_the_gold_check_refuses(tmp_path):
    w.write_pack(tmp_path / "pack", w.World.from_bundled(), engines=("clickhouse",))
    with pytest.raises(PreconditionError, match="does not cover snowflake"):
        _require(tmp_path / "pack")
    check, _, _ = _require(tmp_path / "pack", engines=("clickhouse",))
    assert check.mode == "single_engine"


def test_gold_check_record_invariants():
    base = dict(
        schema_version="1.0",
        kind="gold_check",
        pack="uk_price_paid@1.0",
        snapshot_sha256="a" * 64,
        as_of="2026-09-30",
        engines=("clickhouse", "snowflake"),
        mode="cross_engine",
        status="pass",
        questions=("q-001",),
        checked_at=w.T0,
        gold_check_id="b" * 64,
    )
    GoldCheck.model_validate(base)
    for bad in (
        {"mode": "single_engine"},  # two engines are cross-engine
        {"status": "fail"},  # a failure lists failures
        {"failures": ("q-001: tie",)},  # a pass lists none
        {"questions": ("q-001", "q-001")},
        {"probes": ("p-iq01", "p-iq01")},
        {"probes": ("q-001",)},
        {"as_of": "yesterday"},
        {"engines": ("clickhouse", "clickhouse")},
        {"gold_check_id": "nothex"},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            GoldCheck.model_validate({**base, **bad})


def test_current_snapshot_is_the_newest_record(tmp_path):
    with pytest.raises(PreconditionError, match="no snapshot record"):
        current_snapshot(tmp_path)
    for day, sha in (("2026-10-01", "1" * 64), ("2026-11-01", "2" * 64)):
        (tmp_path / f"snapshot-{day}.json").write_text(
            json.dumps({"sha256": sha, "downloaded_at": f"{day}T09:00:00+00:00"}),
            encoding="utf-8",
        )
    assert current_snapshot(tmp_path) == "2" * 64


def test_parity_must_pass_for_this_snapshot_and_engines(tmp_path):
    with pytest.raises(PreconditionError, match="none found"):
        require_parity(tmp_path, w.SNAPSHOT, ("clickhouse", "snowflake"))
    w.write_measurements(tmp_path, engines=("clickhouse",))
    with pytest.raises(PreconditionError, match="none covers"):
        require_parity(tmp_path, w.SNAPSHOT, ("clickhouse", "snowflake"))
    assert require_parity(tmp_path, w.SNAPSHOT, ("clickhouse",)).name.startswith("parity-")
    with pytest.raises(PreconditionError, match="none found"):
        require_parity(tmp_path, "9" * 64, ("clickhouse",))


def test_failed_parity_refuses(tmp_path):
    w.write_measurements(tmp_path, parity_status="fail")
    with pytest.raises(PreconditionError, match="parity failed"):
        require_parity(tmp_path, w.SNAPSHOT, ("clickhouse", "snowflake"))


def test_single_engine_parity_is_not_enough_for_two_engines(tmp_path):
    w.write_measurements(tmp_path, parity_status="single_engine")
    with pytest.raises(PreconditionError):
        require_parity(tmp_path, w.SNAPSHOT, ("clickhouse", "snowflake"))


@pytest.mark.parametrize(
    "damage",
    [
        "no_gold_check",
        "failed_gold",
        "no_parity",
        "failed_parity",
        "no_snapshot",
        "edited_gold",
        "other_as_of",
    ],
)
def test_run_refuses_with_exit_1_and_makes_no_calls(tmp_path, damage):
    s = w.setup(tmp_path)
    m = tmp_path / "measurements"
    if damage == "no_gold_check":
        (s.pack_dir / "gold" / "gold_check.json").unlink()
    elif damage == "failed_gold":
        w.write_pack(s.pack_dir, s.world, status="fail")
    elif damage == "no_parity":
        (m / "parity-2026-11-01.json").unlink()
    elif damage == "failed_parity":
        w.write_measurements(m, parity_status="fail")
    elif damage == "no_snapshot":
        (m / "snapshot-2026-11-01.json").unlink()
    elif damage == "other_as_of":
        w.write_pack(s.pack_dir, s.world, as_of=date(2026, 8, 31))
    elif damage == "edited_gold":
        q = next(q for q in s.world.questions if q.expected == "answer")
        p = s.pack_dir / "gold" / "clickhouse" / f"{q.id}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["sql_hash"] = "0" * 64
        p.write_text(json.dumps(data), encoding="utf-8")
    cfg = w.config()
    out = run_benchmark(
        cfg, s.pack(), services=lambda: s.services(cfg), options=w.options(tmp_path)
    )
    assert out.exit_code == 1, out.message
    assert out.message.startswith("refusing to start")
    assert s.counter.total == 0
    assert not (tmp_path / "results").exists()
