"""N1.7 verifier round 2: each test plants one defect class the first suite let through (L-2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.setup import SecretBox, SetupError, read_env_file
from agentic_analytics.setup import clickhouse as chs
from agentic_analytics.setup.cli import cli_setup
from agentic_analytics.setup.clickhouse import ClickHouseOptions, setup_clickhouse
from agentic_analytics.setup.clickhousectl import Clickhousectl, Completed
from agentic_analytics.setup.envfile import EnvFileError, update_env_file
from tests.setup.fakes import (
    ADMIN_PW,
    MCP_ROLE_ID,
    SERVICE_ID,
    FakeAdminClient,
    FakeCloud,
    service_doc,
)
from tests.setup.test_cli_setup import _deps, _ns

ROLE_IDS = (MCP_ROLE_ID,)


def _ctl(runner: Any, box: SecretBox | None = None, environ: dict[str, str] | None = None) -> Any:
    return Clickhousectl("/fake/clickhousectl", runner, environ or {}, box or SecretBox())


# ---- .env --------------------------------------------------------------------------------------


@pytest.mark.safety
@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_values_with_any_line_separator_are_refused(tmp_path: Path, sep: str) -> None:
    """str.splitlines (used by load_dotenv) breaks on these too: a value must not add a line."""
    env = tmp_path / ".env"
    env.write_text("# c\n", encoding="utf-8")
    value = f"h{sep}CLICKHOUSE_MCP_URL=https://evil.example/mcp{sep}"
    with pytest.raises(EnvFileError) as err:
        update_env_file(env, {"CLICKHOUSE_HOST": value})
    assert "evil" not in str(err.value) and "CLICKHOUSE_HOST" in str(err.value)
    assert env.read_text(encoding="utf-8") == "# c\n"
    assert "CLICKHOUSE_MCP_URL" not in read_env_file(env)


def test_other_lines_keep_their_bytes_including_crlf(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"# keep\r\nA=1\r\nB=2\r\n")
    update_env_file(env, {"A": "9"})
    assert env.read_bytes() == b"# keep\r\nA=9\nB=2\r\n"
    assert read_env_file(env) == {"A": "9", "B": "2"}


def test_unchanged_value_is_kept_even_if_it_cannot_be_requoted(tmp_path: Path) -> None:
    """A hand-written value with both quote kinds must not make every rerun fail."""
    env = tmp_path / ".env"
    line = "CLICKHOUSE_RO_PASSWORD='a\"b\\c'd'"
    env.write_text(f"{line}\nX=1\n", encoding="utf-8")
    value = read_env_file(env)["CLICKHOUSE_RO_PASSWORD"]
    assert update_env_file(env, {"CLICKHOUSE_RO_PASSWORD": value, "X": "1"}) == []
    assert env.read_text(encoding="utf-8") == f"{line}\nX=1\n"
    with pytest.raises(EnvFileError):  # a new value of that kind is still refused
        update_env_file(env, {"CLICKHOUSE_RO_PASSWORD": value + "x"})


# ---- secret scrubbing --------------------------------------------------------------------------


@pytest.mark.safety
def test_clickhousectl_error_is_scrubbed_before_it_is_cut(tmp_path: Path) -> None:
    def runner(argv: Any, env: Any) -> Completed:
        return Completed(1, "", "x" * 390 + " " + ADMIN_PW)

    with pytest.raises(SetupError) as err:
        _ctl(runner, SecretBox([ADMIN_PW])).json("service", "list")
    assert ADMIN_PW[:6] not in str(err.value)


@pytest.mark.safety
def test_secrets_in_a_failed_calls_json_are_scrubbed() -> None:
    planted = "NestedPw-" + "Zq9" * 5
    doc = {"result": {"service": {"id": "x", "password": planted}, "keySecret": "KS-" + "w" * 9}}

    def runner(argv: Any, env: Any) -> Completed:
        return Completed(1, json.dumps(doc), "")

    with pytest.raises(SetupError) as err:
        _ctl(runner).json("service", "create", "--name", "x")
    assert planted not in str(err.value) and "KS-www" not in str(err.value)


@pytest.mark.safety
def test_secret_box_hides_url_and_sql_escaped_forms() -> None:
    box = SecretBox(["p@ss'w/rd=1 x+y"])
    forms = (
        "p%40ss%27w%2Frd%3D1%20x%2By",  # URL-encoded
        "p%40ss%27w%2Frd%3D1+x%2By",  # form-encoded
        "p@ss\\'w/rd=1 x+y",  # SQL backslash-escaped
        "p@ss''w/rd=1 x+y",  # SQL doubled quote
    )
    for form in forms:
        assert form not in box.scrub(f"error near {form} here"), form


@pytest.mark.safety
def test_a_cut_pem_private_key_is_still_removed() -> None:
    head = "-----BEGIN " + "PRIVATE KEY-----"
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"
    out = SecretBox().scrub(f"failed: {head}\n{body}\nAAAA")
    assert body not in out and "failed:" in out


def test_clickhousectl_telemetry_is_always_off() -> None:
    assert (
        _ctl(lambda a, e: Completed(0, "{}", ""), environ={"DO_NOT_TRACK": "0"}).environ[
            "DO_NOT_TRACK"
        ]
        == "1"
    )


@pytest.mark.safety
def test_reused_password_with_a_quote_never_shows_in_its_sql_escaped_form(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    tricky = "it's-a-Pw9xyz"
    env.write_text(
        f"CLICKHOUSE_ADMIN_PASSWORD={ADMIN_PW}\nCLICKHOUSE_RO_USER=aa_ro_user\n"
        f'CLICKHOUSE_RO_PASSWORD="{tricky}"\n',
        encoding="utf-8",
    )
    cloud = FakeCloud()
    cloud.services[SERVICE_ID] = service_doc(SERVICE_ID, "agentic-analytics", "running")
    admin = FakeAdminClient(fail_at=1)  # its error echoes the statement as sent
    with pytest.raises(SetupError) as err:
        setup_clickhouse(
            ClickHouseOptions(mcp_role_ids=ROLE_IDS),
            ctl=_ctl(cloud),
            env_path=env,
            environ={},
            admin_client=admin.factory,
            say=lambda s: None,
            sleep=lambda s: None,
            clock=lambda: 0.0,
        )
    assert "it\\'s-a-Pw9xyz" not in str(err.value) and "statement 1/" in str(err.value)


@pytest.mark.safety
def test_an_unexpected_error_is_scrubbed_with_the_runs_secrets(tmp_path: Path) -> None:
    """The admin password is issued mid-run, so only the setup box (not the env redactor) knows
    it; an exception type setup does not expect must still pass through that box."""
    cloud = FakeCloud()

    def on_call(args: tuple[str, ...]) -> None:
        if args[:2] == ("service", "get"):
            raise RuntimeError(f"socket closed while holding {ADMIN_PW}")

    cloud.on_call = on_call
    ns = _ns("clickhouse", "--region", "eu-west-1", "--env-file", str(tmp_path / ".env"))
    code, payload, text = cli_setup(
        ns, {}, say=lambda s: None, deps=_deps(cloud, FakeAdminClient())
    )
    assert code == 2 and ADMIN_PW not in text and ADMIN_PW not in payload["error"]
    assert "RuntimeError" in text


# ---- values from clickhousectl -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("host", "evil.example.com X=1"), ("host", "a b.clickhouse.cloud"), ("id", "x;y")],
)
def test_odd_host_or_service_id_from_clickhousectl_is_refused(
    tmp_path: Path, field: str, value: str
) -> None:
    cloud = FakeCloud()
    doc = service_doc(SERVICE_ID, "agentic-analytics", "running")
    if field == "host":
        doc["endpoints"] = [{"protocol": "https", "host": value, "port": 8443}]
    else:
        doc["id"] = value
    cloud.services[value if field == "id" else SERVICE_ID] = doc
    admin = FakeAdminClient()
    with pytest.raises(SetupError, match="unexpected"):
        setup_clickhouse(
            ClickHouseOptions(mcp_role_ids=ROLE_IDS),
            ctl=_ctl(cloud),
            env_path=tmp_path / ".env",
            environ={"CLICKHOUSE_ADMIN_PASSWORD": ADMIN_PW},
            admin_client=admin.factory,
            say=lambda s: None,
            sleep=lambda s: None,
            clock=lambda: 0.0,
        )
    assert admin.opened == []


def test_role_statements_are_checked_as_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The splitter and the checker read comments differently; what is sent is what is checked."""
    real = chs.sql_statements

    def split_more(sql: str) -> list[str]:
        return [*real(sql), "GRANT ALL ON *.* TO analytics_ro"]

    monkeypatch.setattr(chs, "sql_statements", split_more)
    cloud = FakeCloud()
    cloud.services[SERVICE_ID] = service_doc(SERVICE_ID, "agentic-analytics", "running")
    admin = FakeAdminClient()
    with pytest.raises(SetupError, match="not read-only"):
        setup_clickhouse(
            ClickHouseOptions(mcp_role_ids=ROLE_IDS),
            ctl=_ctl(cloud),
            env_path=tmp_path / ".env",
            environ={"CLICKHOUSE_ADMIN_PASSWORD": ADMIN_PW},
            admin_client=admin.factory,
            say=lambda s: None,
            sleep=lambda s: None,
            clock=lambda: 0.0,
        )
    assert admin.opened == [] and admin.commands == []


# ---- pinned forms the first suite did not check (mutants M10, M15) ------------------------------


@pytest.mark.safety
def test_secret_box_hides_the_json_escaped_form() -> None:
    secret = 'pw"with\\back-' + "Tz4" * 4
    escaped = json.dumps(secret)[1:-1]
    assert escaped != secret
    out = SecretBox([secret]).scrub(json.dumps({"error": f"bad {secret}"}))
    assert escaped not in out and "[REDACTED]" in out


def test_bootstrap_sql_statements_and_order_are_pinned(tmp_path: Path) -> None:
    """Written out independently (EK L-10): a rerun must reinstall both current public keys."""
    from agentic_analytics.setup.snowflake import bootstrap_sql, ensure_keys

    keys = {k.user: k.public_b64 for k in ensure_keys(tmp_path / "keys")}
    sql = bootstrap_sql(list(ensure_keys(tmp_path / "keys")))
    for user, pub in keys.items():
        sql = sql.replace(pub, f"<{user}>")
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    stmts = [" ".join(s.split()) for s in body.split(";") if s.strip()]
    assert stmts[:15] == [
        "USE ROLE ACCOUNTADMIN",
        "CREATE USER IF NOT EXISTS AA_ADMIN_USER TYPE = SERVICE DEFAULT_ROLE = SYSADMIN "
        "RSA_PUBLIC_KEY = '<AA_ADMIN_USER>'",
        "ALTER USER AA_ADMIN_USER SET RSA_PUBLIC_KEY = '<AA_ADMIN_USER>'",
        "GRANT ROLE SYSADMIN TO USER AA_ADMIN_USER",
        "GRANT ROLE SECURITYADMIN TO USER AA_ADMIN_USER",
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SYSADMIN",
        "CREATE USER IF NOT EXISTS AA_RO_USER TYPE = SERVICE RSA_PUBLIC_KEY = '<AA_RO_USER>'",
        "ALTER USER AA_RO_USER SET RSA_PUBLIC_KEY = '<AA_RO_USER>'",
        "USE ROLE SYSADMIN",
        "CREATE DATABASE IF NOT EXISTS ANALYTICS",
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS.UK",
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS.UK_PROBES",
        "USE ROLE ACCOUNTADMIN",
        "CREATE ROLE IF NOT EXISTS ANALYTICS_RO",
        "CREATE WAREHOUSE IF NOT EXISTS ANALYTICS_XS WITH WAREHOUSE_SIZE = 'XSMALL' "
        "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE",
    ]
    assert stmts[-2:] == [
        "GRANT USAGE ON WAREHOUSE ANALYTICS_XS TO ROLE SYSADMIN",
        "SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS SNOWFLAKE_ACCOUNT",
    ]
    assert "BEGIN" not in sql and "PUBLIC KEY-----" not in sql
