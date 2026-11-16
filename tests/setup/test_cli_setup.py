"""SET-01..06 through the CLI: `agentic-analytics setup clickhouse|snowflake`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics import cli
from agentic_analytics.setup import read_env_file
from agentic_analytics.setup.cli import USAGE, cli_setup
from agentic_analytics.setup.clickhouse import ADMIN_KEY_NOTE
from tests.setup.fakes import (
    ADMIN_PW,
    CLOUD_API_SECRET,
    KEY_SECRET,
    PLANTED,
    FakeAdminClient,
    FakeCloud,
)


def _ns(*argv: str) -> argparse.Namespace:
    ns, rest = cli.build_parser().parse_known_args(["setup", *argv])
    assert rest == []
    return ns


def _deps(cloud: FakeCloud, admin: FakeAdminClient) -> dict[str, Any]:
    return {
        "clickhousectl": "/fake/clickhousectl",
        "runner": cloud,
        "admin_client": admin.factory,
        "sleep": lambda s: None,
        "clock": lambda: 0.0,
    }


def test_setup_is_a_built_command_owned_by_n17() -> None:
    assert cli.COMMANDS["setup"][1] == "N1.7" and "setup" in cli.BUILT


def test_setup_without_engine_prints_usage_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["setup"]) == 2
    err = capsys.readouterr().err
    assert err.strip() == USAGE.strip()
    assert "agentic-analytics setup clickhouse" in USAGE and "setup snowflake" in USAGE


def test_cli_setup_clickhouse_end_to_end_with_fakes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# keep me\nANTHROPIC_API_KEY=sk-ant-keep\n", encoding="utf-8")
    cloud, admin = FakeCloud(), FakeAdminClient()
    lines: list[str] = []
    ns = _ns("clickhouse", "--region", "eu-west-1", "--env-file", str(env_file))
    environ = {"CLICKHOUSE_CLOUD_API_SECRET": CLOUD_API_SECRET}
    code, payload, text = cli_setup(ns, environ, say=lines.append, deps=_deps(cloud, admin))
    assert code == 0 and payload["ok"] and payload["created"]
    assert payload["role"] == "analytics_ro" and payload["mcp_key"] == "created"
    assert "Two steps have no public API" in text and ADMIN_KEY_NOTE in text
    assert payload["console_steps"] in text
    assert env_file.read_text(encoding="utf-8").startswith(
        "# keep me\nANTHROPIC_API_KEY=sk-ant-keep\n"
    )
    env = read_env_file(env_file)
    assert (
        env["CLICKHOUSE_ADMIN_PASSWORD"] == ADMIN_PW
        and env["CLICKHOUSE_MCP_KEY_SECRET"] == KEY_SECRET
    )
    assert "CLICKHOUSE_MCP_KEY_SECRET" in payload["env_written"]


def test_cli_text_and_json_carry_the_console_steps(tmp_path: Path) -> None:
    ns = _ns("clickhouse", "--region", "eu-west-1", "--env-file", str(tmp_path / ".env"), "--json")
    lines: list[str] = []
    code, payload, _text = cli_setup(
        ns, {}, say=lines.append, deps=_deps(FakeCloud(), FakeAdminClient())
    )
    assert code == 0 and lines == []  # --json: no progress lines
    assert payload["console_steps"].startswith("Two steps have no public API.")
    assert "set its role to 'analytics_ro'" in payload["console_steps"]
    json.dumps(payload)


@pytest.mark.safety
def test_planted_secrets_never_reach_output(tmp_path: Path) -> None:
    ns = _ns("clickhouse", "--region", "eu-west-1", "--env-file", str(tmp_path / ".env"))
    lines: list[str] = []
    environ = {"CLICKHOUSE_CLOUD_API_SECRET": CLOUD_API_SECRET}
    code, payload, text = cli_setup(
        ns, environ, say=lines.append, deps=_deps(FakeCloud(), FakeAdminClient())
    )
    ro_pw = read_env_file(tmp_path / ".env")["CLICKHOUSE_RO_PASSWORD"]
    out = "\n".join(lines) + text + json.dumps(payload)
    assert code == 0
    for secret in (*PLANTED, ro_pw):
        assert secret not in out


@pytest.mark.safety
def test_errors_are_exit_2_and_scrubbed(tmp_path: Path) -> None:
    from agentic_analytics.setup.clickhousectl import Completed

    cloud = FakeCloud(fail={"key create": Completed(1, "", f"quota hit; secret {ADMIN_PW}")})
    ns = _ns("clickhouse", "--region", "eu-west-1", "--env-file", str(tmp_path / ".env"))
    code, payload, text = cli_setup(
        ns, {}, say=lambda s: None, deps=_deps(cloud, FakeAdminClient())
    )
    assert code == 2 and not payload["ok"]
    assert ADMIN_PW not in text and ADMIN_PW not in payload["error"] and "[REDACTED]" in text
    # the admin password issued before the failure is kept for the rerun
    assert read_env_file(tmp_path / ".env")["CLICKHOUSE_ADMIN_PASSWORD"] == ADMIN_PW


def test_missing_clickhousectl_is_a_clear_error(tmp_path: Path) -> None:
    ns = _ns("clickhouse", "--clickhousectl", str(tmp_path / "nope"))
    code, _payload, text = cli_setup(ns, {})
    assert code == 2 and "does not exist" in text


def test_main_setup_snowflake_json_prints_sql_and_no_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    keys = tmp_path / "keys"
    assert cli.main(["setup", "snowflake", "--key-dir", str(keys), "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload["ok"] and not payload["ran"] and "USE ROLE ACCOUNTADMIN;" in payload["sql"]
    assert payload["keys"]["AA_RO_USER"]["key_file"] == str(keys / "ro_key.p8")
    for p in keys.glob("*.p8"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if "-----" not in line:
                assert line not in raw
    assert not (tmp_path / ".env").exists()  # print mode leaves .env alone


def test_main_setup_snowflake_text_tells_the_snowsight_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["setup", "snowflake", "--key-dir", str(tmp_path / "k")]) == 0
    out = capsys.readouterr().out
    assert "Paste this SQL into Snowsight as ACCOUNTADMIN" in out
    assert "SNOWFLAKE_RO_USER=AA_RO_USER" in out and "--run" in out
