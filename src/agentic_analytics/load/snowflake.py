"""Snowflake loader (LOD-02, FR1, D-11, H3): snapshot → internal stage → raw table → typed table.

The statements come only from the pack's `load/snowflake.sql` (generated from the pack's one
transform spec, so the typed table is built with the same transforms as ClickHouse). Placeholders:
`{catalog}` (database), `{database}` (`DATABASE.SCHEMA` from the pack), `{local_file}` (the
gzipped snapshot) and `{stage_file}` (its name on the stage).

Run on an injected **admin** DB-API connection. Setup always runs: database and schema if
missing; file format, internal stage and raw table replaced (`CREATE OR REPLACE`), so no stale
object survives; typed table created if missing, and an existing typed table whose columns
(`DESCRIBE TABLE`) differ from the pack's is refused with the difference named, unless `force`,
which drops and recreates it. If every pack table already has rows and `force` is off, the load
is skipped and nothing is downloaded. Otherwise the snapshot is downloaded (injectable), hashed
and gzipped, checked against the ClickHouse load time when one is given (D-11: within an hour),
and the load phase runs: `PUT`, `COPY … FORCE = TRUE`, truncate typed, `INSERT … SELECT` from the
raw table. The raw and typed row counts must then agree.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic_analytics.load._common import (
    Action,
    Clock,
    LoaderError,
    LoadResult,
    Step,
    ident,
    normalise,
    plan,
    qualified,
    render,
    safe_path,
    schema_mismatch,
    table_columns,
    utc_now,
)
from agentic_analytics.load.snapshot import (
    Downloader,
    SnapshotInfo,
    download_snapshot,
    http_download,
    source_csv_url,
    within_load_window,
)
from agentic_analytics.packs import Pack, read_pack_text

LOAD_FILE = "load/snowflake.sql"
RAW_SUFFIX = "_raw"
STAGE_SUFFIX = "_stage"
FORMAT_SUFFIX = "_csv"


def database(pack: Pack) -> tuple[str, str]:
    """(`DATABASE`, `DATABASE.SCHEMA`) from the pack's Snowflake database."""
    if "snowflake" not in pack.manifest.engines:
        raise LoaderError(f"pack {pack.ref} has no Snowflake database")
    catalog, schema = qualified(pack.manifest.databases["snowflake"], 2, "Snowflake database")
    return catalog, f"{catalog}.{schema}"


def rendered_sql(pack: Pack, *, local_file: str, stage_file: str) -> str:
    catalog, db = database(pack)
    params = {
        "catalog": catalog,
        "database": db,
        "local_file": safe_path(local_file, "local file"),
        "stage_file": safe_path(stage_file, "stage file"),
    }
    if "/" in stage_file or "\\" in stage_file:
        raise ValueError(f"stage file {stage_file!r} must be a bare file name")
    return render(read_pack_text(pack, LOAD_FILE), params)


def load_steps(pack: Pack, *, local_file: str, stage_file: str) -> list[Step]:
    catalog, db = database(pack)
    tables = [f"{db}.{ident(t, 'table')}" for t in pack.manifest.tables]
    raws = [f"{t}{RAW_SUFFIX}" for t in tables]
    stages = [f"{t}{STAGE_SUFFIX}" for t in tables]
    allowed = {
        "create_database": [catalog],
        "create_schema": [db],
        "create_file_format": [f"{t}{FORMAT_SUFFIX}" for t in tables],
        "create_stage": stages,
        "create_table": tables + raws,
        "truncate": tables,
        "put": stages,
        "copy": raws + stages,
        "insert": tables,
    }
    steps = plan(
        rendered_sql(pack, local_file=local_file, stage_file=stage_file),
        allowed,
        engine="snowflake",
        select_source="table",
        sources=raws,
    )
    # PUT stages only this load's snapshot file, and COPY reads only that file from the stage.
    put = re.compile(rf"^PUT 'file://{re.escape(local_file)}' @\S+(?: |$)", re.I)
    copy = re.compile(rf"^COPY INTO \S+ FROM @\S+/{re.escape(stage_file)} ", re.I)
    for st in steps:
        flat = normalise(st.sql)
        wrong_put = st.kind == "put" and not put.match(flat)
        wrong_copy = st.kind == "copy" and not copy.match(flat)
        if wrong_put or wrong_copy:
            raise LoaderError(f"{st.kind} of a file other than this load's snapshot: {flat[:99]!r}")
    return steps


def _count(conn: Any, table: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    finally:
        cur.close()
    n = row[0] if row else None
    if isinstance(n, bool) or not isinstance(n, int):
        raise LoaderError(f"row count of {table} is not an integer: {n!r}")
    return n


def _columns(conn: Any, table: str) -> list[tuple[str, str]]:
    cur = conn.cursor()
    try:
        cur.execute(f"DESCRIBE TABLE {table}")
        rows = cur.fetchall()
    finally:
        cur.close()
    return [(str(r[0]), str(r[1])) for r in rows]


def load_snowflake(
    pack: Pack,
    conn: Any,
    *,
    workdir: Path,
    force: bool = False,
    download: Downloader = http_download,
    snapshot: SnapshotInfo | None = None,
    clickhouse_loaded_at: dt.datetime | None = None,
    now: Clock = utc_now,
) -> LoadResult:
    """Run the pack's Snowflake load. Pass `snapshot` to reuse a file already downloaded, and
    `clickhouse_loaded_at` to enforce D-11's one-hour window between the two loads."""
    _, db = database(pack)
    tables = [f"{db}.{ident(t, 'table')}" for t in pack.manifest.tables]
    started = now()
    sent: list[str] = []

    def run(sql: str) -> None:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()
        sent.append(sql)

    # Setup first; a placeholder file name is enough to render and check the CREATE statements.
    replaced: list[str] = []
    for st in load_steps(pack, local_file="/setup", stage_file="setup"):
        if not st.kind.startswith("create_"):
            continue
        run(st.sql)
        if st.kind == "create_table" and st.targets[0] in tables:
            table = st.targets[0]
            diff = schema_mismatch("snowflake", table_columns(st.sql), _columns(conn, table))
            if diff:
                if not force:
                    raise LoaderError(
                        f"{table} exists with other columns ({diff}); rerun with --force to "
                        "replace it"
                    )
                run(f"DROP TABLE {table}")
                run(st.sql)
                replaced.append(table)
    before = {t: _count(conn, t) for t in tables}
    if all(n > 0 for n in before.values()) and not force:
        return LoadResult(
            engine="snowflake",
            target=db,
            action="skipped",
            rows_before=before,
            rows_after=before,
            statements=tuple(sent),
            started_at=started,
            finished_at=now(),
        )

    snap = snapshot or download_snapshot(source_csv_url(pack), workdir, download=download, now=now)
    if clickhouse_loaded_at is not None and not within_load_window(
        clickhouse_loaded_at, snap.downloaded_at
    ):
        raise LoaderError(
            f"snapshot downloaded at {snap.downloaded_at.isoformat()} is more than an hour from "
            f"the ClickHouse load at {clickhouse_loaded_at.isoformat()} (D-11)"
        )
    local = snap.gz_path.resolve().as_posix()
    for st in load_steps(pack, local_file=local, stage_file=snap.stage_name):
        if not st.kind.startswith("create_"):
            run(st.sql)
    after = {t: _count(conn, t) for t in tables}
    for t in tables:
        raw = _count(conn, f"{t}{RAW_SUFFIX}")
        if after[t] == 0 or raw != after[t]:
            raise LoaderError(f"{t} has {after[t]} rows but its raw table has {raw}")
    reloaded = replaced or any(n > 0 for n in before.values())
    action: Action = "reloaded" if reloaded else "loaded"
    return LoadResult(
        engine="snowflake",
        target=db,
        action=action,
        rows_before=before,
        rows_after=after,
        statements=tuple(sent),
        started_at=started,
        finished_at=now(),
        snapshot=snap.to_dict(),
    )


def admin_connection_from_env(environ: Mapping[str, str] | None = None) -> Any:  # pragma: no cover
    """A Snowflake connection with the ADMIN key pair (loader, semantic view, role setup only)."""
    import snowflake.connector

    from agentic_analytics.warehouse.snowflake import load_private_key

    env = os.environ if environ is None else environ
    names = (
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_ADMIN_USER",
        "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH",
        "SNOWFLAKE_WAREHOUSE",
    )
    missing = [n for n in names if not (env.get(n) or "").strip()]
    if missing:
        raise LoaderError(f"missing environment variables: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=env["SNOWFLAKE_ACCOUNT"].strip(),
        user=env["SNOWFLAKE_ADMIN_USER"].strip(),
        private_key=load_private_key(env["SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH"].strip()),
        warehouse=env["SNOWFLAKE_WAREHOUSE"].strip(),
        session_parameters={"QUERY_TAG": "agentic-analytics-loader"},
    )


__all__ = ["admin_connection_from_env", "database", "load_snowflake", "load_steps", "rendered_sql"]
