"""GLD-06's other half: `run.gold.require_gold` refuses a gold check that no longer matches the
files on disk (D-73: `run/gold.py` ships with Q2.2, so its refusals are proven here, without the
runner). Each test runs a clean gold check over the fake world, then breaks one thing."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from agentic_analytics.gold import run_gold_check
from agentic_analytics.questions import load_questions
from agentic_analytics.run.gold import (
    GOLD_CHECK_FILE,
    GoldCheck,
    PreconditionError,
    current_snapshot,
    require_gold,
)
from tests.gold_check.fakes import AS_OF, SNAPSHOT
from tests.gold_check.world import make_world

pytestmark = pytest.mark.gold

T0 = datetime(2026, 11, 15, 12, 30, 5, tzinfo=UTC)
BOTH = ("clickhouse", "snowflake")


@pytest.fixture
def world(tmp_path):
    w = make_world(tmp_path)
    executors: dict = w.executors()
    out = run_gold_check(
        w.pack, w.questions, executors, as_of=AS_OF, snapshot_sha256=SNAPSHOT, now=lambda: T0
    )
    assert out.exit_code == 0 and out.check is not None
    return w


def _require(world, engines=BOTH, snapshot=SNAPSHOT, as_of=None):
    return require_gold(world.pack, load_questions(world.pack), engines, snapshot, as_of=as_of)


def _edit_json(path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_clean_check_is_accepted(world) -> None:
    check, selected, gold = _require(world, as_of=AS_OF)
    assert check.status == "pass" and selected and gold


def test_editing_a_gold_file_after_the_check_refuses(world) -> None:
    path = world.root / "gold" / "clickhouse" / "q-001.json"
    _edit_json(path, lambda d: d["result"].__setitem__("rows", [[1]]))
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(world)


def test_editing_probe_gold_after_the_check_refuses(world) -> None:
    path = world.root / "gold" / "clickhouse" / "p-iq01.json"
    _edit_json(path, lambda d: d["result"].__setitem__("rows", [[1]]))
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(world)


def test_editing_a_selected_question_after_the_check_refuses(world) -> None:
    p = world.root / "questions.jsonl"
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    rows[0]["text"] += " Please."
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(PreconditionError, match="changed after the gold check"):
        _require(world)


def test_another_snapshot_refuses(world) -> None:
    with pytest.raises(PreconditionError, match="not the current snapshot"):
        _require(world, snapshot="7" * 64)


def test_another_as_of_refuses(world) -> None:
    with pytest.raises(PreconditionError, match="as-of 2026-09-30, not the run's 2026-08-31"):
        _require(world, as_of=date(2026, 8, 31))


def test_a_check_for_another_pack_refuses(world) -> None:
    _edit_json(world.root / GOLD_CHECK_FILE, lambda d: d.__setitem__("pack", "other_pack@1.0"))
    with pytest.raises(PreconditionError, match=r"the gold check is for other_pack@1\.0, not"):
        _require(world)


def test_missing_gold_result_refuses(world) -> None:
    (world.root / "gold" / "snowflake" / "q-001.json").unlink()
    with pytest.raises(PreconditionError, match=r"no gold result for gold/snowflake/q-001\.json"):
        _require(world)


def test_missing_probe_gold_refuses(world) -> None:
    (world.root / "gold" / "clickhouse" / "p-id01.json").unlink()
    with pytest.raises(PreconditionError, match=r"no probe gold for gold/clickhouse/p-id01\.json"):
        _require(world)


def test_a_gold_file_holding_another_question_refuses(world) -> None:
    _, selected, _ = _require(world)
    a, b = [q.id for q in selected if q.expected == "answer"][:2]
    gold = world.root / "gold" / "clickhouse"
    (gold / f"{b}.json").write_text(
        (gold / f"{a}.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(PreconditionError, match=rf"gold/clickhouse/{b}\.json holds {a}"):
        _require(world)


def test_a_probe_gold_file_holding_another_probe_refuses(world) -> None:
    gold = world.root / "gold" / "clickhouse"
    (gold / "p-id01.json").write_text(
        (gold / "p-iq01.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(PreconditionError, match=r"gold/clickhouse/p-id01\.json holds p-iq01"):
        _require(world)


def test_a_check_selecting_unknown_questions_refuses(world) -> None:
    _edit_json(
        world.root / GOLD_CHECK_FILE,
        lambda d: d.__setitem__("questions", sorted([*d["questions"], "q-999"])),
    )
    with pytest.raises(PreconditionError, match=r"selects unknown questions \['q-999'\]"):
        _require(world)


def test_engine_not_covered_refuses(world) -> None:
    _edit_json(
        world.root / GOLD_CHECK_FILE,
        lambda d: d.update(engines=["clickhouse"], mode="single_engine"),
    )
    with pytest.raises(PreconditionError, match="does not cover snowflake"):
        _require(world)


def test_missing_and_failed_checks_refuse(world) -> None:
    _edit_json(
        world.root / GOLD_CHECK_FILE,
        lambda d: d.update(status="fail", failures=["q-001 [clickhouse]: tie at the LIMIT cut"]),
    )
    with pytest.raises(PreconditionError, match="failed"):
        _require(world)
    (world.root / GOLD_CHECK_FILE).unlink()
    with pytest.raises(PreconditionError, match="no gold check"):
        _require(world)


def test_gold_check_record_invariants() -> None:
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
        checked_at=T0,
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


def test_current_snapshot_is_the_newest_record(tmp_path) -> None:
    with pytest.raises(PreconditionError, match="no snapshot record"):
        current_snapshot(tmp_path)
    for day, sha in (("2026-11-01", "2" * 64), ("2026-10-01", "1" * 64)):
        (tmp_path / f"snapshot-{day}.json").write_text(
            json.dumps({"sha256": sha, "downloaded_at": f"{day}T09:00:00+00:00"}),
            encoding="utf-8",
        )
    assert current_snapshot(tmp_path) == "2" * 64
