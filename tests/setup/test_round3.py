"""N1.7 second verifier pass: each test plants one defect the round-2 fixes still let through."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.setup import SecretBox, SetupError, read_env_file
from agentic_analytics.setup.clickhouse import ClickHouseOptions, setup_clickhouse
from agentic_analytics.setup.clickhousectl import Clickhousectl, Completed
from agentic_analytics.setup.envfile import update_env_file
from tests.setup.fakes import ADMIN_PW, MCP_ROLE_ID, SERVICE_ID, FakeCloud, service_doc


class EchoingAdmin:
    """An admin client whose first statement fails with `pad` and the statement as sent, as a
    server error that quotes the query would."""

    def __init__(self, pad: str) -> None:
        self.pad, self.commands = pad, []

    def factory(self, host: str, user: str, password: str) -> EchoingAdmin:
        return self

    def command(self, sql: str) -> None:
        self.commands.append(sql)
        raise RuntimeError(f"{self.pad} while running {sql}")


def _run_failing(tmp_path: Path, ro_password: str, pad: str) -> str:
    env = tmp_path / ".env"
    env.write_text(
        f"CLICKHOUSE_ADMIN_PASSWORD={ADMIN_PW}\nCLICKHOUSE_RO_USER=aa_ro_user\n"
        f"CLICKHOUSE_RO_PASSWORD='{ro_password}'\n",
        encoding="utf-8",
    )
    assert read_env_file(env)["CLICKHOUSE_RO_PASSWORD"] == ro_password
    cloud = FakeCloud()
    cloud.services[SERVICE_ID] = service_doc(SERVICE_ID, "agentic-analytics", "running")
    with pytest.raises(SetupError) as err:
        setup_clickhouse(
            ClickHouseOptions(mcp_role_ids=(MCP_ROLE_ID,)),
            ctl=Clickhousectl("/fake/clickhousectl", cloud, {}, SecretBox()),
            env_path=env,
            environ={},
            admin_client=EchoingAdmin(pad).factory,
            say=lambda s: None,
            sleep=lambda s: None,
            clock=lambda: 0.0,
        )
    return str(err.value)


@pytest.mark.safety
def test_a_failed_statement_is_scrubbed_before_it_is_cut(tmp_path: Path) -> None:
    """The error is cut at 500 characters; cut first, a password straddling the cut leaked its
    first characters. Every cut position across the password is tried."""
    pw = "ReusedRoPw-" + "Kd7" * 4
    for n in range(330, 520, 3):
        msg = _run_failing(tmp_path, pw, "x" * n)
        assert pw[:5] not in msg, n
        assert msg.startswith("statement 1/")


@pytest.mark.safety
def test_a_failed_statement_is_scrubbed_before_spaces_are_collapsed(tmp_path: Path) -> None:
    """A hand-written password with a tab or two spaces no longer matched once re-spaced."""
    pw = "Hand\tWritten  Pw-9x"
    msg = _run_failing(tmp_path, pw, "Code: 497.")
    assert "Hand Written Pw-9x" not in msg and "Written" not in msg
    assert "[REDACTED]" in msg


@pytest.mark.safety
def test_a_secret_in_a_second_json_document_is_scrubbed() -> None:
    """Only the first JSON value was read for secrets: a failed call that printed a notice
    document first and the password in a second one showed the password."""
    planted = "SecondDocPw-" + "Vq3" * 4
    out = json.dumps({"notice": "creating"}) + "\n" + json.dumps({"password": planted})

    def runner(argv: Any, env: Any) -> Completed:
        return Completed(1, out, "")

    ctl = Clickhousectl("/fake/clickhousectl", runner, {}, SecretBox())
    with pytest.raises(SetupError) as err:
        ctl.json("service", "create", "--name", "x")
    assert planted not in str(err.value) and "[REDACTED]" in str(err.value)


@pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x1c", "\x85", " ", "\r"])
def test_a_name_after_a_rare_line_break_is_replaced_not_shadowed(tmp_path: Path, sep: str) -> None:
    """load_dotenv splits lines where str.splitlines does and the first assignment wins. The
    writer split on \\n only, so it appended the new value after a stale one it could not see,
    and the stale value was what every later read got."""
    env = tmp_path / ".env"
    # bytes, not write_text: on Windows write_text turns each \n into \r\n
    env.write_bytes(f"# keep\nA=1{sep}CLICKHOUSE_HOST=stale.example\nB=2\n".encode())
    assert read_env_file(env)["CLICKHOUSE_HOST"] == "stale.example"
    assert update_env_file(env, {"CLICKHOUSE_HOST": "new.clickhouse.cloud"}) == ["CLICKHOUSE_HOST"]
    assert read_env_file(env) == {"A": "1", "CLICKHOUSE_HOST": "new.clickhouse.cloud", "B": "2"}
    raw = env.read_bytes().decode("utf-8")
    assert raw == f"# keep\nA=1{sep}CLICKHOUSE_HOST=new.clickhouse.cloud\nB=2\n"
    assert update_env_file(env, {"CLICKHOUSE_HOST": "new.clickhouse.cloud"}) == []


def test_role_sql_that_revokes_the_role_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVOKE analytics_ro FROM ALL takes the role from the Query API endpoint user (L-7)."""
    from agentic_analytics.setup import clickhouse as chs

    real = chs.pack_role_sql
    monkeypatch.setattr(
        chs, "pack_role_sql", lambda *a, **k: real(*a, **k) + "REVOKE analytics_ro FROM ALL;\n"
    )
    cloud = FakeCloud()
    cloud.services[SERVICE_ID] = service_doc(SERVICE_ID, "agentic-analytics", "running")
    admin = EchoingAdmin("")
    with pytest.raises(SetupError, match="destructive statement: REVOKE analytics_ro FROM ALL"):
        setup_clickhouse(
            ClickHouseOptions(mcp_role_ids=(MCP_ROLE_ID,)),
            ctl=Clickhousectl("/fake/clickhousectl", cloud, {}, SecretBox()),
            env_path=tmp_path / ".env",
            environ={"CLICKHOUSE_ADMIN_PASSWORD": ADMIN_PW},
            admin_client=admin.factory,
            say=lambda s: None,
            sleep=lambda s: None,
            clock=lambda: 0.0,
        )
    assert admin.commands == []
