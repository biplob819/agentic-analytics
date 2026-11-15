"""`agentic-analytics doctor` core (EXE-06). The CLI wiring lives in cli.py.

The report names every Tech Spec §4 environment variable as set or missing and never holds a
value; any text taken from a driver error is scrubbed of every environment value before it is
stored. With executors injected, it checks each engine end to end:

- the executor reaches the engine, and its guard refuses a write and `url()` before sending;
- the server itself refuses a write (`INSERT ... LIMIT 0`, harmless even if accepted, and
  `CREATE TABLE`) and, on ClickHouse, `SELECT * FROM url(...)`, sent raw past the guard with no
  per-query settings, so the role and settings profile are what is tested;
- the ClickHouse profile reads back `readonly = 1` and `max_execution_time <= 30`; the Snowflake
  session runs as the read-only role, and the warehouse reads back `AUTO_SUSPEND` <= 60 s with
  `AUTO_RESUME`. The ClickHouse Cloud idle timeout is a console setting with no SQL read-back, so
  that check is reported as skipped.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agentic_analytics.schemas import DatasetPack
from agentic_analytics.warehouse.clickhouse import ClickHouseExecutor
from agentic_analytics.warehouse.roles import SF_ROLE
from agentic_analytics.warehouse.snowflake import SnowflakeExecutor

ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLICKHOUSE_MCP_KEY_ID",
    "CLICKHOUSE_MCP_KEY_SECRET",
    "CLICKHOUSE_SERVICE_ID",
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_RO_USER",
    "CLICKHOUSE_RO_PASSWORD",
    "CLICKHOUSE_ADMIN_USER",
    "CLICKHOUSE_ADMIN_PASSWORD",
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_RO_USER",
    "SNOWFLAKE_RO_PRIVATE_KEY_PATH",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ADMIN_USER",
    "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "DEMO_PASSCODE",
    "EVALKIT_DEMO_DATABASE_URL",
)

CH_REFUSAL_CODES = frozenset({164, 291, 497})  # READONLY, DATABASE_ACCESS_DENIED, ACCESS_DENIED
CH_REFUSAL_NAMES = ("READONLY", "ACCESS_DENIED", "Not enough privileges")
SF_REFUSAL_ERRNOS = frozenset({3001})  # insufficient privileges
SF_REFUSAL_SQLSTATES = frozenset({"42501"})
PROBE_URL = "http://127.0.0.1:9/agentic-analytics-doctor.csv"
WRITE_PROBE_TABLE = "aa_doctor_write_check"
MAX_AUTO_SUSPEND_S = 60
MAX_EXECUTION_TIME_S = 30

Status = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class DoctorReport:
    env: tuple[tuple[str, bool], ...]
    checks: tuple[Check, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "env": {name: ("set" if present else "missing") for name, present in self.env},
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def render(self) -> str:
        width = max(len(n) for n, _ in self.env) if self.env else 0
        lines = ["Environment (names only, values are never shown):"]
        lines += [f"  {n:<{width}}  {'set' if p else 'missing'}" for n, p in self.env]
        lines.append("Checks:")
        for c in self.checks:
            tail = f"  {c.detail}" if c.detail else ""
            lines.append(f"  {c.status.upper():<4}  {c.name}{tail}")
        lines.append("doctor: OK" if self.ok else "doctor: FAILED")
        return "\n".join(lines) + "\n"


def env_presence(environ: Mapping[str, str]) -> tuple[tuple[str, bool], ...]:
    return tuple((n, bool((environ.get(n) or "").strip())) for n in ENV_VARS)


_SECRET_NAME = re.compile(r"KEY|SECRET|PASS|TOKEN|CREDENTIAL|DATABASE_URL|DSN", re.I)
WITHHELD = "[detail withheld: it may contain an environment value]"
MIN_MASKABLE = 4


class _Scrubber:
    """Removes the value of every §4 variable (and any secret-looking variable) from driver text.

    Values of 4+ characters are replaced wherever they occur, longest first. A shorter value
    cannot be masked without mangling the text, so if one still occurs the whole detail is
    withheld: safe by default, nothing is ever shown partially masked.
    """

    def __init__(self, environ: Mapping[str, str], extra: tuple[str, ...] = ()) -> None:
        names = {n for n in environ if n in ENV_VARS or _SECRET_NAME.search(n)}
        raw = [environ[n] for n in names] + list(extra)
        values = {x for v in raw if v and v.strip() for x in (v, v.strip())}
        self._long = sorted((v for v in values if len(v) >= MIN_MASKABLE), key=len, reverse=True)
        self._short = sorted(v for v in values if len(v) < MIN_MASKABLE)

    def __call__(self, text: str, limit: int = 240) -> str:
        for v in self._long:
            text = text.replace(v, "***")
        if any(v in text for v in self._short):
            return WITHHELD
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"


def _expect_refused(
    name: str,
    action: Callable[[], Any],
    is_refusal: Callable[[BaseException], bool],
    scrub: _Scrubber,
    on_accept: Callable[[], Any] | None = None,
) -> Check:
    try:
        action()
    except Exception as e:
        if is_refusal(e):
            return Check(name, "pass", "refused by the server")
        code = getattr(e, "code", None) or getattr(e, "errno", None)
        return Check(name, "fail", scrub(f"failed for another reason (code {code}): {e}"))
    if on_accept is not None:
        with contextlib.suppress(Exception):
            on_accept()
    return Check(name, "fail", "accepted: the role is not read-only")


def _ch_refusal(e: BaseException) -> bool:
    return getattr(e, "code", None) in CH_REFUSAL_CODES or any(
        n in str(e) for n in CH_REFUSAL_NAMES
    )


def _sf_refusal(e: BaseException) -> bool:
    return (
        getattr(e, "errno", None) in SF_REFUSAL_ERRNOS
        or getattr(e, "sqlstate", None) in SF_REFUSAL_SQLSTATES
        or "Insufficient privileges" in str(e)
    )


def _guard_checks(prefix: str, executor: Any, table: str, url_sql: str) -> list[Check]:
    out = []
    for what, sql in (
        ("write", f"INSERT INTO {table} SELECT * FROM {table} LIMIT 0"),
        ("url", url_sql),
    ):
        ex = executor.execute(sql, row_cap=1)
        ok = ex.error == "guard" and ex.query_id is None
        out.append(
            Check(
                f"{prefix}.guard_refuses_{what}",
                "pass" if ok else "fail",
                "refused before sending" if ok else f"executor returned error={ex.error}",
            )
        )
    return out


def clickhouse_checks(
    executor: ClickHouseExecutor, *, database: str, table: str, scrub: _Scrubber
) -> list[Check]:
    full = f"{database}.{table}"
    ping = executor.execute("SELECT 1 AS ok", row_cap=1)
    if ping.error is not None:
        return [Check("clickhouse.reachable", "fail", f"SELECT 1 returned error={ping.error}")]
    checks = [Check("clickhouse.reachable", "pass", f"query_id {ping.query_id}")]
    url_sql = f"SELECT * FROM url('{PROBE_URL}', 'CSV', 'x String') LIMIT 1"
    checks += _guard_checks("clickhouse", executor, full, url_sql)
    client = executor.client  # raw: no guard, no per-query settings
    probe = f"{database}.{WRITE_PROBE_TABLE}"
    checks += [
        _expect_refused(
            "clickhouse.role_refuses_insert",
            lambda: client.command(f"INSERT INTO {full} SELECT * FROM {full} LIMIT 0"),
            _ch_refusal,
            scrub,
        ),
        _expect_refused(
            "clickhouse.role_refuses_create",
            lambda: client.command(f"CREATE TABLE {probe} (x UInt8) ENGINE = Memory"),
            _ch_refusal,
            scrub,
            on_accept=lambda: client.command(f"DROP TABLE IF EXISTS {probe}"),
        ),
        _expect_refused(
            "clickhouse.role_refuses_url", lambda: client.query(url_sql), _ch_refusal, scrub
        ),
    ]
    try:
        res = client.query(
            "SELECT name, value FROM system.settings "
            "WHERE name IN ('readonly', 'max_execution_time')"
        )
        got = {str(n): str(v) for n, v in res.result_rows}
        ro, met = got.get("readonly"), got.get("max_execution_time", "0")
        good = ro == "1" and met.isdigit() and 0 < int(met) <= MAX_EXECUTION_TIME_S
        checks.append(
            Check(
                "clickhouse.profile",
                "pass" if good else "fail",
                f"readonly={ro} max_execution_time={met}",
            )
        )
    except Exception as e:
        checks.append(Check("clickhouse.profile", "fail", scrub(f"could not read settings: {e}")))
    checks.append(
        Check(
            "clickhouse.idle_timeout",
            "skip",
            "ClickHouse Cloud idle timeout is a console setting; no SQL read-back",
        )
    )
    return checks


def _sf_rows(conn: Any, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        cols = [str(d[0]).lower() for d in (cur.description or ())]
        return cols, [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def snowflake_checks(
    executor: SnowflakeExecutor, *, database: str, table: str, scrub: _Scrubber
) -> list[Check]:
    full = f"{database}.{table}"
    who = executor.execute("SELECT CURRENT_ROLE() AS r", row_cap=1)
    if who.error is not None or who.result is None or not who.result.rows:
        return [Check("snowflake.reachable", "fail", f"SELECT CURRENT_ROLE() error={who.error}")]
    role = str(who.result.rows[0][0])
    checks = [
        Check("snowflake.reachable", "pass", f"query_id {who.query_id}"),
        Check(
            "snowflake.role",
            "pass" if role.upper() == SF_ROLE else "fail",
            f"session role {role}, required {SF_ROLE}",
        ),
    ]
    checks += _guard_checks("snowflake", executor, full, "SELECT $1 FROM @~/doctor.csv")
    conn = executor.connection

    def run(sql: str) -> Any:
        return _sf_rows(conn, sql)

    probe = f"{database}.{WRITE_PROBE_TABLE.upper()}"
    checks += [
        _expect_refused(
            "snowflake.role_refuses_insert",
            lambda: run(f"INSERT INTO {full} SELECT * FROM {full} LIMIT 0"),
            _sf_refusal,
            scrub,
        ),
        _expect_refused(
            "snowflake.role_refuses_create",
            lambda: run(f"CREATE TABLE {probe} (X INT)"),
            _sf_refusal,
            scrub,
            on_accept=lambda: run(f"DROP TABLE IF EXISTS {probe}"),
        ),
    ]
    try:
        cols, rows = run(f"SHOW WAREHOUSES LIKE '{executor.warehouse}'")
        if not rows:
            raise LookupError(f"warehouse {executor.warehouse} is not visible to the role")
        rec = dict(zip(cols, rows[0], strict=False))
        suspend = rec.get("auto_suspend")
        resume = str(rec.get("auto_resume")).lower() in ("true", "1")
        secs = int(str(suspend)) if str(suspend).isdigit() else None
        good = secs is not None and 0 < secs <= MAX_AUTO_SUSPEND_S and resume
        checks.append(
            Check(
                "snowflake.auto_suspend",
                "pass" if good else "fail",
                f"AUTO_SUSPEND={suspend} AUTO_RESUME={rec.get('auto_resume')}",
            )
        )
    except Exception as e:
        checks.append(Check("snowflake.auto_suspend", "fail", scrub(f"read-back failed: {e}")))
    return checks


def _safe(name: str, fn: Callable[[], list[Check]], scrub: _Scrubber) -> list[Check]:
    try:
        return fn()
    except Exception as e:  # a config error or a driver bug: report it, never crash doctor
        return [Check(name, "fail", scrub(f"{type(e).__name__}: {e}"))]


_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def run_doctor(
    environ: Mapping[str, str] | None = None,
    *,
    clickhouse: ClickHouseExecutor | None = None,
    snowflake: SnowflakeExecutor | None = None,
    pack: DatasetPack | None = None,
    clickhouse_target: tuple[str, str] = ("uk", "uk_price_paid"),
    snowflake_target: tuple[str, str] = ("ANALYTICS.UK", "UK_PRICE_PAID"),
) -> DoctorReport:
    """Build the doctor report. Engines without an executor are reported as skipped."""
    env = os.environ if environ is None else environ
    scrub = _Scrubber(env)
    if pack is not None:
        if "clickhouse" in pack.engines:
            clickhouse_target = (pack.databases["clickhouse"], pack.tables[0])
        if "snowflake" in pack.engines:
            snowflake_target = (pack.databases["snowflake"], pack.tables[0].upper())
    for part in (*clickhouse_target, *snowflake_target):
        if not _PLAIN.match(part):
            raise ValueError(f"{part!r} is not a plain table or database name")
    checks: list[Check] = []
    if clickhouse is None:
        checks.append(Check("clickhouse", "skip", "no executor (ClickHouse env vars missing)"))
    else:
        db, tbl = clickhouse_target
        checks += _safe(
            "clickhouse.error",
            lambda: clickhouse_checks(clickhouse, database=db, table=tbl, scrub=scrub),
            scrub,
        )
    if snowflake is None:
        checks.append(Check("snowflake", "skip", "no executor (Snowflake env vars missing)"))
    else:
        db, tbl = snowflake_target
        checks += _safe(
            "snowflake.error",
            lambda: snowflake_checks(snowflake, database=db, table=tbl, scrub=scrub),
            scrub,
        )
    return DoctorReport(env=env_presence(env), checks=tuple(checks))


def executors_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[ClickHouseExecutor | None, SnowflakeExecutor | None]:
    """Build the read-only executors from §4 variables; an engine with any variable missing is
    None. Nothing connects until the first query."""
    env = os.environ if environ is None else environ

    def get(n: str) -> str:
        return (env.get(n) or "").strip()

    ch = None
    if all(get(n) for n in ("CLICKHOUSE_HOST", "CLICKHOUSE_RO_USER", "CLICKHOUSE_RO_PASSWORD")):
        ch = ClickHouseExecutor(
            get("CLICKHOUSE_HOST"), get("CLICKHOUSE_RO_USER"), get("CLICKHOUSE_RO_PASSWORD")
        )
    sf = None
    sf_vars = (
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_RO_USER",
        "SNOWFLAKE_RO_PRIVATE_KEY_PATH",
        "SNOWFLAKE_WAREHOUSE",
    )
    if all(get(n) for n in sf_vars):
        sf = SnowflakeExecutor(
            get("SNOWFLAKE_ACCOUNT"),
            get("SNOWFLAKE_RO_USER"),
            get("SNOWFLAKE_RO_PRIVATE_KEY_PATH"),
            get("SNOWFLAKE_WAREHOUSE"),
            SF_ROLE,
        )
    return ch, sf


__all__ = [
    "ENV_VARS",
    "Check",
    "DoctorReport",
    "clickhouse_checks",
    "env_presence",
    "executors_from_env",
    "run_doctor",
    "snowflake_checks",
]
