"""`agentic-analytics gold` runs the gold check (Q2.2; exit codes 0/1/2 and `--json`)."""

from __future__ import annotations

import json

import pytest

from agentic_analytics import cli
from agentic_analytics.gold import check as gold_check
from agentic_analytics.run.gold import GOLD_CHECK_FILE
from tests.gold_check.fakes import SNAPSHOT, bound, rs
from tests.gold_check.world import make_world

pytestmark = pytest.mark.contract


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = make_world(tmp_path)
    m = tmp_path / "measurements"
    m.mkdir()
    (m / "snapshot-2026-11-01.json").write_text(
        json.dumps({"sha256": SNAPSHOT, "downloaded_at": "2026-11-01T09:00:00+00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gold_check, "configured_executors", lambda environ: w.executors())
    return w


ARGS = ["--pack", "./pack", "--as-of", "2026-09-30", "--measurements", "measurements"]


def last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_gold_passes_writes_and_then_verifies(world, capsys):
    assert cli.main(["gold", "--json", *ARGS]) == 0
    out = last_json(capsys)
    assert out["ok"] and out["status"] == "pass" and out["mode"] == "cross_engine"
    assert out["questions"] == 5 and len(out["gold_check_id"]) == 64
    assert (world.root / GOLD_CHECK_FILE).is_file()
    assert cli.main(["gold", "--verify", "--pack", "./pack", "--json"]) == 0
    assert last_json(capsys)["gold_check_id"] == out["gold_check_id"]


def test_gold_mismatch_exits_1_and_lists_failures(world, capsys):
    world.sf.set(bound(world.q("q-002"), "snowflake"), rs(["MEDIAN_PRICE"], [1.0]))
    assert cli.main(["gold", *ARGS]) == 1
    err = capsys.readouterr().err
    assert "gold check fail" in err and "q-002: cross-engine mismatch" in err


def test_gold_only_checks_one_question(world, capsys):
    assert cli.main(["gold", "--only", "q-071", "--json", *ARGS]) == 0
    out = last_json(capsys)
    assert out == {"ok": True, "failures": [], "only": "q-071"}
    assert not (world.root / "gold").exists()
    assert cli.main(["gold", "--only", "q-999", *ARGS]) == 2


def test_gold_as_of_comes_from_run_yaml(world, capsys):
    from agentic_analytics.run.config import RUN_YAML

    (world.root.parent / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    args = ["--pack", "./pack", "--measurements", "measurements", "--json"]
    assert "as_of: 2026-09-30" in RUN_YAML  # the fake warehouse answers for that as-of
    assert cli.main(["gold", *args]) == 0
    capsys.readouterr()
    assert cli.main(["gold", "--config", "missing.yaml", *args]) == 2
    assert "--as-of" in last_json(capsys)["error"]


def test_gold_config_errors_exit_2(world, monkeypatch, capsys):
    assert cli.main(["gold", "--as-of", "30/09/2026", "--pack", "./pack", "--json"]) == 2
    assert "YYYY-MM-DD" in last_json(capsys)["error"]
    monkeypatch.setattr(gold_check, "configured_executors", lambda environ: {})
    assert cli.main(["gold", "--json", *ARGS]) == 2
    assert "no engine is configured" in last_json(capsys)["error"]


def test_gold_without_a_snapshot_record_refuses_with_exit_1(world, capsys):
    args = ["--pack", "./pack", "--as-of", "2026-09-30", "--measurements", "nowhere", "--json"]
    assert cli.main(["gold", *args]) == 1
    assert "no snapshot record" in last_json(capsys)["error"]


def test_gold_runtime_error_is_exit_2_without_traceback(world, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(world.ch, "execute", boom)
    assert cli.main(["gold", "--json", *ARGS]) == 2
    assert "runtime error: RuntimeError: socket closed" in last_json(capsys)["error"]


def test_verify_without_a_gold_check_exits_1(world, capsys):
    assert cli.main(["gold", "--verify", "--pack", "./pack", "--json"]) == 1
    assert "has no gold check" in last_json(capsys)["error"]


def test_verify_of_a_tampered_check_exits_1(world, capsys):
    assert cli.main(["gold", *ARGS]) == 0
    capsys.readouterr()
    path = world.root / "gold" / "clickhouse" / "q-001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["result"]["rows"] = [[1]]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["gold", "--verify", "--pack", "./pack", "--json"]) == 1
    assert "not clean" in last_json(capsys)["error"]


def test_gold_reads_the_as_of_from_the_run_yaml_file(world, monkeypatch, capsys):
    """A run.yaml whose as-of differs from every default: the check gets the file's date."""
    from agentic_analytics.run.config import RUN_YAML

    text = RUN_YAML.replace("as_of: 2026-09-30", "as_of: 2025-12-31")
    assert text != RUN_YAML
    (world.root.parent / "run.yaml").write_text(text, encoding="utf-8")
    seen = {}

    class Outcome:
        failures, check, written, exit_code = (), None, (), 0

    def fake_run(*args, **kw):
        seen.update(kw)
        return Outcome()

    monkeypatch.setattr(gold_check, "run_gold_check", fake_run)
    assert cli.main(["gold", "--pack", "./pack", "--measurements", "measurements"]) == 0
    assert str(seen["as_of"]) == "2025-12-31"
    assert cli.main(["gold", "--seed", "7", *ARGS]) == 0
    assert seen["seed"] == 7
