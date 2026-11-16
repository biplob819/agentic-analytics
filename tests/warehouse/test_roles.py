"""EXE-04: role SQL for both engines grants reads only, parameterised by the pack."""

from __future__ import annotations

import re

import pytest

from agentic_analytics.warehouse import (
    clickhouse_role_sql,
    pack_role_sql,
    role_sql_problems,
    snowflake_role_sql,
)
from tests.schemas.factories import pack

pytestmark = pytest.mark.safety

CH_WRITE_WORDS = (
    "ALL",
    "INSERT",
    "ALTER",
    "CREATE",
    "DROP",
    "TRUNCATE",
    "OPTIMIZE",
    "SYSTEM",
    "SOURCES",
    "URL",
    "REMOTE",
    "S3",
    "FILE",
    "MYSQL",
    "POSTGRES",
    "ODBC",
    "JDBC",
    "HDFS",
    "KILL",
    "INTROSPECTION",
    "DICTGET",
)
SF_WRITE_WORDS = (
    "ALL PRIVILEGES",
    "OWNERSHIP",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "CREATE",
    "MODIFY",
    "OPERATE",
    "MANAGE",
    "APPLY",
    "EXECUTE",
    "ACCOUNTADMIN",
    "SYSADMIN",
)


def _grants(sql: str) -> list[str]:
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [" ".join(s.split()) for s in body.split(";") if s.strip().upper().startswith("GRANT")]


def test_clickhouse_role_sql_grants_select_on_the_pack_databases_only() -> None:
    sql = pack_role_sql(pack(), "clickhouse", user="aa_reader")
    grants = _grants(sql)
    assert "GRANT SELECT ON uk.* TO analytics_ro" in grants
    assert "GRANT SELECT ON uk_probes.* TO analytics_ro" in grants
    assert "GRANT analytics_ro TO aa_reader" in grants
    assert len(grants) == 3
    assert "readonly = 1 MIN 1 MAX 2 CHANGEABLE_IN_READONLY," in sql  # D-64
    assert "max_execution_time = 30 MIN 1 MAX 300 CHANGEABLE_IN_READONLY" in sql  # D-64
    assert "CREATE ROLE IF NOT EXISTS analytics_ro;" in sql
    assert "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile';" in sql
    for g in grants:
        words = set(re.findall(r"[A-Z0-9_]+", g.upper()))
        assert not words & set(CH_WRITE_WORDS), g
        assert "*.*" not in g
    assert role_sql_problems(sql, "clickhouse") == []


def _ch_statements(sql: str) -> list[str]:
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def test_clickhouse_role_sql_statements_are_pinned() -> None:
    """Written out independently (EK L-10). Setup re-applies this on every run, so the role and
    the profile are created IF NOT EXISTS and then ALTERed, in that order (an ALTER before its
    CREATE fails on a new service)."""
    settings = (
        "readonly = 1 MIN 1 MAX 2 CHANGEABLE_IN_READONLY, "
        "max_execution_time = 30 MIN 1 MAX 300 CHANGEABLE_IN_READONLY, "
        "max_result_rows MIN 1 MAX 10001 CHANGEABLE_IN_READONLY, "
        "result_overflow_mode CHANGEABLE_IN_READONLY, "
        "send_progress_in_http_headers CHANGEABLE_IN_READONLY, "
        "http_headers_progress_interval_ms CHANGEABLE_IN_READONLY, "
        "max_memory_usage CONST, max_threads CONST, max_bytes_to_read CONST, "
        "max_rows_to_read CONST, max_result_bytes CONST"
    )
    assert _ch_statements(pack_role_sql(pack(), "clickhouse", user="aa_reader")) == [
        "CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile",
        f"ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS {settings}",
        "CREATE ROLE IF NOT EXISTS analytics_ro",
        "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile'",
        "GRANT SELECT ON uk.* TO analytics_ro",
        "GRANT SELECT ON uk_probes.* TO analytics_ro",
        "GRANT analytics_ro TO aa_reader",
        "ALTER USER aa_reader DEFAULT ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile'",
    ]


@pytest.mark.parametrize("user", [None, "aa_reader"])
def test_clickhouse_role_sql_never_replaces_the_role_or_profile(user: str | None) -> None:
    """Found live: replacing the role dropped ClickHouse Cloud's grant of analytics_ro to the
    Query API endpoint user, so MCP queries ran with no role. Not even a comment says it."""
    for sql in (pack_role_sql(pack(), "clickhouse", user=user), clickhouse_role_sql(["uk"])):
        assert "OR REPLACE" not in sql.upper()
        assert not re.search(r"\bREPLACE\b", sql, re.I)


def test_snowflake_role_sql_grants_usage_and_select_only() -> None:
    sql = pack_role_sql(pack(), "snowflake", user="AA_READER", warehouse="AA_XS")
    grants = _grants(sql)
    want = [
        "GRANT USAGE ON WAREHOUSE AA_XS TO ROLE ANALYTICS_RO",
        "GRANT USAGE ON DATABASE ANALYTICS TO ROLE ANALYTICS_RO",
        "GRANT USAGE ON SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON FUTURE TABLES IN SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO",
        "GRANT USAGE ON SCHEMA ANALYTICS.UK_PROBES TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS.UK_PROBES TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON FUTURE TABLES IN SCHEMA ANALYTICS.UK_PROBES TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO",
        "GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO",
        "GRANT DATABASE ROLE SNOWFLAKE.CORTEX_ANALYST_USER TO ROLE ANALYTICS_RO",
        "GRANT ROLE ANALYTICS_RO TO USER AA_READER",
    ]
    assert grants == want
    assert (
        "CREATE WAREHOUSE IF NOT EXISTS AA_XS WITH WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 "
        "AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;" in sql
    )
    for g in grants:
        assert not any(w in g.upper() for w in SF_WRITE_WORDS), g
    assert role_sql_problems(sql, "snowflake") == []


def test_database_names_come_from_the_pack() -> None:
    p = pack(databases={"clickhouse": "ppd", "snowflake": "LAB.PPD"})
    assert "GRANT SELECT ON ppd_probes.* TO analytics_ro;" in pack_role_sql(p, "clickhouse")
    sf = pack_role_sql(p, "snowflake")
    assert "GRANT USAGE ON DATABASE LAB TO ROLE ANALYTICS_RO;" in sf
    assert "IN SCHEMA LAB.PPD_PROBES" in sf and "UK" not in sf


@pytest.mark.parametrize(
    "bad",
    ["uk; DROP DATABASE uk", "uk.*", "", "1uk", "uk`", "u k"],
)
def test_identifiers_cannot_inject_sql(bad: str) -> None:
    with pytest.raises(ValueError):
        clickhouse_role_sql([bad])
    with pytest.raises(ValueError):
        snowflake_role_sql(bad, ["UK"])


def test_pack_engine_and_shape_errors() -> None:
    with pytest.raises(ValueError):
        pack_role_sql(pack(engines=["clickhouse"], databases={"clickhouse": "uk"}), "snowflake")
    with pytest.raises(ValueError, match=r"DATABASE\.SCHEMA"):
        pack_role_sql(pack(databases={"clickhouse": "uk", "snowflake": "ANALYTICS"}), "snowflake")


_CH = clickhouse_role_sql(["uk", "uk_probes"], user="aa_reader")
_SF = snowflake_role_sql("ANALYTICS", ["UK", "UK_PROBES"], user="AA_READER")

CH_DEFECTS = {
    "grant all": _CH + "GRANT ALL ON uk.* TO analytics_ro;\n",
    "grant insert": _CH.replace("GRANT SELECT ON uk.*", "GRANT SELECT, INSERT ON uk.*"),
    "every database": _CH.replace("ON uk.*", "ON *.*"),
    "sources": _CH + "GRANT SOURCES ON *.* TO analytics_ro;\n",
    "url": _CH + "GRANT URL ON *.* TO analytics_ro;\n",
    "remote": _CH + "GRANT REMOTE ON *.* TO analytics_ro;\n",
    "grant option": _CH.replace(
        "GRANT SELECT ON uk.* TO analytics_ro;",
        "GRANT SELECT ON uk.* TO analytics_ro WITH GRANT OPTION;",
    ),
    "readonly missing": _CH.replace("    readonly = 1 MIN 1 MAX 2 CHANGEABLE_IN_READONLY,\n", ""),
    "readonly may drop to 0": _CH.replace("readonly = 1 MIN 1 MAX 2", "readonly = 1 MIN 0 MAX 2"),
    "readonly unbounded": _CH.replace("readonly = 1 MIN 1 MAX 2", "readonly = 1"),
    "mcp cap raised": _CH.replace("MAX 300 CHANGEABLE", "MAX 900 CHANGEABLE"),
    "threads not const": _CH.replace("    max_threads CONST,\n", ""),
    "memory not const": _CH.replace("    max_memory_usage CONST,\n", ""),
    "readonly zero": _CH.replace("readonly = 1", "readonly = 0"),
    "long timeout": _CH.replace(
        "max_execution_time = 30 MIN 1 MAX 300", "max_execution_time = 3600 MIN 1 MAX 3600"
    ),
    "profile unbound": _CH.replace(" SETTINGS PROFILE 'analytics_ro_profile';\nGRANT", ";\nGRANT"),
    "other role to user": _CH + "GRANT default_admin TO aa_reader;\n",
    "old OR REPLACE template": _CH.replace(
        "CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile;\n"
        "ALTER SETTINGS PROFILE analytics_ro_profile",
        "CREATE SETTINGS PROFILE OR REPLACE analytics_ro_profile",
    ).replace(
        "CREATE ROLE IF NOT EXISTS analytics_ro;\nALTER ROLE analytics_ro",
        "CREATE ROLE OR REPLACE analytics_ro",
    ),
    "second alter loosens the profile": _CH
    + "ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS readonly = 0;\n",
    "role settings appended": _CH + "ALTER ROLE analytics_ro SETTINGS max_threads = 64;\n",
    "profile alter missing": "\n".join(
        ln for ln in _CH.splitlines() if not ln.startswith(("ALTER SETTINGS", "    "))
    ),
}
SF_DEFECTS = {
    "all privileges": _SF + "GRANT ALL PRIVILEGES ON SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO;\n",
    "insert": _SF.replace(
        "GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS.UK ",
        "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ANALYTICS.UK ",
    ),
    "ownership": _SF + "GRANT OWNERSHIP ON SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO;\n",
    "create table": _SF + "GRANT CREATE TABLE ON SCHEMA ANALYTICS.UK TO ROLE ANALYTICS_RO;\n",
    "operate warehouse": _SF.replace(
        "GRANT USAGE ON WAREHOUSE", "GRANT USAGE, OPERATE ON WAREHOUSE"
    ),
    "modify warehouse": _SF + "GRANT MODIFY ON WAREHOUSE ANALYTICS_XS TO ROLE ANALYTICS_RO;\n",
    "admin role": _SF + "GRANT ROLE SYSADMIN TO ROLE ANALYTICS_RO;\n",
    "account role to user": _SF.replace(
        "GRANT ROLE ANALYTICS_RO TO USER", "GRANT ROLE ACCOUNTADMIN TO USER"
    ),
    "no cortex": _SF.replace(
        "GRANT DATABASE ROLE SNOWFLAKE.CORTEX_ANALYST_USER TO ROLE ANALYTICS_RO;\n", ""
    ),
    "no future tables": "\n".join(ln for ln in _SF.splitlines() if "FUTURE TABLES" not in ln),
    "slow suspend": _SF.replace("AUTO_SUSPEND = 60", "AUTO_SUSPEND = 3600"),
    "never suspend": _SF.replace("AUTO_SUSPEND = 60", "AUTO_SUSPEND = 0"),
    "no resume": _SF.replace("AUTO_RESUME = TRUE", "AUTO_RESUME = FALSE"),
    "big warehouse": _SF.replace("'XSMALL'", "'LARGE'"),
    "starts running": _SF.replace(" INITIALLY_SUSPENDED = TRUE", ""),
    "grant option": _SF.replace(
        "TO ROLE ANALYTICS_RO;\nGRANT USAGE ON DATABASE",
        "TO ROLE ANALYTICS_RO WITH GRANT OPTION;\nGRANT USAGE ON DATABASE",
    ),
}


@pytest.mark.parametrize("name", sorted(CH_DEFECTS))
def test_checker_catches_planted_clickhouse_defects(name: str) -> None:
    sql = CH_DEFECTS[name]
    assert sql != _CH, f"planted defect {name!r} did not change the SQL"  # EK L-11
    assert role_sql_problems(sql, "clickhouse"), name


@pytest.mark.parametrize("name", sorted(SF_DEFECTS))
def test_checker_catches_planted_snowflake_defects(name: str) -> None:
    sql = SF_DEFECTS[name]
    assert sql != _SF, f"planted defect {name!r} did not change the SQL"  # EK L-11
    assert role_sql_problems(sql, "snowflake"), name


def test_checker_refuses_sql_with_no_grants() -> None:
    assert role_sql_problems("-- nothing\n", "clickhouse")
    assert role_sql_problems("-- nothing\n", "snowflake")


def test_grant_option_is_reported_as_such() -> None:
    """Isolates the grant-option check: other checks also flag these lines, so assert the
    specific problem (it would vanish if that check were removed)."""
    ch = _CH.replace(
        "GRANT SELECT ON uk.* TO analytics_ro;",
        "GRANT SELECT ON uk.* TO analytics_ro WITH GRANT OPTION;",
    )
    sf = _SF.replace(
        "GRANT USAGE ON DATABASE ANALYTICS TO ROLE ANALYTICS_RO;",
        "GRANT USAGE ON DATABASE ANALYTICS TO ROLE ANALYTICS_RO WITH GRANT OPTION;",
    )
    assert ch != _CH and sf != _SF
    assert any(p.startswith("grant option") for p in role_sql_problems(ch, "clickhouse"))
    assert any(p.startswith("grant option") for p in role_sql_problems(sf, "snowflake"))


def test_query_api_progress_settings_may_change_but_nothing_else() -> None:
    """D-63: the Cloud Query API (MCP path) sets two progress settings on every request."""
    from agentic_analytics.warehouse.roles import clickhouse_role_sql, role_sql_problems

    sql = clickhouse_role_sql(["uk"])
    assert "send_progress_in_http_headers CHANGEABLE_IN_READONLY" in sql
    assert "http_headers_progress_interval_ms CHANGEABLE_IN_READONLY" in sql
    assert role_sql_problems(sql, "clickhouse") == []
    widened = sql.replace(
        "result_overflow_mode CHANGEABLE_IN_READONLY,",
        "result_overflow_mode CHANGEABLE_IN_READONLY,\n"
        "    max_memory_usage CHANGEABLE_IN_READONLY,",
    )
    assert any("max_memory_usage" in p for p in role_sql_problems(widened, "clickhouse"))


def test_role_sql_comments_hold_no_semicolon() -> None:
    """A ';' inside a comment splits a statement for a naive splitter (found live, D-64)."""
    from agentic_analytics.warehouse.roles import clickhouse_role_sql

    sql = clickhouse_role_sql(["uk"])
    assert all(";" not in line for line in sql.splitlines() if line.strip().startswith("--"))


# ---- the settings profile, one rule at a time (L-2; D-63/D-64 verifier) -----------------------

_RO = "    readonly = 1 MIN 1 MAX 2 CHANGEABLE_IN_READONLY,\n"
_CREATE_PROFILE = "CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile;\n"
_ALTER_PROFILE = _CH[_CH.index("ALTER SETTINGS PROFILE") : _CH.index("CREATE ROLE")]
_CREATE_ROLE = "CREATE ROLE IF NOT EXISTS analytics_ro;\n"
_BIND = "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile';\n"
_OR_REPLACE = (
    "OR REPLACE drops the grants and bindings of the role or profile"
    " (the Query API endpoint's grant among them): "
)
_UNBOUND = (
    "role is not bound to the read-only settings profile by one ALTER ROLE analytics_ro"
    " SETTINGS PROFILE 'analytics_ro_profile' "
)


def _swap(old: str, new: str, sql: str = _CH) -> str:
    assert sql.count(old) == 1, old  # EK L-11
    return sql.replace(old, new)


# Each input breaks exactly one rule; the checker must report exactly that rule's message.
PROFILE_RULES = {
    "readonly bounds": (
        _swap("readonly = 1 MIN 1 MAX 2", "readonly = 1 MIN 0 MAX 2"),
        "readonly can be changed to 0 or above 2",
    ),
    "readonly max 3": (
        _swap("readonly = 1 MIN 1 MAX 2", "readonly = 1 MIN 1 MAX 3"),
        "readonly can be changed to 0 or above 2",
    ),
    "readonly default": (
        _swap("readonly = 1 MIN 1 MAX 2", "readonly = 2 MIN 1 MAX 2"),
        "settings profile does not set readonly = 1",
    ),
    "readonly twice": (_swap(_RO, _RO + _RO), "readonly is set more than once"),
    "writable": (
        _swap(
            "send_progress_in_http_headers CHANGEABLE_IN_READONLY",
            "send_progress_in_http_headers WRITABLE",
        ),
        "send_progress_in_http_headers is WRITABLE",
    ),
    "changeable outside the allow-list": (
        _swap("max_threads CONST,", "max_threads CONST CHANGEABLE_IN_READONLY,"),
        "max_threads may be changed in readonly mode",
    ),
    "unexpected setting": (
        _swap(_RO, _RO + "    max_partitions_to_read = 5,\n"),
        "unexpected setting in the profile: max_partitions_to_read",
    ),
    "inherits a profile": (
        _swap(_RO, _RO + "    PROFILE 'default',\n"),
        "settings item not understood: \"PROFILE 'default'\"",
    ),
    "time default above 30": (
        _swap("max_execution_time = 30 MIN 1", "max_execution_time = 31 MIN 1"),
        "max_execution_time is not 30 s by default and at most 300 s",
    ),
    "time max above 300": (
        _swap("MIN 1 MAX 300", "MIN 1 MAX 301"),
        "max_execution_time is not 30 s by default and at most 300 s",
    ),
    "time may be 0": (
        _swap("max_execution_time = 30 MIN 1 MAX", "max_execution_time = 30 MAX"),
        "max_execution_time may be set to 0 (no time limit): MIN 1 is missing",
    ),
    "rows above the cap": (
        _swap("MAX 10001", "MAX 10002"),
        "max_result_rows is not bounded to 1..10001",
    ),
    "rows may be 0": (
        _swap("max_result_rows MIN 1", "max_result_rows MIN 0"),
        "max_result_rows is not bounded to 1..10001",
    ),
    "second alter profile": (
        _swap(_ALTER_PROFILE, _ALTER_PROFILE + _ALTER_PROFILE),
        "expected one ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS, found 2",
    ),
    "alter profile other clause": (
        _CH + "ALTER SETTINGS PROFILE analytics_ro_profile ADD SETTINGS readonly = 0;\n",
        "settings changed outside the profile's ALTER: "
        "ALTER SETTINGS PROFILE analytics_ro_profile ADD SETTINGS readonly = 0",
    ),
    "second profile": (
        _CH + "CREATE PROFILE other SETTINGS readonly = 0;\n",
        "settings changed outside the profile's ALTER: CREATE PROFILE other SETTINGS readonly = 0",
    ),
    "profile created twice": (
        _swap(_CREATE_PROFILE, _CREATE_PROFILE + _CREATE_PROFILE),
        "expected one CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile, found 2",
    ),
    "profile never created": (
        _swap(_CREATE_PROFILE, ""),
        "expected one CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile, found 0",
    ),
    "settings on create profile": (
        _swap(
            "PROFILE IF NOT EXISTS analytics_ro_profile;",
            "PROFILE IF NOT EXISTS analytics_ro_profile SETTINGS readonly = 1;",
        ),
        "the profile's CREATE is not bare (settings go in its ALTER): "
        "CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile SETTINGS readonly = 1",
    ),
    "profile or replace": (
        _swap(
            "PROFILE IF NOT EXISTS analytics_ro_profile;",
            "PROFILE OR REPLACE analytics_ro_profile;",
        ),
        _OR_REPLACE + "CREATE SETTINGS PROFILE OR REPLACE analytics_ro_profile",
    ),
    "profile created without if not exists": (
        _swap("PROFILE IF NOT EXISTS analytics_ro_profile;", "PROFILE analytics_ro_profile;"),
        "IF NOT EXISTS is missing, so a rerun fails: CREATE SETTINGS PROFILE analytics_ro_profile",
    ),
    "role or replace": (
        _swap(_CREATE_ROLE, "CREATE ROLE OR REPLACE analytics_ro;\n"),
        _OR_REPLACE + "CREATE ROLE OR REPLACE analytics_ro",
    ),
    "or replace behind a comment": (
        _CH + "CREATE ROLE OR /* x */ REPLACE analytics_ro;\n",
        _OR_REPLACE + "CREATE ROLE OR /* x */ REPLACE analytics_ro",
    ),
    "role created without if not exists": (
        _swap(_CREATE_ROLE, "CREATE ROLE analytics_ro;\n"),
        "IF NOT EXISTS is missing, so a rerun fails: CREATE ROLE analytics_ro",
    ),
    "role created twice": (
        _swap(_CREATE_ROLE, _CREATE_ROLE + _CREATE_ROLE),
        "expected one CREATE ROLE IF NOT EXISTS analytics_ro, found 2",
    ),
    "role never created": (
        _swap(_CREATE_ROLE, ""),
        "expected one CREATE ROLE IF NOT EXISTS analytics_ro, found 0",
    ),
    "role binding missing": (_swap(_BIND, ""), _UNBOUND + "(found 0)"),
    "role bound twice": (_swap(_BIND, _BIND + _BIND), _UNBOUND + "(found 2)"),
    "role settings": (
        _swap(
            "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile';",
            "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile', max_threads = 64;",
        ),
        "settings changed outside the profile's ALTER: "
        "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile', max_threads = 64",
    ),
    "role created with settings": (
        _swap(_CREATE_ROLE, "CREATE ROLE IF NOT EXISTS analytics_ro SETTINGS readonly = 0;\n"),
        "settings changed outside the profile's ALTER: "
        "CREATE ROLE IF NOT EXISTS analytics_ro SETTINGS readonly = 0",
    ),
    "user on another profile": (
        _CH + "ALTER USER aa_reader SETTINGS PROFILE 'default';\n",
        "settings changed outside the profile's ALTER: "
        "ALTER USER aa_reader SETTINGS PROFILE 'default'",
    ),
    "user settings after the profile": (
        _swap(
            "DEFAULT ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile';",
            "DEFAULT ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile', readonly = 0;",
        ),
        "settings changed outside the profile's ALTER: ALTER USER aa_reader DEFAULT ROLE "
        "analytics_ro SETTINGS PROFILE 'analytics_ro_profile', readonly = 0",
    ),
    "another role bound": (
        _swap("ALTER ROLE analytics_ro SETTINGS", "ALTER ROLE x SETTINGS"),
        _UNBOUND + "(found 0)",
    ),
}


@pytest.mark.parametrize("name", sorted(PROFILE_RULES))
def test_each_profile_rule_is_reported_alone(name: str) -> None:
    sql, message = PROFILE_RULES[name]
    assert role_sql_problems(sql, "clickhouse") == [message]


# Independent copies of the D-64 lists (EK L-10): not imported from roles.py.
D64_CONST = (
    "max_memory_usage",
    "max_threads",
    "max_bytes_to_read",
    "max_rows_to_read",
    "max_result_bytes",
)
D64_CHANGEABLE = {
    "readonly",
    "max_execution_time",
    "max_result_rows",
    "result_overflow_mode",
    "send_progress_in_http_headers",
    "http_headers_progress_interval_ms",
}


def test_profile_lists_match_d64() -> None:
    from agentic_analytics.warehouse.roles import CH_CHANGEABLE, CH_CONST, MCP_MAX_EXECUTION_S

    assert set(CH_CONST) == set(D64_CONST)
    assert set(CH_CHANGEABLE) == D64_CHANGEABLE
    assert MCP_MAX_EXECUTION_S == 300
    profile = _CH.split("CREATE ROLE")[0]
    for name in D64_CONST:
        assert re.search(rf"^    {name} CONST[,;]$", profile, re.M), name
    for name in D64_CHANGEABLE:
        assert re.search(rf"^    {name}\b.* CHANGEABLE_IN_READONLY[,;]$", profile, re.M), name


@pytest.mark.parametrize("name", D64_CONST)
def test_every_const_limit_is_required(name: str) -> None:
    sql = re.sub(rf"(?m)^    {name} CONST([,;])$", rf"    {name}\1", _CH)
    assert sql != _CH
    assert role_sql_problems(sql, "clickhouse") == [f"{name} is not CONST"]


@pytest.mark.parametrize(
    "item",
    [
        "max_bytes_to_read changeable_in_readonly",  # case
        "max_bytes_to_read\n\n      CHANGEABLE_IN_READONLY",  # line breaks
        "max_bytes_to_read = 'a,b' CHANGEABLE_IN_READONLY",  # a comma inside a value
        "max_bytes_to_read /* , */ CHANGEABLE_IN_READONLY",  # a block comment
        "`max_bytes_to_read` CHANGEABLE_IN_READONLY",  # a quoted name
    ],
)
def test_changeable_spellings_are_caught(item: str) -> None:
    assert role_sql_problems(_swap(_RO, _RO + f"    {item},\n"), "clickhouse")


def test_comments_with_separators_do_not_change_the_verdict() -> None:
    sql = _swap(_RO, "    -- readonly; may, here CHANGEABLE_IN_READONLY\n" + _RO)
    assert role_sql_problems(sql, "clickhouse") == []


def test_profile_settings_must_be_set_by_exactly_one_alter() -> None:
    assert _ALTER_PROFILE.startswith("ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS\n")
    assert _ALTER_PROFILE.endswith("max_result_bytes CONST;\n")
    problems = role_sql_problems(_swap(_ALTER_PROFILE, ""), "clickhouse")
    assert "expected one ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS, found 0" in problems
    assert "settings profile does not set readonly = 1" in problems


def test_a_second_alter_is_read_on_its_own() -> None:
    """Each ALTER ... SETTINGS replaces the list, so a later one must hold every rule alone."""
    loose = _ALTER_PROFILE.replace(_RO, "    readonly = 0,\n")
    assert loose != _ALTER_PROFILE
    problems = role_sql_problems(_swap(_ALTER_PROFILE, _ALTER_PROFILE + loose), "clickhouse")
    assert "expected one ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS, found 2" in problems
    assert "settings profile does not set readonly = 1" in problems


# ---- N1.7 round 2: statements that take the role from the Query API endpoint user (L-7) ---------


@pytest.mark.parametrize(
    "stmt",
    [
        "REVOKE analytics_ro FROM ALL",
        "REVOKE analytics_ro FROM aa_reader",
        "REVOKE SELECT ON uk.* FROM analytics_ro",
        "ALTER ROLE analytics_ro RENAME TO analytics_ro_old",
        "ALTER SETTINGS PROFILE analytics_ro_profile RENAME TO p2",
        "RENAME TABLE uk.a TO uk.b",
    ],
)
def test_revoke_and_rename_are_destructive(stmt: str) -> None:
    """Each takes `analytics_ro` (or its profile) away from ClickHouse Cloud's Query API endpoint
    user just as OR REPLACE did, so each is reported as destructive, by name."""
    problems = role_sql_problems(_CH + stmt + ";\n", "clickhouse")
    assert f"destructive statement: {stmt}" in problems
    assert role_sql_problems(_CH, "clickhouse") == []
