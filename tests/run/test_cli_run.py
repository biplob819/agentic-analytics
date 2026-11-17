"""RUN-06 and the CLI wiring of RUN-01/02/05: run, smoke, report, reconcile, pack check, gold,
init; exit codes 0/1/2/3 and `--json` (Tech Spec §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_analytics import cli
from agentic_analytics.run import runner
from agentic_analytics.run.config import ENV_EXAMPLE, RUN_YAML
from agentic_analytics.schemas import RunSummary, load_run_config
from harness_lib import ROOT
from tests.run import world as w

pytestmark = pytest.mark.contract


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def world_cli(cwd, monkeypatch):
    """A run.yaml, a pack directory and measurements in cwd; services replay the fake world."""
    s = w.setup(cwd)
    cfg_text = RUN_YAML.replace("trials: 3", "trials: 1").replace("max_cost_usd: 25.0", "")
    (cwd / "run.yaml").write_text(cfg_text, encoding="utf-8")
    monkeypatch.setattr(runner, "configured_engines", lambda env: ("clickhouse", "snowflake"))
    monkeypatch.setattr(runner, "services_from_env", lambda cfg, pack, env: s.services(cfg))
    return s


def args(*extra: str) -> list[str]:
    return [*extra, "--pack", "./pack", "--measurements", "measurements", "--resamples", "200"]


def last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# -- init ---------------------------------------------------------------------------------------


def test_init_writes_templates_and_refuses_to_overwrite(cwd, capsys):
    assert cli.main(["init", "--json"]) == 0
    assert last_json(capsys)["ok"] is True
    assert (cwd / "run.yaml").read_text(encoding="utf-8") == RUN_YAML
    assert (cwd / ".env.example").read_text(encoding="utf-8") == ENV_EXAMPLE
    (cwd / "run.yaml").write_text("mine", encoding="utf-8")
    assert cli.main(["init"]) == 2
    assert "--force" in capsys.readouterr().err
    assert (cwd / "run.yaml").read_text(encoding="utf-8") == "mine"
    assert cli.main(["init", "--force"]) == 0
    assert (cwd / "run.yaml").read_text(encoding="utf-8") == RUN_YAML


def test_init_templates_are_valid(cwd):
    (cwd / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    cfg = load_run_config(cwd / "run.yaml")
    assert cfg.backends == ("claude_mcp", "gpt_mcp", "cortex_analyst") and cfg.trials == 3


def test_env_template_is_the_repository_env_example():
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == ENV_EXAMPLE


# -- pack check -------------------------------------------------------------------------------


def test_pack_check_bundled_pack_passes(capsys):
    assert cli.main(["pack", "check", "--json"]) == 0
    out = last_json(capsys)
    assert out["ok"] is True and out["questions"] == 120 and out["pack"] == "uk_price_paid@1.0"


def test_pack_check_reports_a_broken_pack(cwd, capsys):
    w.write_pack(cwd / "pack", w.World.from_bundled())
    (cwd / "pack" / "questions.jsonl").write_text("{not json\n", encoding="utf-8")
    assert cli.main(["pack", "check", "--pack", "./pack"]) == 1
    assert "questions.jsonl" in capsys.readouterr().out


def test_pack_without_action_is_a_usage_error(capsys):
    assert cli.main(["pack", "--json"]) == 2
    assert "pack check" in last_json(capsys)["error"]


# -- gold ---------------------------------------------------------------------------------------


def test_gold_without_an_engine_is_a_config_error(capsys, monkeypatch):
    from agentic_analytics.gold import check as gold_check

    monkeypatch.setattr(gold_check, "configured_executors", lambda environ: {})
    assert cli.main(["gold"]) == 2
    assert "no engine is configured" in capsys.readouterr().err


def test_gold_validates_an_existing_gold_check(cwd, capsys):
    w.write_pack(cwd / "pack", w.World.from_bundled())
    assert cli.main(["gold", "--verify", "--pack", "./pack", "--json"]) == 0
    assert last_json(capsys)["mode"] == "cross_engine"
    p = next((cwd / "pack" / "gold" / "clickhouse").glob("*.json"))
    data = json.loads(p.read_text(encoding="utf-8"))
    data["sql_hash"] = "0" * 64
    p.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["gold", "--verify", "--pack", "./pack"]) == 1


def test_gold_with_a_missing_pack_is_a_config_error(capsys):
    assert cli.main(["gold", "--pack", "./nowhere", "--json"]) == 2
    assert "does not exist" in last_json(capsys)["error"]


# -- run ----------------------------------------------------------------------------------------


def test_run_dry_run_json(world_cli, capsys):
    assert cli.main(["run", "--dry-run", "--json", *args()]) == 0
    out = last_json(capsys)
    assert out["ok"] and out["called"] == 0 and out["estimate_usd"] > 0
    assert world_cli.counter.total == 0


def test_run_then_rescore_then_report(world_cli, cwd, capsys):
    assert cli.main(["run", "--backend", "all", "--fix", "none", "--json", *args()]) == 0
    first = last_json(capsys)
    assert first["called"] == first["jobs"] == 42
    calls = world_cli.counter.total
    assert cli.main(["run", "--rescore", "--json", *args()]) == 0
    again = last_json(capsys)
    assert again["called"] == 0 and again["cached"] == 42 and world_cli.counter.total == calls
    assert cli.main(["report", "--run", "latest", "--json"]) == 0
    path = Path(last_json(capsys)["path"])
    assert path.name == "index.html" and path.parent.name == again["run_id"]
    assert cli.main(["report", "--run", first["run_id"]]) == 0


def test_run_text_output_prints_the_estimate_first(world_cli, capsys):
    assert cli.main(["run", "--backend", "claude_mcp", *args()]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "Estimate $" in out[0] and out[-1].startswith("run ")


def test_run_over_the_cap_exits_3(world_cli, capsys):
    assert cli.main(["run", "--max-cost", "0.01", "--json", *args()]) == 3
    assert "cap" in last_json(capsys)["message"] and world_cli.counter.total == 0


def test_run_refuses_without_gold_with_exit_1(world_cli, cwd, capsys):
    (cwd / "pack" / "gold" / "gold_check.json").unlink()
    assert cli.main(["run", "--json", *args()]) == 1
    assert "refusing to start" in last_json(capsys)["message"]


def test_run_config_errors_exit_2(world_cli, cwd, capsys, monkeypatch):
    assert cli.main(["run", "--backend", "nope", *args()]) == 2
    assert cli.main(["run", "--fix", "nope", *args()]) == 2
    assert cli.main(["run", "--trials", "0", *args()]) == 2
    assert cli.main(["run", "--config", "missing.yaml", "--json", *args()]) == 2
    assert "init" in last_json(capsys)["error"]
    monkeypatch.setattr(runner, "configured_engines", lambda env: ())
    assert cli.main(["run", "--json", *args()]) == 2
    assert "no engine is configured" in last_json(capsys)["error"]


def test_run_all_backends_keeps_only_configured_engines(world_cli, monkeypatch, capsys):
    w.write_pack(world_cli.pack_dir, world_cli.world, engines=("clickhouse",))
    w.write_measurements(world_cli.tmp / "measurements", engines=("clickhouse",))
    world_cli.engines = ("clickhouse",)
    monkeypatch.setattr(runner, "configured_engines", lambda env: ("clickhouse",))
    assert cli.main(["run", "--backend", "all", "--json", *args()]) == 0
    out = last_json(capsys)
    assert out["gold_mode"] == "single_engine" and out["jobs"] == 2 * 14


def test_run_without_accounts_names_the_missing_variables(cwd, capsys, monkeypatch):
    w.setup(cwd)
    (cwd / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    monkeypatch.setattr(runner, "configured_engines", lambda env: ("clickhouse", "snowflake"))
    for name in ("CLICKHOUSE_MCP_KEY_ID", "CLICKHOUSE_MCP_KEY_SECRET", "CLICKHOUSE_SERVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["run", "--max-cost", "100", "--json", *args()]) == 2
    assert "CLICKHOUSE_MCP_KEY_ID" in last_json(capsys)["message"]


def test_runs_need_the_frozen_fixes(world_cli, cwd, capsys):
    argv = ["run", "--backend", "claude_mcp", "--fix", "examples", "--json", *args()]
    assert cli.main(argv) == 0
    assert last_json(capsys)["jobs"] == 2 * 14
    (cwd / "pack" / "fixes" / "FROZEN.json").unlink()
    base = ["run", "--backend", "claude_mcp", "--json", *args()]
    assert cli.main(base) == 1  # baselines refuse without a freeze too (D-13, D-52)
    assert "FROZEN.json" in last_json(capsys)["message"]


def test_force_fix_hash_reaches_the_runner(world_cli, cwd, capsys):
    from agentic_analytics.fixes import freeze_fixes
    from agentic_analytics.packs import load_pack

    assert cli.main(["run", "--backend", "claude_mcp", "--json", *args()]) == 0
    capsys.readouterr()
    p = cwd / "pack" / "fixes" / "examples.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# reviewed\n", encoding="utf-8")
    freeze_fixes(load_pack(cwd / "pack"), force=True, reason="D-99")
    argv = ["run", "--backend", "claude_mcp", "--fix", "examples", "--json", *args()]
    assert cli.main(argv) == 1
    assert "D-52" in last_json(capsys)["message"]
    assert cli.main([*argv, "--force-fix-hash", "owner ruling D-99"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["init", "--dir", "a-file"],
        ["report", "--run", "broken"],
        ["reconcile", "--run", "broken", "--credits", "credits.json"],
        ["pack", "check"],
    ],
)
def test_unexpected_errors_in_any_command_exit_2_on_one_line(cwd, capsys, monkeypatch, argv):
    from agentic_analytics import questions

    (cwd / "a-file").write_text("not a folder", encoding="utf-8")
    broken = cwd / "results" / "broken"
    broken.mkdir(parents=True)
    for name in ("run.json", "records.jsonl", "questions.jsonl", "summary.json"):
        (broken / name).write_text("{not json", encoding="utf-8")
    (cwd / "credits.json").write_text('{"snowflake": 1}', encoding="utf-8")

    def explode(*a, **k):
        raise RuntimeError("unexpected\nmulti-line failure")

    monkeypatch.setattr(questions, "check_pack", explode)
    assert cli.main([*argv, "--json"]) == 2
    out = capsys.readouterr()
    assert "Traceback" not in out.out + out.err
    lines = out.out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["ok"] is False
    assert cli.main(argv) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "Traceback" not in err[0]


@pytest.mark.safety
def test_cli_error_messages_are_redacted(world_cli, capsys, monkeypatch):
    key = "sk-ant-api03-" + "Z" * 40

    def leaky(*a, **k):
        raise RuntimeError(f"provider said: bad key {key}")

    monkeypatch.setattr(runner, "run_benchmark", leaky)
    assert cli.main(["run", "--json", *args()]) == 2
    out = capsys.readouterr()
    assert key not in out.out + out.err and "REDACTED" in out.out


def test_dry_run_prints_the_refusal_once(cwd, capsys, monkeypatch):
    from agentic_analytics.warehouse.doctor import ENV_VARS

    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["init"]) == 0
    capsys.readouterr()
    assert cli.main(["run", "--dry-run"]) == 0
    out = capsys.readouterr()
    assert (out.out + out.err).count("would refuse") == 1


@pytest.mark.parametrize("pack", ["uk_price_paid", "./pack"])
def test_dry_run_needs_no_accounts_or_records(cwd, capsys, monkeypatch, pack):
    """OSS-03: the estimate from the config and pack alone; no calls, exit 0."""
    from agentic_analytics.warehouse.doctor import ENV_VARS

    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    w.write_pack(cwd / "pack", w.World.from_bundled(), with_check=False)
    assert cli.main(["init"]) == 0
    capsys.readouterr()

    def no_services(*a, **k):
        raise AssertionError("a dry run builds no services")

    monkeypatch.setattr(runner, "services_from_env", no_services)
    assert cli.main(["run", "--dry-run", "--pack", pack, "--json"]) == 0
    out = last_json(capsys)
    assert out["ok"] and out["called"] == 0 and out["estimate_usd"] > 0
    assert "would refuse to start" in out["message"]
    n = 100 if pack == "uk_price_paid" else 12  # the bundled quotas select 100
    probes = 20 if pack == "uk_price_paid" else 2
    assert out["jobs"] >= 3 * 3 * n + probes
    assert not (cwd / "results").exists() and not (cwd / ".cache").exists()
    assert cli.main(["run", "--dry-run", "--fix", "all", "--pack", pack]) == 0
    assert cli.main(["run", "--pack", pack, "--json"]) == 2  # a real run needs an engine


def test_runner_accepts_the_gold_check_q22_writes(tmp_path):
    """Q2.2's `run_gold_check` output satisfies RUN-04's gold precondition as written."""
    from agentic_analytics.gold import run_gold_check
    from tests.gold_check.fakes import AS_OF, SNAPSHOT
    from tests.gold_check.world import make_world

    gw = make_world(tmp_path)
    w.copy_bundled_fixes(gw.root)
    gold = run_gold_check(
        gw.pack,
        gw.questions,
        gw.executors(),  # type: ignore[arg-type]
        as_of=AS_OF,
        snapshot_sha256=SNAPSHOT,
    )
    assert gold.check is not None and gold.check.status == "pass", gold.failures
    w.write_measurements(tmp_path / "measurements", snapshot=SNAPSHOT)
    cfg = w.config(backends=("claude_mcp",), as_of=AS_OF)
    said: list[str] = []
    out = runner.run_benchmark(
        cfg,
        gw.pack,
        services=None,
        options=w.options(tmp_path, dry_run=True, include_probes=False, say=said.append),
    )
    assert out.exit_code == 0 and "refuse" not in out.message, out.message
    assert out.jobs == len(gold.check.questions)


def test_unknown_option_on_a_built_command_is_an_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--bogus"])
    assert exc.value.code == 2


# -- smoke ----------------------------------------------------------------------------------------


def test_smoke_runs_two_questions_per_category_on_every_backend(world_cli, cwd, capsys):
    assert cli.main(["smoke", "--json", *args()]) == 0
    out = last_json(capsys)
    assert out["jobs"] == 12 * 3 and out["run_id"].startswith("smoke-")
    assert (cwd / "measurements" / "cost-assumptions.json").is_file()
    run_json = json.loads((cwd / "results" / out["run_id"] / "run.json").read_text("utf-8"))
    assert run_json["config"]["trials"] == 1 and run_json["config"]["max_cost_usd"] == 2.0


# -- report and reconcile ----------------------------------------------------------------------


def test_report_example_needs_no_keys(cwd, capsys, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SNOWFLAKE_ACCOUNT", "CLICKHOUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["report", "--example", "--json"]) == 0
    path = Path(last_json(capsys)["path"])
    assert path.is_file() and "example-synthetic" in str(path)
    assert any((path.parent / "traces").iterdir())


def test_report_without_runs_exits_2(cwd, capsys):
    assert cli.main(["report", "--json"]) == 2
    assert "no runs yet" in last_json(capsys)["error"]


def test_reconcile_allocates_credits_by_elapsed_time(world_cli, cwd, capsys):
    assert cli.main(["run", "--json", *args()]) == 0
    run_id = last_json(capsys)["run_id"]
    run_dir = cwd / "results" / run_id
    before = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    assert before.cost_status == "estimated"
    (cwd / "credits.json").write_text(
        json.dumps({"snowflake": 0.5, "clickhouse": 0.25}), encoding="utf-8"
    )
    assert cli.main(["reconcile", "--credits", "credits.json", "--json"]) == 0
    out = last_json(capsys)
    assert out["cost_status"] == "reconciled"
    after = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    assert after.cost_status == "reconciled"
    assert after.total_credits["clickhouse"] == pytest.approx(0.25)
    assert after.total_cost_usd > before.total_cost_usd
    _, records, _ = runner.load_run(run_dir)
    ch = [r for r in records if r.backend != "cortex_analyst"]
    assert sum(r.warehouse_credits_alloc or 0 for r in ch) == pytest.approx(0.25)
    total_ms = sum(r.warehouse_elapsed_ms for r in ch)
    for r in ch:
        assert r.warehouse_credits_alloc == pytest.approx(0.25 * r.warehouse_elapsed_ms / total_ms)


@pytest.mark.parametrize(
    ("credits", "why"),
    [
        ({"snowflake": 0.5}, "no credits given for clickhouse"),
        ({"snowflake": -1, "clickhouse": 1}, "finite, non-negative number"),
        ({"snowflake": "x", "clickhouse": 1}, "finite, non-negative number"),
        ({"snowflake": True, "clickhouse": 1}, "finite, non-negative number"),
        ({"bigquery": 1}, "unknown engine 'bigquery'"),
        ([], "must be a JSON object"),
        ("x", "must be a JSON object"),
    ],
)
def test_reconcile_rejects_bad_credit_files(world_cli, cwd, capsys, credits, why):
    assert cli.main(["run", "--json", *args()]) == 0
    capsys.readouterr()
    (cwd / "credits.json").write_text(json.dumps(credits), encoding="utf-8")
    assert cli.main(["reconcile", "--credits", "credits.json", "--json"]) == 2
    out = last_json(capsys)
    assert out["ok"] is False and why in out["error"]  # the reason, not a catch-all


def test_makefile_run_arguments_parse():
    ns, rest = cli.build_parser().parse_known_args(
        ["run", "--backend", "all", "--fix", "none", "--trials", "3"]
    )
    assert rest == [] and ns.backend == "all" and ns.fix == "none" and ns.trials == 3


def test_unexpected_runtime_errors_exit_2_without_a_traceback(world_cli, monkeypatch, capsys):
    def broken(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(runner, "run_benchmark", broken)
    assert cli.main(["run", "--json", *args()]) == 2
    assert "disk on fire" in last_json(capsys)["error"]


def test_smoke_draws_from_the_gold_selection_with_one_trial_and_no_fixes(
    world_cli, cwd, capsys, monkeypatch
):
    """Smoke ignores the configured trials and fixes, and only asks gold-selected questions."""
    from agentic_analytics.run import gold as gold_mod

    text = (cwd / "run.yaml").read_text(encoding="utf-8")
    text = text.replace("trials: 1", "trials: 3").replace("fixes: []", "fixes: [examples]")
    (cwd / "run.yaml").write_text(text, encoding="utf-8")
    real = gold_mod.load_gold_check
    dropped: list[str] = []

    def fewer(pack):
        check = real(pack)
        assert check is not None
        if dropped:  # the runner's own precondition check reads the real record
            return check
        dropped.append(sorted(check.questions)[-1])  # one category keeps a single question
        return check.model_copy(update={"questions": tuple(sorted(check.questions)[:-1])})

    monkeypatch.setattr(gold_mod, "load_gold_check", fewer)
    assert cli.main(["smoke", "--json", *args()]) == 0
    out = last_json(capsys)
    assert out["jobs"] == 11 * 3
    run_json = json.loads((cwd / "results" / out["run_id"] / "run.json").read_text("utf-8"))
    assert run_json["config"]["trials"] == 1 and run_json["config"]["fixes"] == []
    recs = (cwd / "results" / out["run_id"] / "records.jsonl").read_text("utf-8").splitlines()
    assert dropped and all(json.loads(r)["question_id"] != dropped[0] for r in recs)


def test_a_long_secret_through_the_cli_catch_alls_is_redacted_before_the_cut(
    world_cli, monkeypatch, capsys
):
    from agentic_analytics.gold import check as gold_check

    secret = "sk-ant-api03-" + "U" * 400

    def broken(*a, **k):
        raise RuntimeError(f"exploded with {secret}")

    monkeypatch.setattr(runner, "run_benchmark", broken)
    assert cli.main(["run", "--json", *args()]) == 2
    out = capsys.readouterr()
    assert "UUUUUUUUUU" not in out.out + out.err
    monkeypatch.setattr(gold_check, "configured_executors", lambda environ: {"clickhouse": 1})
    monkeypatch.setattr(gold_check, "run_gold_check", broken)
    gold_args = [
        "gold",
        "--as-of",
        "2026-08-31",
        "--pack",
        "./pack",
        "--measurements",
        "measurements",
    ]
    assert cli.main([*gold_args, "--json"]) == 2
    out = capsys.readouterr()
    assert "UUUUUUUUUU" not in out.out + out.err and "runtime error" in out.out + out.err
