"""Probes loader (LOD-04, B9, H7): `listing_notes` with planted instruction rows on both engines.

The probes database is named the way the read-only role SQL already grants it
(`warehouse.roles.pack_role_sql`): ClickHouse `<database>_probes`, Snowflake
`<DATABASE>.<SCHEMA>_PROBES`. The SQL comes only from the pack's `probes/<engine>.sql`; it replaces
the table (`CREATE OR REPLACE`, so no stale schema survives) and inserts the fixed rows, so reruns
are idempotent. Only `INSERT … VALUES` is allowed, with no external function anywhere.
Runs on an injected admin client or connection.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from agentic_analytics.load import clickhouse as ch
from agentic_analytics.load import snowflake as sf
from agentic_analytics.load._common import (
    Clock,
    LoaderError,
    LoadResult,
    Step,
    plan,
    render,
    utc_now,
)
from agentic_analytics.packs import Pack, read_pack_text
from agentic_analytics.schemas import Engine

PROBES_TABLE = "listing_notes"


def clickhouse_probes_database(pack: Pack) -> str:
    return f"{ch.database(pack)}_probes"


def snowflake_probes_database(pack: Pack) -> tuple[str, str]:
    catalog, db = sf.database(pack)
    return catalog, f"{db}_PROBES"


def clickhouse_steps(pack: Pack) -> list[Step]:
    pdb = clickhouse_probes_database(pack)
    table = f"{pdb}.{PROBES_TABLE}"
    sql = render(read_pack_text(pack, "probes/clickhouse.sql"), {"probes_database": pdb})
    allowed = {
        "create_database": [pdb],
        "create_table": [table],
        "insert": [table],
    }
    return plan(sql, allowed, engine="clickhouse")


def snowflake_steps(pack: Pack) -> list[Step]:
    catalog, pdb = snowflake_probes_database(pack)
    table = f"{pdb}.{PROBES_TABLE}"
    sql = render(
        read_pack_text(pack, "probes/snowflake.sql"),
        {"catalog": catalog, "probes_database": pdb},
    )
    allowed = {
        "create_database": [catalog],
        "create_schema": [pdb],
        "create_table": [table],
        "insert": [table],
    }
    return plan(sql, allowed, engine="snowflake")


def _finish(
    engine: Engine, target: str, table: str, n: int, sent: list[str], t0: dt.datetime, now: Clock
) -> LoadResult:
    if n == 0:
        raise LoaderError(f"{table} is empty after the probes load")
    return LoadResult(
        engine=engine,
        target=target,
        action="loaded",
        rows_before={},
        rows_after={table: n},
        statements=tuple(sent),
        started_at=t0,
        finished_at=now(),
    )


def load_probes_clickhouse(pack: Pack, client: Any, *, now: Clock = utc_now) -> LoadResult:
    steps = clickhouse_steps(pack)
    pdb = clickhouse_probes_database(pack)
    table = f"{pdb}.{PROBES_TABLE}"
    t0 = now()
    sent: list[str] = []
    for st in steps:
        client.command(st.sql)
        sent.append(st.sql)
    n = client.query(f"SELECT count() FROM {table}").result_rows[0][0]
    return _finish("clickhouse", pdb, table, int(n), sent, t0, now)


def load_probes_snowflake(pack: Pack, conn: Any, *, now: Clock = utc_now) -> LoadResult:
    steps = snowflake_steps(pack)
    _, pdb = snowflake_probes_database(pack)
    table = f"{pdb}.{PROBES_TABLE}"
    t0 = now()
    sent: list[str] = []
    cur = conn.cursor()
    try:
        for st in steps:
            cur.execute(st.sql)
            sent.append(st.sql)
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    finally:
        cur.close()
    return _finish("snowflake", pdb, table, int(row[0]) if row else 0, sent, t0, now)


__all__ = [
    "PROBES_TABLE",
    "clickhouse_probes_database",
    "clickhouse_steps",
    "load_probes_clickhouse",
    "load_probes_snowflake",
    "snowflake_probes_database",
    "snowflake_steps",
]
