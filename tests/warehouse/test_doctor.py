"""EXE-06: doctor reports env var names only and checks both engines are read-only."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from snowflake.connector.errors import ProgrammingError

from agentic_analytics.warehouse import (
    ENV_VARS,
    ClickHouseExecutor,
    SnowflakeExecutor,
    executors_from_env,
    run_doctor,
)
from agentic_analytics.warehouse.doctor import env_presence
from tests.schemas.factories import pack
from tests.warehouse.fakes import (
    FakeCHClient,
    FakeConnect,
    FakeConnection,
    FakeCursor,
    ch_result,
    readonly_server_settings,
    write_rsa_key,
)

pytestmark = pytest.mark.safety

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-live-SENTINEL-9f8e7d6c"


def _secret_env() -> dict[str, str]:
    return {name: f"{SECRET}-{i}" for i, name in enumerate(ENV_VARS)}


def test_env_vars_match_the_spec_table_independently() -> None:
    """EK L-10: compare with the Tech Spec §4 table, not with the module's own list."""
    spec = (ROOT / "Agentic Analytics Tech Spec.md").read_text(encoding="utf-8")
    table = spec[spec.index("**Environment.**") : spec.index("## 5. Gold protocol")]
    assert set(re.findall(r"`([A-Z][A-Z0-9_]+)`", table)) == set(ENV_VARS)
    assert len(ENV_VARS) == len(set(ENV_VARS))


def test_env_presence_names_only_and_blank_is_missing() -> None:
    env = {"ANTHROPIC_API_KEY": "x" * 10, "OPENAI_API_KEY": "   ", "UNRELATED": "y"}
    got = dict(env_presence(env))
    assert got["ANTHROPIC_API_KEY"] is True
    assert got["OPENAI_API_KEY"] is False
    assert got["CLICKHOUSE_HOST"] is False
    assert "UNRELATED" not in got


def test_values_never_appear_in_any_output() -> None:
    env = _secret_env()
    report = run_doctor(env)
    text, js = report.render(), report.to_json()
    for name in ENV_VARS:
        assert name in text and name in js
    assert SECRET not in text and SECRET not in js
    assert json.loads(js)["env"]["CLICKHOUSE_RO_PASSWORD"] == "set"
    assert report.ok  # both engines skipped, nothing failed
    assert {c.status for c in report.checks} == {"skip"}


# ---- ClickHouse -----------------------------------------------------------------------------


def ch_handler(
    *,
    write: BaseException | None = None,
    url: BaseException | None = None,
    profile: tuple[str, str] = ("1", "30"),
) -> Callable[[str, Any], Any]:
    denied = DatabaseError(
        "Code: 164. DB::Exception: analytics_ro: Cannot execute query in readonly mode. (READONLY)",
        code=164,
    )
    url_denied = DatabaseError(
        "Code: 497. DB::Exception: analytics_ro: Not enough privileges. (ACCESS_DENIED)", code=497
    )

    def handle(sql: str, settings: Any) -> Any:
        s = sql.strip()
        if s.startswith(("INSERT", "CREATE")):
            return write if write is not None else denied
        if s.startswith("DROP"):
            return "dropped"
        if "url(" in s:
            return url if url is not None else url_denied
        if "system.settings" in s:
            return ch_result(
                ["name", "value"], [("readonly", profile[0]), ("max_execution_time", profile[1])]
            )
        return ch_result(["ok"], [(1,)])

    return handle


def ch_exec(handler: Callable[[str, Any], Any]) -> tuple[ClickHouseExecutor, FakeCHClient]:
    client = FakeCHClient(handler=handler, server_settings=readonly_server_settings())
    return ClickHouseExecutor("h", "ro", "pw", client=client), client


def _status(report: Any) -> dict[str, str]:
    return {c.name: c.status for c in report.checks}


def test_clickhouse_read_only_role_passes_every_check() -> None:
    exe, client = ch_exec(ch_handler())
    report = run_doctor({}, clickhouse=exe)
    st = _status(report)
    assert st == {
        "clickhouse.reachable": "pass",
        "clickhouse.guard_refuses_write": "pass",
        "clickhouse.guard_refuses_url": "pass",
        "clickhouse.role_refuses_insert": "pass",
        "clickhouse.role_refuses_create": "pass",
        "clickhouse.role_refuses_url": "pass",
        "clickhouse.profile": "pass",
        "clickhouse.idle_timeout": "skip",
        "snowflake": "skip",
    }
    assert report.ok
    raw = [
        (k, sql, s)
        for k, sql, s in client.calls
        if sql.startswith(("INSERT", "CREATE")) or "url(" in sql
    ]
    assert len(raw) == 3 and all(s is None for _, _, s in raw)  # sent raw: no readonly setting
    assert any(
        "INSERT INTO uk.uk_price_paid SELECT * FROM uk.uk_price_paid LIMIT 0" in sql
        for _, sql, _ in raw
    )


def test_clickhouse_write_accepted_fails_and_cleans_up() -> None:
    exe, client = ch_exec(ch_handler(write=None))
    handler = ch_handler()

    def accepting(sql: str, settings: Any) -> Any:
        return "ok" if sql.startswith(("INSERT", "CREATE")) else handler(sql, settings)

    client.handler = accepting
    report = run_doctor({}, clickhouse=exe)
    st = _status(report)
    assert st["clickhouse.role_refuses_insert"] == "fail"
    assert st["clickhouse.role_refuses_create"] == "fail"
    assert not report.ok
    assert any(
        sql.startswith("DROP TABLE IF EXISTS uk.aa_doctor_write_check")
        for _, sql, _ in client.calls
    )


def test_clickhouse_url_reaching_the_network_fails() -> None:
    net = DatabaseError(f"Code: 1000. Connection refused, password {SECRET}-7", code=1000)
    exe, _ = ch_exec(ch_handler(url=net))
    report = run_doctor(_secret_env(), clickhouse=exe)
    check = next(c for c in report.checks if c.name == "clickhouse.role_refuses_url")
    assert check.status == "fail" and "code 1000" in check.detail
    assert SECRET not in check.detail and SECRET not in report.render()


@pytest.mark.parametrize("profile", [("0", "30"), ("1", "0"), ("1", "3600")])
def test_clickhouse_profile_read_back(profile: tuple[str, str]) -> None:
    exe, _ = ch_exec(ch_handler(profile=profile))
    assert _status(run_doctor({}, clickhouse=exe))["clickhouse.profile"] == "fail"


def test_clickhouse_unreachable_stops_early() -> None:
    from clickhouse_connect.driver.exceptions import OperationalError

    exe, _ = ch_exec(lambda sql, s: OperationalError("Network Error"))
    report = run_doctor({}, clickhouse=exe)
    assert _status(report)["clickhouse.reachable"] == "fail" and not report.ok


def test_pack_names_the_tables_probed() -> None:
    exe, client = ch_exec(ch_handler())
    run_doctor(
        {},
        clickhouse=exe,
        pack=pack(databases={"clickhouse": "ppd", "snowflake": "LAB.PPD"}, tables=["sales"]),
    )
    assert any(
        "INSERT INTO ppd.sales SELECT * FROM ppd.sales LIMIT 0" in sql for _, sql, _ in client.calls
    )


def test_unsafe_target_names_are_rejected() -> None:
    with pytest.raises(ValueError):
        run_doctor({}, clickhouse_target=("uk", "t; DROP TABLE x"))


# ---- Snowflake ------------------------------------------------------------------------------


def sf_handler(
    *, write_ok: bool = False, suspend: Any = 60, resume: str = "true", role: str = "ANALYTICS_RO"
) -> Callable[[str], FakeCursor]:
    def handle(sql: str) -> FakeCursor:
        s = sql.strip()
        if s.startswith(("INSERT", "CREATE")):
            if write_ok:
                return FakeCursor(["number of rows inserted"], [(0,)])
            return FakeCursor(
                error=ProgrammingError(
                    "SQL access control error: Insufficient privileges to operate on table",
                    errno=3001,
                    sqlstate="42501",
                )
            )
        if s.startswith("DROP"):
            return FakeCursor(["status"], [("dropped",)])
        if s.startswith("SHOW WAREHOUSES"):
            return FakeCursor(
                ["name", "state", "auto_suspend", "auto_resume"],
                [("ANALYTICS_XS", "SUSPENDED", suspend, resume)],
            )
        if "CURRENT_ROLE" in s:
            return FakeCursor(["R"], [(role,)])
        return FakeCursor(["X"], [(1,)])

    return handle


@pytest.fixture(scope="module")
def key_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_rsa_key(tmp_path_factory.mktemp("keys") / "ro.p8")


def sf_exec(
    key_path: Path, handler: Callable[[str], FakeCursor]
) -> tuple[SnowflakeExecutor, FakeConnection]:
    conn = FakeConnection(handler=handler)
    return SnowflakeExecutor(
        "acct", "RO", key_path, "ANALYTICS_XS", "ANALYTICS_RO", connect=FakeConnect(conn)
    ), conn


def test_snowflake_read_only_role_passes_every_check(key_path: Path) -> None:
    exe, conn = sf_exec(key_path, sf_handler())
    report = run_doctor({}, snowflake=exe)
    assert _status(report) == {
        "clickhouse": "skip",
        "snowflake.reachable": "pass",
        "snowflake.role": "pass",
        "snowflake.guard_refuses_write": "pass",
        "snowflake.guard_refuses_url": "pass",
        "snowflake.role_refuses_insert": "pass",
        "snowflake.role_refuses_create": "pass",
        "snowflake.auto_suspend": "pass",
    }
    executed = [c.executed[0] for c in conn.handed]
    assert "SHOW WAREHOUSES LIKE 'ANALYTICS_XS'" in executed
    assert (
        "INSERT INTO ANALYTICS.UK.UK_PRICE_PAID SELECT * FROM ANALYTICS.UK.UK_PRICE_PAID LIMIT 0"
        in executed
    )


@pytest.mark.parametrize(
    ("kw", "failing"),
    [
        ({"write_ok": True}, "snowflake.role_refuses_insert"),
        ({"suspend": 3600}, "snowflake.auto_suspend"),
        ({"suspend": 0}, "snowflake.auto_suspend"),
        ({"suspend": None}, "snowflake.auto_suspend"),
        ({"resume": "false"}, "snowflake.auto_suspend"),
        ({"role": "SYSADMIN"}, "snowflake.role"),
    ],
)
def test_snowflake_planted_misconfigurations_fail(
    key_path: Path, kw: dict[str, Any], failing: str
) -> None:
    exe, _ = sf_exec(key_path, sf_handler(**kw))
    report = run_doctor({}, snowflake=exe)
    assert _status(report)[failing] == "fail" and not report.ok


def test_snowflake_config_error_is_reported_not_raised(tmp_path: Path) -> None:
    exe = SnowflakeExecutor(
        "acct", "RO", tmp_path / "missing.p8", "WH", "ANALYTICS_RO", connect=FakeConnect(None)
    )
    report = run_doctor({}, snowflake=exe)
    check = next(c for c in report.checks if c.name == "snowflake.error")
    assert check.status == "fail" and "WarehouseConfigError" in check.detail


def test_executors_from_env_builds_only_complete_engines(key_path: Path) -> None:
    ch, sf = executors_from_env({"CLICKHOUSE_HOST": "h", "CLICKHOUSE_RO_USER": "u"})
    assert ch is None and sf is None
    ch, sf = executors_from_env(
        {
            "CLICKHOUSE_HOST": "h",
            "CLICKHOUSE_RO_USER": "u",
            "CLICKHOUSE_RO_PASSWORD": SECRET,
            "SNOWFLAKE_ACCOUNT": "a",
            "SNOWFLAKE_RO_USER": "u",
            "SNOWFLAKE_RO_PRIVATE_KEY_PATH": str(key_path),
            "SNOWFLAKE_WAREHOUSE": "WH",
        }
    )
    assert isinstance(ch, ClickHouseExecutor) and isinstance(sf, SnowflakeExecutor)
    assert sf.role == "ANALYTICS_RO" and SECRET not in repr(ch)


def test_snowflake_role_must_be_analytics_ro_whatever_the_executor_says(key_path: Path) -> None:
    conn = FakeConnection(handler=sf_handler(role="SYSADMIN"))
    exe = SnowflakeExecutor(
        "acct", "RO", key_path, "ANALYTICS_XS", "SYSADMIN", connect=FakeConnect(conn)
    )
    report = run_doctor({}, snowflake=exe)
    assert _status(report)["snowflake.role"] == "fail" and not report.ok


def test_short_secret_values_withhold_the_detail() -> None:
    net = DatabaseError("Code: 1000. Connection refused, pw=abc", code=1000)
    exe, _ = ch_exec(ch_handler(url=net))
    report = run_doctor({"CLICKHOUSE_RO_PASSWORD": "abc"}, clickhouse=exe)
    check = next(c for c in report.checks if c.name == "clickhouse.role_refuses_url")
    assert check.status == "fail" and "abc" not in check.detail
    assert "withheld" in check.detail and "pw=abc" not in report.render()


def test_unrelated_short_env_values_do_not_hide_details() -> None:
    net = DatabaseError("Code: 1000. Connection refused", code=1000)
    exe, _ = ch_exec(ch_handler(url=net))
    report = run_doctor({"CLAUDECODE": "1", "SHELL": "/bin/zsh"}, clickhouse=exe)
    check = next(c for c in report.checks if c.name == "clickhouse.role_refuses_url")
    assert "code 1000" in check.detail


def test_doctor_fails_when_the_guard_does_not_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_analytics.warehouse import clickhouse as ch_mod
    from agentic_analytics.warehouse.guard import GuardVerdict

    monkeypatch.setattr(ch_mod, "check_sql", lambda sql, engine: GuardVerdict(True))
    exe, _ = ch_exec(ch_handler())
    st = _status(run_doctor({}, clickhouse=exe))
    assert st["clickhouse.guard_refuses_write"] == "fail"
    assert st["clickhouse.guard_refuses_url"] == "fail"


def test_secret_looking_variables_outside_the_spec_are_scrubbed_too() -> None:
    net = DatabaseError("Code: 1000. refused, token tok-PRIVATE-4242", code=1000)
    exe, _ = ch_exec(ch_handler(url=net))
    report = run_doctor({"MY_SERVICE_TOKEN": "tok-PRIVATE-4242"}, clickhouse=exe)
    check = next(c for c in report.checks if c.name == "clickhouse.role_refuses_url")
    assert "tok-PRIVATE-4242" not in check.detail and "***" in check.detail


def test_guard_check_needs_nothing_sent() -> None:
    """A 'guard' error that still carries a query id means SQL reached the server: a fail."""
    from agentic_analytics.schemas import Execution
    from agentic_analytics.warehouse.doctor import _guard_checks

    class Leaky:
        def execute(self, sql: str, *, row_cap: int = 1) -> Execution:
            return Execution(
                engine="clickhouse",
                sql=sql,
                result=None,
                rows_total=None,
                truncated=False,
                query_id="sent-anyway",
                elapsed_ms=1,
                error="guard",
            )

    checks = _guard_checks("clickhouse", Leaky(), "uk.t", "SELECT * FROM url('x', CSV)")
    assert [c.status for c in checks] == ["fail", "fail"]
