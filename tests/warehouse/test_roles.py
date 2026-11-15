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
    assert re.search(r"readonly = 1,", sql)
    assert "max_execution_time = 30 MAX 30 CHANGEABLE_IN_READONLY" in sql
    assert "CREATE ROLE OR REPLACE analytics_ro SETTINGS PROFILE 'analytics_ro_profile'" in sql
    for g in grants:
        words = set(re.findall(r"[A-Z0-9_]+", g.upper()))
        assert not words & set(CH_WRITE_WORDS), g
        assert "*.*" not in g
    assert role_sql_problems(sql, "clickhouse") == []


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
    "readonly missing": _CH.replace("    readonly = 1,\n", ""),
    "readonly zero": _CH.replace("readonly = 1", "readonly = 0"),
    "long timeout": _CH.replace(
        "max_execution_time = 30 MAX 30", "max_execution_time = 3600 MAX 3600"
    ),
    "profile unbound": _CH.replace(" SETTINGS PROFILE 'analytics_ro_profile';\nGRANT", ";\nGRANT"),
    "other role to user": _CH + "GRANT default_admin TO aa_reader;\n",
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
