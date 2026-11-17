"""ASK-02: replay from the demo cache (no calls), `agentic-analytics demo-cache --questions 10`,
and the bundled SYNTHETIC example."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics import cli
from agentic_analytics.demo import ask as A
from agentic_analytics.demo import cache as C
from tests.demo.example_data import EXAMPLE_FILE, example_text, fake_context
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("replay opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---- replay -------------------------------------------------------------------------------------


def test_replay_makes_no_calls(no_network):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)  # live services exist but must not be used
    cache = C.load_example_cache()
    panels = A.ask_panels(Q1, A.DEMO_BACKENDS, "replay", cache=cache, ctx=ctx)
    assert len(panels) == 2 and counter.total == 0
    assert panels == list(cache.find(Q1).panels)  # type: ignore[union-attr]


def test_replay_matches_whitespace_and_case_but_not_other_questions():
    cache = C.load_example_cache()
    assert A.ask_panels("  how many PROPERTY sales were recorded in england and wales in 2023? ",
                        A.DEMO_BACKENDS, "replay", cache=cache)  # fmt: skip
    with pytest.raises(KeyError, match="use live mode"):
        A.ask_panels("Something never cached", A.DEMO_BACKENDS, "replay", cache=cache)
    with pytest.raises(KeyError, match="gpt_mcp"):
        A.ask_panels(Q1, ("gpt_mcp",), "replay", cache=cache)
    with pytest.raises(KeyError):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "replay")


def test_select_questions_spreads_over_categories():
    qs = w.World.from_bundled().questions
    picked = C.select_questions(qs, 10)
    assert len(picked) == 10
    assert [q.category for q in picked[:6]] == list(C.CATEGORY_ORDER)
    assert len({q.id for q in picked}) == 10
    assert len(C.select_questions(qs, 99)) == len(qs)
    with pytest.raises(ValueError):
        C.select_questions(qs, 0)


def test_cache_round_trips_and_saves_atomically(tmp_path):
    cache = C.load_example_cache()
    path = C.save_cache(tmp_path / "sub" / "replay.json", cache)
    assert C.load_cache(path) == cache
    assert [p.name for p in path.parent.iterdir()] == ["replay.json"]  # no temp file left


def test_a_failed_save_keeps_the_old_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    """Verifier round 1: a write that fails half way (here text with no UTF-8 form) leaves the
    previous cache whole, not truncated, and no temporary file behind."""
    path = C.save_cache(tmp_path / "replay.json", C.load_example_cache())
    before = path.read_bytes()
    monkeypatch.setattr(C, "dump_cache", lambda cache: '{"half": "written \ud800"}')
    with pytest.raises(UnicodeEncodeError):
        C.save_cache(path, C.load_example_cache())
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["replay.json"]


def test_build_skips_complete_entries_and_redo_reasks_failed_ones():
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    qs = C.select_questions(w.World(w.World.from_bundled().questions, []).questions, 10)
    first, asked = C.build_demo_cache(qs, ctx, pack="p@1", attribution="credit")
    assert len(asked) == 10
    before = counter.total
    again, asked2 = C.build_demo_cache(qs, ctx, pack="p@1", attribution="credit", existing=first)
    assert asked2 == [] and counter.total == before and again.entries == first.entries
    failed = [e for e in first.entries if not e.complete(first.backends)]
    assert failed  # the fake world has a Cortex SQL error among the samples
    _, redone = C.build_demo_cache(
        qs, ctx, pack="p@1", attribution="credit", existing=first, redo=True
    )
    assert {e.question for e in redone} == {e.question for e in failed}


# ---- the bundled SYNTHETIC example ---------------------------------------------------------------


def test_bundled_example_matches_its_generator():
    assert EXAMPLE_FILE.read_text(encoding="utf-8") == example_text()


def test_the_example_is_clearly_labelled_synthetic_and_credits_the_data():
    cache = C.load_example_cache()
    assert cache.synthetic and cache.label == C.SYNTHETIC_LABEL
    assert "SYNTHETIC" in cache.label and "fakes" in cache.label
    assert cache.attribution.startswith("Contains HM Land Registry data")
    assert cache.backends == A.DEMO_BACKENDS and len(cache.entries) == 10
    samples = cache.samples()
    assert samples and samples[0] == Q1
    assert all(len(e.panels) == 2 for e in cache.entries)


# ---- the CLI ------------------------------------------------------------------------------------


def _cli_world(monkeypatch, tmp_path: Path, counter: w.Counter):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "run.yaml").write_text(
        "backends: [claude_mcp, cortex_analyst]\nmodels: {claude_mcp: claude-sonnet-5-5}\n"
        "as_of: 2026-09-30\ntrials: 1\n",
        encoding="utf-8",
    )
    w.write_pack(tmp_path / "pack", w.World(w.World.from_bundled().questions, []))
    monkeypatch.setattr(
        "agentic_analytics.run.runner.configured_engines", lambda env: ("clickhouse", "snowflake")
    )
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    calls = []

    def fake_ctx(cfg, pack, environ, **kw):
        calls.append(kw)
        ctx.config = cfg
        return ctx

    monkeypatch.setattr(A, "context_from_env", fake_ctx)
    return calls


def test_demo_cache_command_writes_the_replay_file_and_resumes_for_free(
    monkeypatch, tmp_path, capsys
):
    counter = w.Counter()
    calls = _cli_world(monkeypatch, tmp_path, counter)
    args = ["demo-cache", "--questions", "10", "--pack", "./pack", "--json"]
    assert cli.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["asked"] == 10 and out["to_ask"] == 10
    assert calls == [{"limits": A.DemoLimits(), "trace": True}]  # owner run: traced samples
    cache = C.load_cache(tmp_path / "demo" / "replay.json")
    assert not cache.synthetic and len(cache.entries) == 10
    assert cache.attribution.startswith("Contains HM Land Registry data")
    before = counter.total
    assert cli.main(args) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["to_ask"] == 0 and again["asked"] == 0 and counter.total == before


def test_demo_cache_appends_one_spend_ledger_line_per_run_that_asked(monkeypatch, tmp_path, capsys):
    """Verifier round 1 (Tech Spec §9, D-78): a real `demo-cache` run appends its spend: the
    agent panels' charge as dollars and the Cortex messages' trial credits; a rerun that asks
    nothing appends nothing; no question text."""
    from agentic_analytics.cost import credit_rates

    counter = w.Counter()
    _cli_world(monkeypatch, tmp_path, counter)
    monkeypatch.delenv("AA_SPEND_LEDGER", raising=False)
    args = ["demo-cache", "--questions", "3", "--pack", "./pack", "--json"]
    assert cli.main(args) == 0
    capsys.readouterr()
    ledger = tmp_path / "docs" / "measurements" / "spend-ledger.jsonl"
    (row,) = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    cache = C.load_cache(tmp_path / "demo" / "replay.json")
    agent = sum(p.cost_usd for e in cache.entries for p in e.panels if p.backend == "claude_mcp")
    per_message = credit_rates()["cortex_analyst_credits_per_message"].value
    assert row["kind"] == C.LEDGER_KIND == "demo-cache" and row["run_id"].startswith("demo-cache-")
    assert row["spent_usd"] == pytest.approx(agent, abs=1e-6) and row["spent_usd"] > 0
    assert row["credits"] == {"snowflake": pytest.approx(3 * per_message, abs=1e-6)}
    assert "3 sample questions" in row["note"]
    assert not any(e.question in ledger.read_text(encoding="utf-8") for e in cache.entries)
    assert cli.main(args) == 0  # nothing left to ask: no second line
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_demo_cache_dry_run_and_cost_cap(monkeypatch, tmp_path, capsys):
    counter = w.Counter()
    _cli_world(monkeypatch, tmp_path, counter)
    assert cli.main(["demo-cache", "--pack", "./pack", "--dry-run", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["to_ask"] == 10 and dry["upper_bound_usd"] > 0
    assert cli.main(["demo-cache", "--pack", "./pack", "--max-cost", "0.5", "--json"]) == 3
    capped = json.loads(capsys.readouterr().out)
    assert capped["error"] == "cost_cap"
    # the bound counts every question: one two-panel hold (~$0.74) fits $1, ten do not
    assert cli.main(["demo-cache", "--pack", "./pack", "--max-cost", "1.0", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "cost_cap"
    assert cli.main(["demo-cache", "--pack", "./pack", "--max-cost", "1.0", "--questions", "1",
                     "--dry-run", "--json"]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["upper_bound_usd"] < 1.0
    assert counter.total == 0 and not (tmp_path / "demo" / "replay.json").exists()


def test_demo_cache_configuration_errors_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["demo-cache", "--json"]) == 2
    assert "run.yaml" in json.loads(capsys.readouterr().out)["error"]
    counter = w.Counter()
    _cli_world(monkeypatch, tmp_path, counter)
    assert cli.main(["demo-cache", "--pack", "./pack", "--questions", "0", "--json"]) == 2
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "replay.json").write_text("not json", encoding="utf-8")
    assert cli.main(["demo-cache", "--pack", "./pack", "--json"]) == 2
    assert counter.total == 0


def test_cli_defaults_match_the_module_constants():
    ns, _ = cli.build_parser().parse_known_args(["demo-cache"])
    assert ns.questions == C.DEFAULT_QUESTIONS == 10
    assert Path(ns.out) == C.DEFAULT_OUT
    assert ns.max_cost == C.DEMO_CACHE_MAX_COST_USD
    assert ns.backend.split(",") == list(A.DEMO_BACKENDS)
    assert "demo-cache" in cli.BUILT


# ---- reuse only under the same configuration (stamp) ---------------------------------------------


def _with(ctx, **update):
    from agentic_analytics.schemas import RunConfig

    ctx.config = RunConfig.model_validate({**ctx.config.model_dump(), **update})
    return ctx


@pytest.mark.parametrize(
    ("update", "build_kw", "field"),
    [
        ({"as_of": "2026-08-31"}, {}, "as_of"),
        ({"models": {"claude_mcp": "claude-opus-5-5"}}, {}, "models"),
        ({"effort": "high"}, {}, "effort"),
        ({}, {"pack": "p@2"}, "pack"),
        ({}, {"fix_hash": "b" * 64}, "fix_hash"),
    ],
)
def test_a_rebuild_under_another_config_reuses_nothing(update, build_kw, field):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    qs = C.select_questions(w.World(w.World.from_bundled().questions, []).questions, 3)
    base: dict[str, Any] = {"pack": "p@1", "attribution": "credit", "fix_hash": "a" * 64}
    first, _ = C.build_demo_cache(qs, ctx, **base)
    before = counter.total
    _with(ctx, **update)
    kw: dict[str, Any] = {**base, **build_kw}
    with pytest.raises(C.StaleCache, match=field):  # no silent restamp of old answers
        C.build_demo_cache(qs, ctx, existing=first, **kw)
    assert counter.total == before
    again, asked = C.build_demo_cache(qs, ctx, existing=first, redo=True, **kw)
    assert len(asked) == 3 and counter.total > before
    new = again.stamp()
    assert all(e.stamp == new for e in again.entries) and new != first.stamp()
    assert getattr(new, field) != getattr(first.stamp(), field)


def test_an_unrelated_config_change_still_reuses_entries():
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    qs = C.select_questions(w.World(w.World.from_bundled().questions, []).questions, 3)
    first, _ = C.build_demo_cache(qs, ctx, pack="p@1", attribution="credit")
    before = counter.total
    _with(ctx, seed=123)  # not something an answer depends on
    again, asked = C.build_demo_cache(qs, ctx, pack="p@1", attribution="credit", existing=first)
    assert asked == [] and counter.total == before and again.entries == first.entries


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("config", "as_of"), "2026-06-30"),
        (("config", "effort"), "low"),
        (("config", "models"), {"claude_mcp": "claude-opus-5-5"}),
        (("pack",), "uk_price_paid@9.9"),
        (("fix_hash",), "c" * 64),
    ],
)
def test_a_cache_whose_entries_disagree_with_its_config_is_refused_at_load(tmp_path, path, value):
    data = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    node = data
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    bad = tmp_path / "replay.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="different"):
        C.load_cache(bad)


def test_every_entry_of_the_example_carries_the_caches_stamp():
    cache = C.load_example_cache()
    assert all(e.stamp == cache.stamp() for e in cache.entries)
    assert cache.stamp().as_of.isoformat() == "2026-09-30"


def test_demo_cache_command_refuses_a_changed_config_without_redo(monkeypatch, tmp_path, capsys):
    counter = w.Counter()
    _cli_world(monkeypatch, tmp_path, counter)
    args = ["demo-cache", "--questions", "3", "--pack", "./pack", "--json"]
    assert cli.main(args) == 0
    capsys.readouterr()
    before = counter.total
    run_yaml = tmp_path / "run.yaml"
    text = run_yaml.read_text(encoding="utf-8").replace("2026-09-30", "2026-08-31")
    run_yaml.write_text(text, encoding="utf-8")
    assert cli.main(args) == 2
    err = json.loads(capsys.readouterr().out)["error"]
    assert "as_of" in err and "--redo" in err and counter.total == before
    assert cli.main([*args, "--redo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["to_ask"] == 3 and out["asked"] == 3
    cache = C.load_cache(tmp_path / "demo" / "replay.json")
    assert cache.config.as_of.isoformat() == "2026-08-31"
    assert all(e.stamp.as_of == cache.config.as_of for e in cache.entries)


# ---- the synthetic label ------------------------------------------------------------------------


def test_a_synthetic_cache_gets_the_default_label_or_is_refused():
    data = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    data["label"] = ""
    assert C.DemoCache.model_validate(data).label == C.SYNTHETIC_LABEL
    data["label"] = "Real answers from the owner's run"
    with pytest.raises(ValueError, match="SYNTHETIC"):
        C.DemoCache.model_validate(data)
