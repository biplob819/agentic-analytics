"""Read-only role SQL for both engines, rendered from a dataset pack (EXE-04, D-19, §8).

`agentic-analytics setup` (N1.7) re-applies the rendered SQL on every run, so every statement
is idempotent and non-destructive: the ClickHouse role and settings profile are created
IF NOT EXISTS and then ALTERed, never replaced. ClickHouse Cloud grants `analytics_ro` to the
generic Query API endpoint user (`queryEndpoint:<id>`, the remote MCP server's user); replacing
the role drops that grant and MCP queries then run with no role and no profile (found live).
Two steps live outside SQL and are checked live by `doctor`: creating the read-only users with
their password / RSA public key, and binding the ClickHouse Cloud MCP Query API endpoint to
`analytics_ro` (a console setting).

`role_sql_problems(sql, engine)` re-reads rendered SQL and lists every grant that is not
read-only, and every ClickHouse role or profile statement that would replace, revoke or rename
them, so a template edit that widens or replaces the role fails its test (EK L-1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from agentic_analytics.schemas import DatasetPack, Engine
from agentic_analytics.warehouse._common import ROW_CAP, STATEMENT_TIMEOUT_S

CH_ROLE: Final = "analytics_ro"
CH_PROFILE: Final = "analytics_ro_profile"
# D-63/D-64: the remote MCP server runs every query with readonly = 2 and max_execution_time = 300
# and sets two progress headers, so those may change, within bounds; nothing else may.
MCP_MAX_EXECUTION_S: Final = 300
CH_CHANGEABLE: Final = frozenset(
    {
        "readonly",  # MIN 1 MAX 2: never 0, so writes stay refused
        "max_execution_time",
        "max_result_rows",
        "result_overflow_mode",
        "send_progress_in_http_headers",
        "http_headers_progress_interval_ms",
    }
)
# Resource limits a query's SETTINGS clause may not touch under readonly = 2 (D-64).
CH_CONST: Final = (
    "max_memory_usage",
    "max_threads",
    "max_bytes_to_read",
    "max_rows_to_read",
    "max_result_bytes",
)
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
        "-- readonly = 1 refuses writes. The remote MCP server asks for readonly = 2 and a 300 s",
        "-- limit on every query (D-64), so readonly may move between 1 and 2 (never 0) and the",
        "-- time limit between 1 and 300 s (0 would mean no limit), and the resource limits are",
        "-- CONST. No SOURCES grant: url(), s3(), file(), remote() and the other external table",
        "-- functions are refused too.",
        "-- The role and the profile are created IF NOT EXISTS and then ALTERed, never replaced.",
        "-- ClickHouse Cloud grants this role to the Query API endpoint user (queryEndpoint:<id>),",
        "-- which the remote MCP server runs as, and replacing the role drops that grant: MCP",
        "-- queries then run with no role and without this profile. Replacing the profile alone",
        "-- unbinds it from the role. ALTER ... SETTINGS replaces the whole settings list, so a",
        "-- rerun converges to exactly the settings below.",
        f"CREATE SETTINGS PROFILE IF NOT EXISTS {profile};",
        f"ALTER SETTINGS PROFILE {profile} SETTINGS",
        "    readonly = 1 MIN 1 MAX 2 CHANGEABLE_IN_READONLY,",
        f"    max_execution_time = {max_execution_time} MIN 1 MAX {MCP_MAX_EXECUTION_S}"
        " CHANGEABLE_IN_READONLY,",
        f"    max_result_rows MIN 1 MAX {row_cap + 1} CHANGEABLE_IN_READONLY,",
        "    result_overflow_mode CHANGEABLE_IN_READONLY,",
        "    send_progress_in_http_headers CHANGEABLE_IN_READONLY,",
        "    http_headers_progress_interval_ms CHANGEABLE_IN_READONLY,",
        *(f"    {name} CONST," for name in CH_CONST[:-1]),
        f"    {CH_CONST[-1]} CONST;",
        f"CREATE ROLE IF NOT EXISTS {role};",
        f"ALTER ROLE {role} SETTINGS PROFILE '{profile}';",
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
        # REVOKE and RENAME are destructive too: either one takes the role from ClickHouse
        # Cloud's Query API endpoint user, as replacing it did (L-7)
        if u.startswith(("DROP", "INSERT", "DELETE", "UPDATE", "TRUNCATE", "REVOKE")) or (
            u.startswith(("ALTER", "RENAME")) and re.search(r"\bRENAME\b", u)
        ):
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
    out += _clickhouse_replace_problems(stmts)
    out += _clickhouse_role_problems(stmts)
    out += _clickhouse_settings_problems(stmts)
    return out


_CH_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CH_OR_REPLACE = re.compile(r"\bOR\s+REPLACE\b", re.I)
# `CREATE ROLE|[SETTINGS] PROFILE [IF NOT EXISTS | OR REPLACE] name ...`; OR REPLACE is counted
# here so that it is reported once, by `_clickhouse_replace_problems`.
_CH_CREATE = re.compile(
    r"^CREATE (?P<kind>ROLE|(?:SETTINGS )?PROFILE) (?P<mod>IF NOT EXISTS |OR REPLACE )?"
    r"(?P<name>[A-Za-z_]\w*)(?P<rest>.*)$",
    re.I,
)
_CH_ROLE_BINDING = re.compile(rf"^ALTER ROLE {CH_ROLE} SETTINGS\b", re.I)


def _creates(stmts: list[str], kind: str, name: str) -> list[re.Match[str]]:
    found = (_CH_CREATE.match(s) for s in stmts)
    return [
        m
        for m in found
        if m and m["kind"].upper().endswith(kind) and m["name"].lower() == name.lower()
    ]


def _clickhouse_replace_problems(stmts: list[str]) -> list[str]:
    """No CREATE ... OR REPLACE: ClickHouse Cloud grants the role to the Query API endpoint user,
    and replacing the role (or the profile it is bound to) drops that link (found live)."""
    return [
        f"OR REPLACE drops the grants and bindings of the role or profile (the Query API"
        f" endpoint's grant among them): {s}"
        for s in stmts
        if s.upper().startswith("CREATE") and _CH_OR_REPLACE.search(_CH_BLOCK_COMMENT.sub(" ", s))
    ]


def _clickhouse_role_problems(stmts: list[str]) -> list[str]:
    """The role is created once, IF NOT EXISTS, and bound to the profile by one ALTER ROLE (what
    its SETTINGS may hold is checked with every other access statement)."""
    out: list[str] = []
    creates = _creates(stmts, "ROLE", CH_ROLE)
    if len(creates) != 1:
        out.append(f"expected one CREATE ROLE IF NOT EXISTS {CH_ROLE}, found {len(creates)}")
    out += [f"IF NOT EXISTS is missing, so a rerun fails: {m[0]}" for m in creates if not m["mod"]]
    bindings = sum(1 for s in stmts if _CH_ROLE_BINDING.match(s))
    if bindings != 1:
        out.append(
            f"role is not bound to the read-only settings profile by one ALTER ROLE {CH_ROLE}"
            f" SETTINGS PROFILE '{CH_PROFILE}' (found {bindings})"
        )
    return out


_CH_PROFILE_STMT = re.compile(r"^(CREATE|ALTER|ATTACH)\s+(SETTINGS\s+)?PROFILE\b", re.I)
_CH_PROFILE_HEAD = re.compile(
    rf"^ALTER (SETTINGS )?PROFILE {CH_PROFILE} SETTINGS (?P<body>.+)$",
    re.I,
)
_CH_ACCESS_STMT = re.compile(r"^(CREATE|ALTER|ATTACH)\s+(ROLE|USER)\b", re.I)
_CH_VALUE = r"(?:'(?:[^'\\]|\\.|'')*'|[-+\w.]+)"
_CH_ITEM = re.compile(
    rf"^(?P<name>[A-Za-z_]\w*)(?:\s*=\s*(?P<value>{_CH_VALUE}))?"
    rf"(?P<rest>(?:\s+(?:MIN|MAX)(?:\s*=\s*|\s+){_CH_VALUE}"
    r"|\s+(?:CONST|READONLY|WRITABLE|CHANGEABLE_IN_READONLY))*)$",
    re.I,
)
_CH_BOUND = re.compile(rf"\b(MIN|MAX)(?:\s*=\s*|\s+)({_CH_VALUE})", re.I)


def _split_items(body: str) -> list[str]:
    """Settings items split on commas outside quoted values."""
    items, buf, quoted, i = [], [], False, 0
    while i < len(body):
        ch = body[i]
        if quoted and ch == "\\" and i + 1 < len(body):
            buf.append(body[i : i + 2])
            i += 2
            continue
        if ch == "'":
            quoted = not quoted
        if ch == "," and not quoted:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    items.append("".join(buf).strip())
    return items


def _int(text: str | None) -> int | None:
    return int(text) if text is not None and text.isdigit() else None


def _clickhouse_settings_problems(stmts: list[str]) -> list[str]:
    """The one settings profile (D-63/D-64): one bare `CREATE SETTINGS PROFILE IF NOT EXISTS`,
    then one `ALTER SETTINGS PROFILE ... SETTINGS`, which replaces the whole list. Each ALTER is
    read item by item: every setting is set once, only the allow-listed ones are
    CHANGEABLE_IN_READONLY, none is WRITABLE, the CONST set is CONST. No other statement sets
    settings (another profile statement, settings on a role or a user)."""
    out: list[str] = []
    creates = _creates(stmts, "PROFILE", CH_PROFILE)
    bodies: list[str] = []
    for s in stmts:
        if not _CH_PROFILE_STMT.match(s) or any(m[0] == s for m in creates):
            continue
        head = _CH_PROFILE_HEAD.match(s)
        if head:
            bodies.append(head["body"])
        else:
            out.append(f"settings changed outside the profile's ALTER: {s}")
    for m in creates:
        if not m["mod"]:
            out.append(f"IF NOT EXISTS is missing, so a rerun fails: {m[0]}")
        if m["rest"].strip():
            out.append(f"the profile's CREATE is not bare (settings go in its ALTER): {m[0]}")
    for s in stmts:
        if _CH_ACCESS_STMT.match(s) and re.search(r"\bSETTINGS\b", s, re.I):
            tail = re.split(r"\bSETTINGS\b", s, maxsplit=1, flags=re.I)[1].strip()
            if not re.fullmatch(rf"PROFILE '{CH_PROFILE}'", tail, re.I):
                out.append(f"settings changed outside the profile's ALTER: {s}")
    if len(creates) != 1:
        out.append(
            f"expected one CREATE SETTINGS PROFILE IF NOT EXISTS {CH_PROFILE}, found {len(creates)}"
        )
    if len(bodies) != 1:
        out.append(
            f"expected one ALTER SETTINGS PROFILE {CH_PROFILE} SETTINGS, found {len(bodies)}"
        )
    # Each ALTER replaces the list, so each must hold every rule on its own.
    for body in bodies or [None]:
        out += _profile_items_problems(body)
    return out


def _profile_items_problems(body: str | None) -> list[str]:
    out: list[str] = []
    items: dict[str, tuple[str | None, dict[str, str], set[str]]] = {}
    if body is not None:
        for item in _split_items(body):
            m = _CH_ITEM.match(item)
            if not m:
                out.append(f"settings item not understood: {item!r}")
                continue
            name = m["name"].lower()
            bounds = {b.upper(): v for b, v in _CH_BOUND.findall(m["rest"])}
            flags = {w.upper() for w in m["rest"].split()} & {
                "CONST",
                "READONLY",
                "WRITABLE",
                "CHANGEABLE_IN_READONLY",
            }
            if name in items:
                out.append(f"{name} is set more than once")
            items[name] = (m["value"], bounds, flags)
            if "WRITABLE" in flags:
                out.append(f"{name} is WRITABLE")
            if "CHANGEABLE_IN_READONLY" in flags and name not in CH_CHANGEABLE:
                out.append(f"{name} may be changed in readonly mode")
            if name not in CH_CHANGEABLE and name not in CH_CONST:
                out.append(f"unexpected setting in the profile: {name}")
    value, bounds, flags = items.get("readonly", (None, {}, set()))
    if value != "1":
        out.append("settings profile does not set readonly = 1")
    lo, hi = _int(bounds.get("MIN")), _int(bounds.get("MAX"))
    if "CHANGEABLE_IN_READONLY" in flags and not (lo == 1 and hi in (1, 2)):
        out.append("readonly can be changed to 0 or above 2")
    value, bounds, _ = items.get("max_execution_time", (None, {}, set()))
    default, lo, hi = _int(value), _int(bounds.get("MIN")), _int(bounds.get("MAX"))
    if (
        default is None
        or hi is None
        or not 0 < default <= STATEMENT_TIMEOUT_S
        or hi > MCP_MAX_EXECUTION_S
    ):
        out.append(
            f"max_execution_time is not {STATEMENT_TIMEOUT_S} s by default and at most"
            f" {MCP_MAX_EXECUTION_S} s"
        )
    if lo is None or lo < 1:
        out.append("max_execution_time may be set to 0 (no time limit): MIN 1 is missing")
    _, bounds, _ = items.get("max_result_rows", (None, {}, set()))
    lo, hi = _int(bounds.get("MIN")), _int(bounds.get("MAX"))
    if lo is None or lo < 1 or hi is None or hi > ROW_CAP + 1:
        out.append(f"max_result_rows is not bounded to 1..{ROW_CAP + 1}")
    for name in CH_CONST:
        if not items.get(name, (None, {}, set()))[2] & {"CONST", "READONLY"}:
            out.append(f"{name} is not CONST")
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
