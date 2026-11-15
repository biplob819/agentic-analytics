"""Read-only role SQL for both engines, rendered from a dataset pack (EXE-04, D-19, §8).

The admin runs the rendered SQL once per account (N1.3 wires it into setup). Two steps live
outside SQL and are checked live by `doctor` in the measurement node: creating the read-only users
with their password / RSA public key, and binding the ClickHouse Cloud MCP Query API endpoint to
`analytics_ro` (a console / Cloud API setting).

`role_sql_problems(sql, engine)` re-reads rendered SQL and lists every grant that is not
read-only, so a template edit that widens the role fails its test (EK L-1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from agentic_analytics.schemas import DatasetPack, Engine
from agentic_analytics.warehouse._common import ROW_CAP, STATEMENT_TIMEOUT_S

CH_ROLE: Final = "analytics_ro"
CH_PROFILE: Final = "analytics_ro_profile"
SF_ROLE: Final = "ANALYTICS_RO"
SF_WAREHOUSE: Final = "ANALYTICS_XS"
SF_AUTO_SUSPEND_S: Final = 60
CORTEX_DB_ROLE: Final = "SNOWFLAKE.CORTEX_ANALYST_USER"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"{what} {name!r} is not a plain SQL identifier")
    return name


def clickhouse_role_sql(
    databases: Sequence[str],
    *,
    role: str = CH_ROLE,
    profile: str = CH_PROFILE,
    user: str | None = None,
    max_execution_time: int = STATEMENT_TIMEOUT_S,
    row_cap: int = ROW_CAP,
) -> str:
    dbs = [_ident(d, "database") for d in databases]
    if not dbs:
        raise ValueError("at least one database is needed")
    role, profile = _ident(role, "role"), _ident(profile, "profile")
    lines = [
        f"-- {role}: read-only role for the executor and the MCP Query API endpoint (D-19).",
        "-- Bind the ClickHouse Cloud MCP Query API endpoint to this role in the console.",
        "-- readonly = 1 refuses writes and setting changes; the executor may only lower its",
        "-- own limits (CHANGEABLE_IN_READONLY). No SOURCES grant: url(), s3(), file(),",
        "-- remote() and the other external table functions are refused by the server too.",
        f"CREATE SETTINGS PROFILE OR REPLACE {profile} SETTINGS",
        "    readonly = 1,",
        f"    max_execution_time = {max_execution_time} MAX {max_execution_time}"
        " CHANGEABLE_IN_READONLY,",
        f"    max_result_rows MIN 1 MAX {row_cap + 1} CHANGEABLE_IN_READONLY,",
        "    result_overflow_mode CHANGEABLE_IN_READONLY;",
        f"CREATE ROLE OR REPLACE {role} SETTINGS PROFILE '{profile}';",
    ]
    lines += [f"GRANT SELECT ON {db}.* TO {role};" for db in dbs]
    if user is not None:
        user = _ident(user, "user")
        lines += [
            f"GRANT {role} TO {user};",
            f"ALTER USER {user} DEFAULT ROLE {role} SETTINGS PROFILE '{profile}';",
        ]
    return "\n".join(lines) + "\n"


def snowflake_role_sql(
    database: str,
    schemas: Sequence[str],
    *,
    semantic_schema: str | None = None,
    warehouse: str = SF_WAREHOUSE,
    role: str = SF_ROLE,
    user: str | None = None,
) -> str:
    db = _ident(database, "database")
    schs = [_ident(s, "schema") for s in schemas]
    if not schs:
        raise ValueError("at least one schema is needed")
    wh, role = _ident(warehouse, "warehouse"), _ident(role, "role")
    sem = _ident(semantic_schema, "schema") if semantic_schema else schs[0]
    wh_opts = f"WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = {SF_AUTO_SUSPEND_S} AUTO_RESUME = TRUE"
    lines = [
        f"-- {role}: select-only role for the executor and Cortex Analyst (D-19, §8).",
        f"CREATE ROLE IF NOT EXISTS {role};",
        f"CREATE WAREHOUSE IF NOT EXISTS {wh} WITH {wh_opts} INITIALLY_SUSPENDED = TRUE;",
        f"ALTER WAREHOUSE {wh} SET {wh_opts};",
        f"GRANT USAGE ON WAREHOUSE {wh} TO ROLE {role};",
        f"GRANT USAGE ON DATABASE {db} TO ROLE {role};",
    ]
    for s in schs:
        lines += [
            f"GRANT USAGE ON SCHEMA {db}.{s} TO ROLE {role};",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {db}.{s} TO ROLE {role};",
            f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {db}.{s} TO ROLE {role};",
        ]
    lines += [
        f"GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA {db}.{sem} TO ROLE {role};",
        f"GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA {db}.{sem} TO ROLE {role};",
        f"GRANT DATABASE ROLE {CORTEX_DB_ROLE} TO ROLE {role};",
    ]
    if user is not None:
        user = _ident(user, "user")
        lines += [
            f"GRANT ROLE {role} TO USER {user};",
            f"ALTER USER {user} SET DEFAULT_ROLE = {role} DEFAULT_WAREHOUSE = {wh};",
        ]
    return "\n".join(lines) + "\n"


def pack_role_sql(
    pack: DatasetPack,
    engine: Engine,
    *,
    user: str | None = None,
    warehouse: str = SF_WAREHOUSE,
) -> str:
    """Role SQL for a pack's databases plus its probes database / schema (`<name>_probes`)."""
    if engine not in pack.engines:
        raise ValueError(f"pack {pack.ref} has no {engine} database")
    target = pack.databases[engine]
    if engine == "clickhouse":
        return clickhouse_role_sql([target, f"{target}_probes"], user=user)
    db, sep, schema = target.partition(".")
    if not sep:
        raise ValueError(f"Snowflake database {target!r} must be DATABASE.SCHEMA")
    return snowflake_role_sql(
        db, [schema, f"{schema}_PROBES"], semantic_schema=schema, warehouse=warehouse, user=user
    )


# ---- checker ---------------------------------------------------------------------------------

_CH_GRANT_PRIV = re.compile(
    r"^GRANT\s+(?P<privs>.+?)\s+ON\s+(?P<target>\S+)\s+TO\s+(?P<to>\S+)$", re.I
)
_CH_GRANT_ROLE = re.compile(r"^GRANT\s+(?P<role>[A-Za-z_]\w*)\s+TO\s+(?P<to>[A-Za-z_]\w*)$", re.I)
_SF_GRANT_PRIV = re.compile(
    r"^GRANT\s+(?P<privs>.+?)\s+ON\s+(?P<obj>.+?)\s+TO\s+ROLE\s+(?P<to>\w+)$", re.I
)
_SF_GRANT_DB_ROLE = re.compile(r"^GRANT\s+DATABASE\s+ROLE\s+(?P<r>\S+)\s+TO\s+ROLE\s+\w+$", re.I)
_SF_GRANT_ROLE = re.compile(r"^GRANT\s+ROLE\s+(?P<r>\w+)\s+TO\s+USER\s+\w+$", re.I)
_SF_OBJECTS = re.compile(
    r"^(WAREHOUSE|DATABASE|SCHEMA|(ALL|FUTURE)\s+(TABLES|SEMANTIC\s+VIEWS)\s+IN\s+SCHEMA)\s+\S+$",
    re.I,
)


def _statements(sql: str) -> list[str]:
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def role_sql_problems(sql: str, engine: Engine) -> list[str]:
    """Every way rendered role SQL is not read-only; empty means the grants are read-only."""
    problems: list[str] = []
    stmts = _statements(sql)
    grants = [s for s in stmts if s.upper().startswith("GRANT")]
    for s in stmts:
        u = s.upper()
        if "WITH GRANT OPTION" in u or "WITH ADMIN OPTION" in u:
            problems.append(f"grant option: {s}")
        if u.startswith(("DROP", "INSERT", "DELETE", "UPDATE", "TRUNCATE")):
            problems.append(f"destructive statement: {s}")
    if not grants:
        problems.append("no grants at all")
    if engine == "clickhouse":
        problems += _clickhouse_problems(stmts, grants)
    else:
        problems += _snowflake_problems(stmts, grants)
    return problems


def _clickhouse_problems(stmts: list[str], grants: list[str]) -> list[str]:
    out: list[str] = []
    selects = 0
    for g in grants:
        m = _CH_GRANT_PRIV.match(g)
        if m:
            privs = [p.strip().upper() for p in m["privs"].split(",")]
            if privs != ["SELECT"]:
                out.append(f"non-SELECT privilege: {g}")
            if m["target"].startswith("*"):
                out.append(f"grant on every database: {g}")
            selects += 1
            continue
        m = _CH_GRANT_ROLE.match(g)
        if m and m["role"].lower() == CH_ROLE:
            continue
        out.append(f"unexpected grant: {g}")
    if not selects:
        out.append("no SELECT grant")
    profile = " ".join(s for s in stmts if s.upper().startswith("CREATE SETTINGS PROFILE"))
    if not re.search(r"\breadonly\s*=\s*1\b", profile, re.I):
        out.append("settings profile does not set readonly = 1")
    m = re.search(r"\bmax_execution_time\s*=\s*(\d+)\s+MAX\s+(\d+)", profile, re.I)
    if not m or int(m[1]) > STATEMENT_TIMEOUT_S or int(m[2]) > STATEMENT_TIMEOUT_S:
        out.append(f"max_execution_time is not capped at {STATEMENT_TIMEOUT_S} s")
    if re.search(r"\breadonly\b[^,;]*CHANGEABLE_IN_READONLY|\breadonly\s*=\s*0", profile, re.I):
        out.append("readonly can be changed")
    if not any(re.match(rf"CREATE ROLE .*SETTINGS PROFILE '{CH_PROFILE}'", s, re.I) for s in stmts):
        out.append("role is not bound to the read-only settings profile")
    return out


def _snowflake_problems(stmts: list[str], grants: list[str]) -> list[str]:
    out: list[str] = []
    seen_cortex = seen_future = False
    for g in grants:
        m = _SF_GRANT_DB_ROLE.match(g)
        if m:
            if m["r"].upper() != CORTEX_DB_ROLE:
                out.append(f"unexpected database role: {g}")
            seen_cortex = True
            continue
        m = _SF_GRANT_ROLE.match(g)
        if m:
            if m["r"].upper() != SF_ROLE:
                out.append(f"unexpected role grant: {g}")
            continue
        m = _SF_GRANT_PRIV.match(g)
        if not m:
            out.append(f"unexpected grant: {g}")
            continue
        privs = [p.strip().upper() for p in m["privs"].split(",")]
        obj = m["obj"].upper()
        if not _SF_OBJECTS.match(obj):
            out.append(f"grant on an unexpected object: {g}")
        allowed = {"USAGE"} if obj.split()[0] in ("WAREHOUSE", "DATABASE", "SCHEMA") else {"SELECT"}
        if any(p not in allowed for p in privs):
            out.append(f"privilege beyond {sorted(allowed)}: {g}")
        if obj.startswith("FUTURE TABLES"):
            seen_future = True
    if not seen_cortex:
        out.append(f"missing DATABASE ROLE {CORTEX_DB_ROLE}")
    if not seen_future:
        out.append("missing SELECT on future tables")
    wh = [s for s in stmts if re.match(r"(CREATE|ALTER) WAREHOUSE", s, re.I)]
    if not wh:
        out.append("no warehouse settings")
    for s in wh:
        m = re.search(r"AUTO_SUSPEND\s*=\s*(\d+)", s, re.I)
        if not m or not 0 < int(m[1]) <= SF_AUTO_SUSPEND_S:
            out.append(f"AUTO_SUSPEND is not 1..{SF_AUTO_SUSPEND_S} s: {s}")
        if not re.search(r"AUTO_RESUME\s*=\s*TRUE", s, re.I):
            out.append(f"AUTO_RESUME is not TRUE: {s}")
        if not re.search(r"WAREHOUSE_SIZE\s*=\s*'X-?SMALL'", s, re.I):
            out.append(f"warehouse is not XSMALL: {s}")
        if s.upper().startswith("CREATE") and "INITIALLY_SUSPENDED = TRUE" not in s.upper():
            out.append(f"warehouse is not created suspended: {s}")
    return out


__all__ = [
    "CH_PROFILE",
    "CH_ROLE",
    "SF_ROLE",
    "SF_WAREHOUSE",
    "clickhouse_role_sql",
    "pack_role_sql",
    "role_sql_problems",
    "snowflake_role_sql",
]
