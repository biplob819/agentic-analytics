"""ClickHouse loader (LOD-01, FR1, D-11): the documented `CREATE` and `INSERT … FROM url(…)`.

The statements come only from the pack's `load/clickhouse.sql`, rendered with the validated
database name. The loader runs on an injected **admin** client (never the read-only executors)
and makes the documented statements idempotent without changing what they do:

- `CREATE DATABASE` / `CREATE TABLE` get `IF NOT EXISTS`;
- an existing table whose columns (`DESCRIBE TABLE`) differ from the pack's is refused with the
  difference named, unless `force`, which drops and recreates it;
- each `INSERT` is skipped when its table already has rows, unless `force`, which first runs
  `TRUNCATE TABLE` and then reloads.

Every statement must be a `CREATE DATABASE` of the pack database, a `CREATE TABLE`, `TRUNCATE`
or `INSERT` on one of the pack's tables in it, with no external table function or engine in its
body except the one documented `url()` source of the `INSERT` (`_common.plan`); anything else
stops the load before it is sent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from agentic_analytics.load._common import (
    Action,
    Clock,
    LoaderError,
    LoadResult,
    Step,
    ident,
    plan,
    render,
    schema_mismatch,
    table_columns,
    utc_now,
)
from agentic_analytics.packs import Pack, read_pack_text

LOAD_FILE = "load/clickhouse.sql"
INSERT_TIMEOUT_S = 3600  # the documented url() insert of ~27 M rows takes minutes

_CREATE = re.compile(r"^(\s*CREATE\s+(?:DATABASE|TABLE))\s+(?!IF\s+NOT\s+EXISTS\b)", re.I)


def make_idempotent(stmt: str) -> str:
    """Add `IF NOT EXISTS` to a `CREATE DATABASE` / `CREATE TABLE`; other statements unchanged."""
    return _CREATE.sub(r"\1 IF NOT EXISTS ", stmt, count=1)


def database(pack: Pack) -> str:
    if "clickhouse" not in pack.manifest.engines:
        raise LoaderError(f"pack {pack.ref} has no ClickHouse database")
    return ident(pack.manifest.databases["clickhouse"], "ClickHouse database")


def rendered_sql(pack: Pack) -> str:
    """`load/clickhouse.sql` with `{database}` filled in, exactly as the pack holds it."""
    return render(read_pack_text(pack, LOAD_FILE), {"database": database(pack)})


def load_steps(pack: Pack) -> list[Step]:
    db = database(pack)
    tables = [f"{db}.{ident(t, 'table')}" for t in pack.manifest.tables]
    return plan(
        rendered_sql(pack),
        {"create_database": [db], "create_table": tables, "insert": tables},
        engine="clickhouse",
        select_source="url",
    )


def _count(client: Any, table: str) -> int:
    rows = client.query(f"SELECT count() FROM {table}").result_rows
    n = rows[0][0]
    if isinstance(n, bool) or not isinstance(n, int):
        raise LoaderError(f"row count of {table} is not an integer: {n!r}")
    return n


def _columns(client: Any, table: str) -> list[tuple[str, str]]:
    return [(str(r[0]), str(r[1])) for r in client.query(f"DESCRIBE TABLE {table}").result_rows]


def load_clickhouse(
    pack: Pack,
    client: Any,
    *,
    force: bool = False,
    now: Clock = utc_now,
) -> LoadResult:
    """Run the pack's ClickHouse load on an admin client (`clickhouse_connect` client API:
    `command(sql)` and `query(sql).result_rows`)."""
    steps = load_steps(pack)
    db = database(pack)
    tables = [f"{db}.{t}" for t in pack.manifest.tables]
    started = now()
    sent: list[str] = []

    def run(sql: str) -> None:
        client.command(sql)
        sent.append(sql)

    replaced: list[str] = []
    for st in steps:
        if not st.kind.startswith("create_"):
            continue
        run(make_idempotent(st.sql))
        if st.kind == "create_table":
            table = st.targets[0]
            diff = schema_mismatch("clickhouse", table_columns(st.sql), _columns(client, table))
            if diff:
                if not force:
                    raise LoaderError(
                        f"{table} exists with other columns ({diff}); rerun with --force to "
                        "replace it"
                    )
                run(f"DROP TABLE {table}")
                run(make_idempotent(st.sql))
                replaced.append(table)
    before = {t: _count(client, t) for t in tables}
    inserted: list[str] = []
    for st in steps:
        if st.kind != "insert":
            continue
        table = st.targets[0]
        if before[table] > 0:
            if not force:
                continue
            run(f"TRUNCATE TABLE {table}")
        run(st.sql)
        inserted.append(table)
    after = {t: _count(client, t) for t in tables}
    empty = [t for t in inserted if after[t] == 0]
    if empty:
        raise LoaderError(f"INSERT left {', '.join(empty)} empty")
    action: Action
    if not inserted:
        action = "skipped"
    elif replaced or any(before[t] > 0 for t in inserted):
        action = "reloaded"
    else:
        action = "loaded"
    return LoadResult(
        engine="clickhouse",
        target=db,
        action=action,
        rows_before=before,
        rows_after=after,
        statements=tuple(sent),
        started_at=started,
        finished_at=now(),
    )


def admin_client_from_env(environ: Mapping[str, str] | None = None) -> Any:  # pragma: no cover
    """A clickhouse-connect client with the ADMIN credentials (loader and role setup only)."""
    import clickhouse_connect

    env = os.environ if environ is None else environ
    names = ("CLICKHOUSE_HOST", "CLICKHOUSE_ADMIN_USER", "CLICKHOUSE_ADMIN_PASSWORD")
    missing = [n for n in names if not (env.get(n) or "").strip()]
    if missing:
        raise LoaderError(f"missing environment variables: {', '.join(missing)}")
    return clickhouse_connect.get_client(
        host=env["CLICKHOUSE_HOST"].strip(),
        username=env["CLICKHOUSE_ADMIN_USER"].strip(),
        password=env["CLICKHOUSE_ADMIN_PASSWORD"],
        port=8443,
        secure=True,
        query_limit=0,
        connect_timeout=10,
        send_receive_timeout=INSERT_TIMEOUT_S,
    )


__all__ = [
    "admin_client_from_env",
    "load_clickhouse",
    "load_steps",
    "make_idempotent",
    "rendered_sql",
]
