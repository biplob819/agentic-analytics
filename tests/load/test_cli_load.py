"""N1.3 CLI wiring: `agentic-analytics load` on the stateful fakes (no network, no accounts)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics import cli
from agentic_analytics.load import cli as load_cli
from agentic_analytics.load._common import LoaderError
from agentic_analytics.packs import load_pack
from agentic_analytics.packs.uk_price_paid.transforms import apply_row
from tests.load.fakes import FakeClickHouse, FakeSnowflake, apply_nullable, fixture_download

ENV = {
    "CLICKHOUSE_HOST": "ch.example",
    "CLICKHOUSE_ADMIN_USER": "default",
    "CLICKHOUSE_ADMIN_PASSWORD": "pw-ch-admin-0123456789",
    "SNOWFLAKE_ACCOUNT": "ACME-XY",
    "SNOWFLAKE_ADMIN_USER": "AA_ADMIN_USER",
    "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH": "/keys/admin.p8",
    "SNOWFLAKE_WAREHOUSE": "ANALYTICS_XS",
}
SNAP_FILE = "pp-complete.csv"
REAL_CLI_LOAD = load_cli.cli_load  # before any test patches it


class Clock:
    """A clock that moves forward a fixed step on every call (a load takes time)."""

    def __init__(self, step: dt.timedelta = dt.timedelta(seconds=1)) -> None:
        self.t = dt.datetime(2026, 11, 17, 9, 0, tzinfo=dt.UTC)
        self.step = step

    def __call__(self) -> dt.datetime:
        self.t += self.step
        return self.t


class World:
    """Both fake engines, kept across invocations so reruns see the loaded tables."""

    def __init__(self, sf_transform: Callable[[list[Any]], dict[str, object]] = apply_nullable):
        self.pack = load_pack("uk_price_paid")
        self.ch = FakeClickHouse(self.pack)
        self.sf = FakeSnowflake(self.pack, transform=sf_transform)
        self.download = fixture_download()
        self.opened: list[str] = []
        self.clock = Clock()
        self.free = 10**12

    def deps(self, **over: Any) -> dict[str, Any]:
        def ch(env: Any) -> FakeClickHouse:
            self.opened.append("clickhouse")
            return self.ch

        def sf(env: Any) -> FakeSnowflake:
            self.opened.append("snowflake")
            return self.sf

        base = {
            "clickhouse": ch,
            "snowflake": sf,
            "download": self.download,
            "size_of": lambda url: 4000,
            "free": lambda path: self.free,
            "now": self.clock,
        }
        return base | over


def ns(tmp_path: Path, *args: str) -> argparse.Namespace:
    parsed = cli.build_parser().parse_args(
        [
            "load",
            "--workdir",
            str(tmp_path / "data" / "snapshots"),
            "--measurements",
            str(tmp_path / "m"),
            "--config",
            str(tmp_path / "run.yaml"),
            *args,
        ]
    )
    if not hasattr(parsed, "json"):
        parsed.json = False
    return parsed


def run(world: World, tmp_path: Path, *args: str, env: dict[str, str] | None = None, **deps: Any):
    said: list[str] = []
    code, payload, text = load_cli.cli_load(
        ns(tmp_path, *args), ENV if env is None else env, say=said.append, deps=world.deps(**deps)
    )
    return code, payload, text, said


@pytest.fixture
def world() -> World:
    return World()


def test_load_both_engines_writes_snapshot_parity_and_as_of(world, tmp_path) -> None:
    (tmp_path / "run.yaml").write_text("backends: [claude_mcp]\ntrials: 3\n", encoding="utf-8")
    code, payload, text, said = run(world, tmp_path)
    assert code == 0, text
    assert payload["ok"] is True
    assert {e: r["action"] for e, r in payload["loads"].items()} == {
        "clickhouse": "loaded",
        "snowflake": "loaded",
    }
    assert payload["loads"]["clickhouse"]["rows_after"] == {"uk.uk_price_paid": 30}
    assert payload["loads"]["snowflake"]["rows_after"] == {"ANALYTICS.UK.uk_price_paid": 30}
    assert payload["probes"] == {
        "clickhouse": {"uk_probes.listing_notes": 10},
        "snowflake": {"ANALYTICS.UK_PROBES.listing_notes": 10},
    }
    # snapshot record: the sha256 of the bytes downloaded, in the measurements folder
    snap = json.loads(Path(payload["snapshot_record"]).read_text(encoding="utf-8"))
    assert snap["sha256"] == payload["snapshot"]["sha256"]
    assert Path(payload["snapshot_record"]).name == "snapshot-2026-11-17.json"
    assert (tmp_path / "data" / "snapshots" / SNAP_FILE).is_file()
    # parity: pass, written, stamped with that sha256
    report = json.loads(Path(payload["parity"]["path"]).read_text(encoding="utf-8"))
    assert report["status"] == "pass" == payload["parity"]["status"]
    assert report["snapshot_sha256"] == snap["sha256"]
    assert report["engines"] == ["clickhouse", "snowflake"]
    # as-of (D-50): the fixture's latest sale is 2026-10-14, so 2026-09-30, kept with other keys
    assert payload["max_date"] == "2026-10-14"
    assert payload["as_of"] == "2026-09-30"
    config = (tmp_path / "run.yaml").read_text(encoding="utf-8")
    assert "as_of: 2026-09-30" in config and "trials: 3" in config
    assert payload["warnings"] == []
    assert any("downloading" in s for s in said)


def test_clickhouse_loads_before_snowflake_and_the_window_is_checked(world, tmp_path) -> None:
    """D-11: the Snowflake download is checked against this run's ClickHouse load time."""
    order: list[str] = []
    real_command = world.ch.command

    def command(sql: str, settings: Any = None) -> Any:
        if sql.lstrip().upper().startswith("INSERT INTO UK.UK_PRICE_PAID"):
            order.append("clickhouse insert")
        return real_command(sql, settings)

    world.ch.command = command  # type: ignore[method-assign]

    def download(url: str, dest: Path) -> None:
        order.append("snowflake download")
        world.download(url, dest)

    code, _, text, _ = run(world, tmp_path, download=download)
    assert code == 0, text
    assert order == ["clickhouse insert", "snowflake download"]


def test_a_download_outside_the_hour_of_the_clickhouse_load_stops(world, tmp_path) -> None:
    world.clock.step = dt.timedelta(minutes=40)  # each clock read moves 40 minutes
    code, payload, text, _ = run(world, tmp_path)
    assert code == 2
    assert "D-11" in text and payload["ok"] is False
    assert not any(s.lstrip().startswith("PUT") for s in world.sf.statements)
    assert not (tmp_path / "m").exists() or not list((tmp_path / "m").glob("parity-*.json"))


def test_rerun_skips_both_loads_without_downloading(world, tmp_path) -> None:
    assert run(world, tmp_path)[0] == 0
    code, payload, text, _ = run(world, tmp_path)
    assert code == 0, text
    assert {r["action"] for r in payload["loads"].values()} == {"skipped"}
    assert len(world.download.calls) == 1
    assert payload["snapshot_record"] is None
    # the parity record still names the snapshot the tables were loaded from
    first = json.loads(next((tmp_path / "m").glob("snapshot-*.json")).read_text(encoding="utf-8"))
    assert payload["snapshot"]["sha256"] == first["sha256"]
    report = json.loads(Path(payload["parity"]["path"]).read_text(encoding="utf-8"))
    assert report["snapshot_sha256"] == first["sha256"]
    assert world.ch.count("uk.uk_price_paid") == 30


def test_a_skipped_run_stamps_parity_with_the_newest_snapshot_record(world, tmp_path) -> None:
    """Verifier: with several snapshot records, the newest by download time names the data."""
    assert run(world, tmp_path)[0] == 0
    newest = json.loads(next((tmp_path / "m").glob("snapshot-*.json")).read_text(encoding="utf-8"))
    older = dict(newest, sha256="0" * 64, downloaded_at="2026-10-01T09:00:00+00:00")
    (tmp_path / "m" / "snapshot-2026-10-01.json").write_text(json.dumps(older), encoding="utf-8")
    same_day = dict(newest, sha256="f" * 64, downloaded_at="2026-11-17T08:00:00+00:00")
    (tmp_path / "m" / "snapshot-2026-11-17-early.json").write_text(
        json.dumps(same_day), encoding="utf-8"
    )
    code, payload, text, _ = run(world, tmp_path)
    assert code == 0, text
    assert {r["action"] for r in payload["loads"].values()} == {"skipped"}
    assert payload["snapshot"]["sha256"] == newest["sha256"]  # 09:00:xx on 2026-11-17 is newer
    report = json.loads(Path(payload["parity"]["path"]).read_text(encoding="utf-8"))
    assert report["snapshot_sha256"] == newest["sha256"]


def test_force_reloads_both_engines_with_a_fresh_download(world, tmp_path) -> None:
    assert run(world, tmp_path)[0] == 0
    code, payload, text, _ = run(world, tmp_path, "--force")
    assert code == 0, text
    assert {r["action"] for r in payload["loads"].values()} == {"reloaded"}
    assert len(world.download.calls) == 2
    assert world.ch.count("uk.uk_price_paid") == 30
    assert world.sf.count("ANALYTICS.UK.uk_price_paid") == 30


def test_snowflake_reloaded_alone_warns_that_the_window_was_not_checked(world, tmp_path) -> None:
    assert run(world, tmp_path)[0] == 0
    world.sf.tables["ANALYTICS.UK.uk_price_paid"] = []  # Snowflake emptied, ClickHouse kept
    code, payload, text, _ = run(world, tmp_path)
    assert code == 0, text
    assert payload["loads"]["clickhouse"]["action"] == "skipped"
    assert payload["loads"]["snowflake"]["action"] == "loaded"
    assert len(payload["warnings"]) == 1 and "D-11" in payload["warnings"][0]
    assert "warning:" in text


@pytest.mark.parametrize("choice", ["all", "both"])
def test_all_and_both_mean_every_pack_engine(choice) -> None:
    pack = load_pack("uk_price_paid")
    assert load_cli.chosen_engines(choice, pack) == ("clickhouse", "snowflake")


def test_engine_clickhouse_loads_only_clickhouse_and_parity_is_single_engine(
    world, tmp_path
) -> None:
    code, payload, text, _ = run(world, tmp_path, "--engine", "clickhouse")
    assert code == 0, text
    assert world.opened == ["clickhouse"]
    assert list(payload["loads"]) == ["clickhouse"]
    assert payload["parity"]["status"] == "single_engine"
    assert world.download.calls == []
    assert world.sf.statements == []
    assert payload["probes"] == {"clickhouse": {"uk_probes.listing_notes": 10}}


def test_engine_snowflake_needs_only_the_snowflake_variables(world, tmp_path) -> None:
    env = {k: v for k, v in ENV.items() if k.startswith("SNOWFLAKE_")}
    code, payload, text, _ = run(world, tmp_path, "--engine", "snowflake", env=env)
    assert code == 0, text
    assert world.opened == ["snowflake"]
    assert payload["parity"]["status"] == "single_engine"
    assert payload["warnings"] == []


def test_engine_outside_the_choices_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["load", "--engine", "postgres"])


def test_dry_run_checks_every_statement_and_connects_to_nothing(world, tmp_path) -> None:
    code, payload, text, _ = run(world, tmp_path, "--dry-run", env={})
    assert code == 0, text
    assert world.opened == [] and world.download.calls == []
    assert not (tmp_path / "m").exists() and not (tmp_path / "data").exists()
    assert payload["dry_run"] is True
    assert payload["steps"]["clickhouse"] == ["create_database", "create_table", "insert"]
    assert payload["steps"]["snowflake"][-4:] == ["put", "copy", "truncate", "insert"]
    assert payload["steps"]["snowflake_probes"][-1] == "insert"
    assert payload["parity_fingerprints"] == 17
    assert payload["source_url"].endswith("/pp-complete.csv")
    assert set(payload["missing_env"]) == set(ENV)
    assert "nothing was sent" in text


def test_dry_run_refuses_a_pack_statement_outside_the_allow_list(tmp_path) -> None:
    """The dry run is the loaders' own check: a smuggled DROP stops it with exit 2."""
    import shutil
    from importlib import resources

    src = resources.files("agentic_analytics.packs") / "uk_price_paid"
    pack_dir = tmp_path / "pack"
    with resources.as_file(src) as p:
        shutil.copytree(p, pack_dir)
    sql = pack_dir / "load" / "clickhouse.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8") + "\nDROP TABLE {database}.uk_price_paid;\n",
        encoding="utf-8",
    )
    code, payload, _, _ = run(World(), tmp_path, "--dry-run", "--pack", str(pack_dir))
    assert code == 2 and payload["ok"] is False


def test_missing_admin_variables_are_named_without_values(world, tmp_path) -> None:
    env = dict(ENV)
    del env["CLICKHOUSE_ADMIN_PASSWORD"], env["SNOWFLAKE_WAREHOUSE"]
    code, payload, text, _ = run(world, tmp_path, env=env)
    assert code == 2
    assert payload["missing_env"] == ["CLICKHOUSE_ADMIN_PASSWORD", "SNOWFLAKE_WAREHOUSE"]
    assert world.opened == []
    for value in env.values():
        assert value not in text


def test_parity_failure_exits_1_and_names_the_fingerprint(tmp_path) -> None:
    def price_plus_one(raw: list[Any]) -> dict[str, object]:
        row = apply_row(raw)
        row["price"] = int(row["price"]) + 1  # type: ignore[call-overload]
        return row

    world = World(sf_transform=price_plus_one)
    (tmp_path / "run.yaml").write_text("trials: 3\n", encoding="utf-8")
    code, payload, text, _ = run(world, tmp_path)
    assert code == 1
    assert payload["ok"] is False and payload["parity"]["status"] == "fail"
    assert [f["name"] for f in payload["parity"]["failed"]] == ["sum_price"]
    assert "FAILED sum_price" in text
    assert payload["as_of"] is None
    assert "as_of" not in (tmp_path / "run.yaml").read_text(encoding="utf-8")


def test_not_enough_disk_space_stops_before_downloading(world, tmp_path) -> None:
    world.free = load_cli.needed_bytes(4000) - 1
    code, payload, text, _ = run(world, tmp_path)
    assert code == 2
    assert "not enough disk space" in text
    assert world.download.calls == []
    assert not any(s.lstrip().startswith("PUT") for s in world.sf.statements)
    # ClickHouse had already loaded; its result is still reported
    assert payload["loads"]["clickhouse"]["action"] == "loaded"


def test_disk_space_exactly_enough_downloads(world, tmp_path) -> None:
    world.free = load_cli.needed_bytes(4000)
    assert run(world, tmp_path)[0] == 0
    assert len(world.download.calls) == 1


def test_needed_bytes_covers_the_csv_and_its_gzip_copy() -> None:
    assert load_cli.needed_bytes(10_000_000_000) == 15_000_000_000 + (1 << 30)


def test_connections_are_closed_after_the_load(world, tmp_path) -> None:
    closed: list[str] = []
    world.ch.close = lambda: closed.append("clickhouse")  # type: ignore[attr-defined]
    world.sf.close = lambda: closed.append("snowflake")  # type: ignore[attr-defined]
    assert run(world, tmp_path)[0] == 0
    assert sorted(closed) == ["clickhouse", "snowflake"]


def test_json_quiet_mode_prints_no_progress(world, tmp_path) -> None:
    said: list[str] = []
    parsed = ns(tmp_path)
    parsed.json = True
    code, _, _ = load_cli.cli_load(parsed, ENV, say=said.append, deps=world.deps())
    assert code == 0 and said == []


# ---- through cli.main: --json, exit codes and redaction ---------------------------------------


@pytest.fixture
def main_world(world, tmp_path, monkeypatch) -> World:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)  # no .env is read from the repo
    deps = world.deps()
    real = load_cli.cli_load

    def with_fakes(ns_: Any, environ: Any, *, say: Any = None, deps_: Any = None) -> Any:
        return real(ns_, environ, say=say, deps=deps)

    monkeypatch.setattr(load_cli, "cli_load", with_fakes)
    return world


def test_main_load_json_output(main_world, tmp_path, capsys) -> None:
    assert cli.main(["load", "--json", "--measurements", "m", "--workdir", "data/s"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["parity"]["status"] == "pass"
    assert out["as_of"] == "2026-09-30"


def test_main_top_level_json_and_makefile_spelling(main_world, capsys) -> None:
    assert cli.main(["--json", "load", "--engine", "all", "--measurements", "m"]) == 0
    assert json.loads(capsys.readouterr().out)["engines"] == ["clickhouse", "snowflake"]


def test_main_parity_failure_exit_code(world, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    def failing(*a: Any, **k: Any) -> Any:
        return 1, {"ok": False, "parity": {"status": "fail"}}, "parity: fail"

    monkeypatch.setattr(load_cli, "cli_load", failing)
    assert cli.main(["load"]) == 1
    assert "parity: fail" in capsys.readouterr().err


def test_main_redacts_secrets_in_errors_and_progress(main_world, tmp_path, monkeypatch, capsys):
    secret = ENV["CLICKHOUSE_ADMIN_PASSWORD"]

    def leaky(env: Any) -> Any:
        raise LoaderError(f"cannot connect with password {secret}")

    real = REAL_CLI_LOAD

    def with_leaky(ns_: Any, environ: Any, *, say: Any = None, deps: Any = None) -> Any:
        if say:
            say(f"progress mentions {secret}")
        return real(ns_, environ, say=say, deps=main_world.deps(clickhouse=leaky))

    monkeypatch.setattr(load_cli, "cli_load", with_leaky)
    assert cli.main(["load", "--measurements", "m"]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "[REDACTED" in captured.out and "[REDACTED" in captured.err
    assert "cannot connect" in captured.err


def test_main_unexpected_error_is_one_redacted_line(main_world, monkeypatch, capsys) -> None:
    secret = ENV["CLICKHOUSE_ADMIN_PASSWORD"]

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError(f"driver said {secret}")

    monkeypatch.setattr(load_cli, "cli_load", boom)
    assert cli.main(["load", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert secret not in out["error"] and "runtime error" in out["error"]


@pytest.mark.skipif(os.environ.get("AA_LIVE") == "1", reason="offline guard only")
def test_an_unpatched_load_in_the_offline_suite_never_connects(tmp_path, monkeypatch, capsys):
    """L-6: even with admin variables set and nothing injected, `load` cannot reach an account
    from an offline test (tests/conftest.py)."""
    from tests.conftest import OFFLINE_ADMIN_REFUSAL

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["load", "--json", "--engine", "clickhouse"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert OFFLINE_ADMIN_REFUSAL in out["error"]
    assert not (tmp_path / "docs").exists()
