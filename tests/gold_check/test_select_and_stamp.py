"""GLD-05 (seeded selection with the pack's quotas) and GLD-06 (gold/<engine>/<qid>.json,
gold/check-<ts>.json and a gold_check_id R3.1's runner accepts)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime

import pytest

from agentic_analytics.gold import SELECTION_SEED, run_gold_check, select_questions
from agentic_analytics.questions import load_probes, load_questions
from agentic_analytics.run.gold import (
    GOLD_CHECK_FILE,
    AnyGold,
    GoldCheck,
    PreconditionError,
    ProbeGold,
    compute_gold_check_id,
    load_gold_results,
    load_probe_gold,
    require_gold,
)
from agentic_analytics.schemas import GoldResult
from tests.gold_check.fakes import AS_OF, BUNDLED, SNAPSHOT, bound, rs
from tests.gold_check.world import make_world

T0 = datetime(2026, 11, 15, 12, 30, 5, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def run(world, **kw):
    return run_gold_check(
        world.pack,
        world.questions,
        world.executors(*kw.pop("engines", ())),
        as_of=AS_OF,
        snapshot_sha256=SNAPSHOT,
        now=lambda: T0,
        **kw,
    )


# -- GLD-05 -------------------------------------------------------------------------------------


def test_bundled_quotas_select_100_as_17_17_17_17_16_16():
    bank = load_questions(BUNDLED)
    assert dict(BUNDLED.manifest.quotas) == {
        "aggregation": 17,
        "time": 17,
        "geography": 17,
        "top_n": 17,
        "yoy": 16,
        "ambiguous": 16,
    }
    ids, problems = select_questions(bank, BUNDLED.manifest.quotas)
    assert problems == [] and len(ids) == 100 and list(ids) == sorted(ids)
    by_id = {q.id: q for q in bank}
    counts = Counter(by_id[i].category for i in ids)
    assert counts == Counter(dict(BUNDLED.manifest.quotas))


def test_selection_is_seeded_and_independent_of_input_order():
    bank = list(load_questions(BUNDLED))
    quotas = BUNDLED.manifest.quotas
    a, _ = select_questions(bank, quotas, SELECTION_SEED)
    b, _ = select_questions(list(reversed(bank)), quotas, SELECTION_SEED)
    c, _ = select_questions(bank, quotas, SELECTION_SEED + 1)
    assert a == b and a != c


def test_selection_draws_only_from_passing_questions_and_reports_short_categories():
    bank = list(load_questions(BUNDLED))
    passing = [q for q in bank if q.id not in {"q-001", "q-002", "q-003", "q-004"}]
    ids, problems = select_questions(passing, BUNDLED.manifest.quotas)
    assert not {"q-001", "q-002", "q-003", "q-004"} & set(ids)
    assert problems == ["aggregation: 16 passing questions, below the quota of 17"]


def test_world_selection_meets_its_quotas(world):
    out = run(world, write=False)
    assert out.check is not None
    assert out.check.questions == ("q-001", "q-003", "q-026", "q-071", "q-101")  # seeded
    assert out.check.probes == ("p-id01", "p-iq01")
    assert out.check.as_of == AS_OF


# -- GLD-06 -------------------------------------------------------------------------------------


def test_files_are_written_and_r31_accepts_them(world):
    out = run(world)
    assert out.exit_code == 0 and out.check is not None
    root = world.root
    for qid in ("q-001", "q-002", "q-003", "q-026", "q-071"):
        for engine in ("clickhouse", "snowflake"):
            g = GoldResult.model_validate_json(
                (root / "gold" / engine / f"{qid}.json").read_text(encoding="utf-8")
            )
            assert g.question_id == qid and g.engine == engine
    assert not (root / "gold" / "clickhouse" / "q-101.json").exists()
    history = root / "gold" / "check-20261115T123005Z.json"
    assert json.loads(history.read_text(encoding="utf-8")) == json.loads(
        (root / GOLD_CHECK_FILE).read_text(encoding="utf-8")
    )
    check, selected, gold = require_gold(
        world.pack, load_questions(world.pack), ("clickhouse", "snowflake"), SNAPSHOT
    )
    assert check.gold_check_id == out.check.gold_check_id
    assert [q.id for q in selected] == list(out.check.questions)
    assert len(gold) == 4 * 2 + 3  # selected answer questions on 2 engines, probe gold
    assert {k for k in gold if k[0].startswith("p-")} == {
        ("p-iq01", "clickhouse"),
        ("p-iq01", "snowflake"),
        ("p-id01", "clickhouse"),  # MCP-only probe: ClickHouse only
    }
    assert isinstance(gold[("p-id01", "clickhouse")], ProbeGold)


def test_gold_check_id_is_sha256_over_questions_gold_and_snapshot(world):
    out = run(world)
    assert out.check is not None
    selected = [q for q in world.questions if q.id in out.check.questions]
    assert len(selected) < len([q for q in world.questions])  # q-002 is not selected
    probes = list(world.probes)
    gold: dict[tuple[str, str], AnyGold] = {
        **load_gold_results(world.pack, world.questions, out.check.engines)
    }
    gold.update(load_probe_gold(world.pack, probes, out.check.engines))

    def gid(qs=selected, snap=SNAPSHOT, as_of=AS_OF, ps=probes):
        return compute_gold_check_id(qs, gold, snap, as_of=as_of, probes=ps)

    assert out.check.gold_check_id == gid()
    assert out.check.gold_check_id != gid(qs=world.questions)  # selected questions only
    assert out.check.gold_check_id != gid(snap="cd" * 32)
    assert out.check.gold_check_id != gid(as_of=date(2026, 8, 31))
    assert out.check.gold_check_id != gid(ps=[])


def test_editing_an_unselected_question_keeps_the_check_valid(world):
    out = run(world)
    assert out.check is not None and "q-002" not in out.check.questions
    path = world.root / "questions.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            ln.replace("in 2024?", "in 2024, please?") if '"q-002"' in ln else ln for ln in lines
        )
        + "\n",
        encoding="utf-8",
    )
    require_gold(world.pack, load_questions(world.pack), ("clickhouse", "snowflake"), SNAPSHOT)


def test_run_with_another_as_of_is_refused(world):
    run(world)
    with pytest.raises(PreconditionError, match="not the run's 2026-08-31"):
        require_gold(
            world.pack,
            load_questions(world.pack),
            ("clickhouse",),
            SNAPSHOT,
            as_of=date(2026, 8, 31),
        )


def test_probe_gold_is_run_and_checked(world):
    from agentic_analytics.gold import check_probe

    iq = world.p("p-iq01")
    out = check_probe(iq, world.executors(), as_of=AS_OF)
    assert out.passed and set(out.gold) == {"clickhouse", "snowflake"}
    idp = check_probe(world.p("p-id01"), world.executors(), as_of=AS_OF)
    assert idp.passed and set(idp.gold) == {"clickhouse"}
    assert bound(world.p("p-id01"), "snowflake") not in world.sf.sqls()
    world.ch.set(bound(world.p("p-id01"), "clickhouse"), rs(["town", "sales"]))
    assert check_probe(world.p("p-id01"), world.executors(), as_of=AS_OF).failures == (
        "p-id01 [clickhouse]: no rows",
    )
    write = next(p for p in load_probes(BUNDLED) if p.kind == "write")
    assert check_probe(write, world.executors(), as_of=AS_OF).gold == {}


def test_gold_result_records_the_executed_sql_hash_and_row_cap_result(world):
    import hashlib

    run(world)
    q = world.q("q-026")
    g = GoldResult.model_validate_json(
        (world.root / "gold" / "clickhouse" / "q-026.json").read_text(encoding="utf-8")
    )
    assert g.sql_hash == hashlib.sha256(bound(q, "clickhouse").encode("utf-8")).hexdigest()
    assert g.result.rows == ((850_000,),)


def test_rerun_is_repeatable(world):
    first = run(world)
    second = run(world)
    assert first.check is not None and second.check is not None
    assert first.check.gold_check_id == second.check.gold_check_id


def test_a_failed_check_is_written_as_fail_and_refused_by_r31(world):
    world.sf.set(bound(world.q("q-026"), "snowflake"), rs(["SALES"], [1]))
    out = run(world)
    assert out.exit_code == 1
    check = GoldCheck.model_validate_json(
        (world.root / GOLD_CHECK_FILE).read_text(encoding="utf-8")
    )
    assert check.status == "fail" and check.failures == out.failures
    with pytest.raises(PreconditionError, match="failed"):
        require_gold(world.pack, load_questions(world.pack), ("clickhouse",), SNAPSHOT)


def test_only_checks_one_question_and_writes_nothing(world):
    out = run(world, only="q-071")
    assert out.check is None and out.exit_code == 0
    assert [o.question.id for o in out.outcomes] == ["q-071"]
    assert not (world.root / "gold").exists()
    world.ch.set(
        world.top_sql(),
        rs(
            ["type", "sales"],
            *[list(r) for r in world.ch.answers[world.top_sql()].rows[:3]],
            ["detached", 200_000],
        ),
    )
    assert run(world, only="q-071").exit_code == 1


def test_only_unknown_question_is_a_config_error(world):
    from agentic_analytics.gold import GoldConfigError

    with pytest.raises(GoldConfigError, match="no question 'q-999'"):
        run(world, only="q-999")


def test_single_engine_check_is_stamped_and_accepted_for_that_engine_only(world):
    out = run(world, engines=("clickhouse",))
    assert out.check is not None and out.check.mode == "single_engine"
    require_gold(world.pack, load_questions(world.pack), ("clickhouse",), SNAPSHOT)
    with pytest.raises(PreconditionError, match="does not cover snowflake"):
        require_gold(world.pack, load_questions(world.pack), ("clickhouse", "snowflake"), SNAPSHOT)


def test_a_failing_question_that_leaves_a_category_short_fails_the_check(world):
    # q-026 is the world's only `time` question (quota 1): a mismatch leaves `time` short
    world.sf.set(bound(world.q("q-026"), "snowflake"), rs(["month", "sales"], [1, -1]))
    out = run(world)
    assert out.exit_code == 1 and out.check is not None and out.check.status == "fail"
    assert any(f.startswith("selection: ") and "time" in f for f in out.failures), out.failures


def test_a_probe_gold_lint_issue_fails_the_probe(world):
    path = world.root / "probes.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    i = next(n for n, r in enumerate(rows) if r["id"] == "p-iq01")
    sql = rows[i]["gold_sql"]["clickhouse"]
    rows[i]["gold_sql"]["clickhouse"] = sql.replace(" WHERE ", " WHERE rand() >= 0 AND ", 1)
    assert rows[i]["gold_sql"]["clickhouse"] != sql
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    probe = next(p for p in load_probes(world.pack) if p.id == "p-iq01")
    world.ch.set(bound(probe, "clickhouse"), rs(["sales"], [1]))
    out = run(world)
    assert out.exit_code == 1
    assert any(f.startswith("p-iq01") and "approximate_function" in f for f in out.failures), (
        out.failures
    )


def test_probe_gold_over_200_rows_fails(world):
    many = rs(["town", "sales"], *[[f"T{i}", i] for i in range(201)])
    world.ch.set(bound(world.p("p-id01"), "clickhouse"), many)
    out = run(world)
    assert out.exit_code == 1
    assert "p-id01 [clickhouse]: more than 200 gold rows" in out.failures


def test_two_runs_in_the_same_second_keep_both_history_copies(world):
    first = run(world)
    second = run(world)
    run(world)
    hist = sorted(p.name for p in (world.root / "gold").glob("check-*.json"))
    assert hist == [
        "check-20261115T123005Z-2.json",
        "check-20261115T123005Z-3.json",
        "check-20261115T123005Z.json",
    ]
    assert first.check is not None and second.check is not None
    assert first.check.gold_check_id == second.check.gold_check_id
