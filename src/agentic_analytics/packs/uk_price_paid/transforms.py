"""The one transform spec for the UK price paid pack (LOD-02, D-11).

`COLUMNS` says, once, how each typed column is made from the 16 raw CSV fields. Three renderers
read it:

- `render_clickhouse_sql()` gives `load/clickhouse.sql`: ClickHouse's documented `CREATE` and
  `INSERT … SELECT FROM url(…)`, character for character apart from layout and `{database}`.
- `render_snowflake_sql()` gives `load/snowflake.sql`: stage, raw table of 16 VARCHAR columns,
  `COPY` of the staged gzipped CSV, and the typed table built with the same transforms in
  Snowflake SQL.
- `apply_row(raw)` is the same spec in Python. Tests use it to compute expected fingerprints from
  a CSV fixture; the loaders never call it.

Loaders read only the rendered SQL files in the pack (LOD-07); tests assert the files equal these
renderings, so the two engines can only drift by editing this spec.
Regenerate the files with `python -m agentic_analytics.packs.uk_price_paid.transforms`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SOURCE_CSV_URL = (
    "http://prod1.publicdata.landregistry.gov.uk.s3-website-eu-west-1.amazonaws.com/pp-complete.csv"
)
TABLE = "uk_price_paid"

# The raw CSV fields, in file order (ClickHouse docs' url() structure).
RAW_COLUMNS: tuple[str, ...] = (
    "uuid_string",
    "price_string",
    "time",
    "postcode",
    "a",
    "b",
    "c",
    "addr1",
    "addr2",
    "street",
    "locality",
    "town",
    "district",
    "county",
    "d",
    "e",
)

Op = Literal["uint32", "date", "split", "map", "flag", "copy"]

TYPE_MAP: tuple[tuple[str, str], ...] = (
    ("T", "terraced"),
    ("S", "semi-detached"),
    ("D", "detached"),
    ("F", "flat"),
    ("O", "other"),
)
DURATION_MAP: tuple[tuple[str, str], ...] = (
    ("F", "freehold"),
    ("L", "leasehold"),
    ("U", "unknown"),
)


@dataclass(frozen=True)
class Column:
    """One typed output column: its name, the transform, the raw field it reads, and its type on
    each engine. `part` is the 1-based split part; `flag` the value meaning 1; `mapping` the code
    to label pairs. An unmapped code is not a valid label: ClickHouse's `transform` passes it
    through and the `Enum8` column then refuses the INSERT; Snowflake's `CASE … ELSE code` keeps
    it, and the parity counts by type and duration fail. Either way the load does not pass."""

    name: str
    op: Op
    source: str
    ch_type: str
    sf_type: str
    part: int = 0
    flag: str = ""
    mapping: tuple[tuple[str, str], ...] = ()


_LC = "LowCardinality(String)"
COLUMNS: tuple[Column, ...] = (
    Column("price", "uint32", "price_string", "UInt32", "NUMBER(10, 0)"),
    Column("date", "date", "time", "Date", "DATE"),
    Column("postcode1", "split", "postcode", _LC, "VARCHAR", part=1),
    Column("postcode2", "split", "postcode", _LC, "VARCHAR", part=2),
    Column(
        "type",
        "map",
        "a",
        "Enum8('terraced' = 1, 'semi-detached' = 2, 'detached' = 3, 'flat' = 4, 'other' = 0)",
        "VARCHAR",
        mapping=TYPE_MAP,
    ),
    Column("is_new", "flag", "b", "UInt8", "NUMBER(1, 0)", flag="Y"),
    Column(
        "duration",
        "map",
        "c",
        "Enum8('freehold' = 1, 'leasehold' = 2, 'unknown' = 0)",
        "VARCHAR",
        mapping=DURATION_MAP,
    ),
    Column("addr1", "copy", "addr1", "String", "VARCHAR"),
    Column("addr2", "copy", "addr2", "String", "VARCHAR"),
    Column("street", "copy", "street", _LC, "VARCHAR"),
    Column("locality", "copy", "locality", _LC, "VARCHAR"),
    Column("town", "copy", "town", _LC, "VARCHAR"),
    Column("district", "copy", "district", _LC, "VARCHAR"),
    Column("county", "copy", "county", _LC, "VARCHAR"),
)
CH_ORDER_BY: tuple[str, ...] = ("postcode1", "postcode2", "addr1", "addr2")


def output_columns() -> tuple[str, ...]:
    return tuple(c.name for c in COLUMNS)


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ---- ClickHouse -------------------------------------------------------------------------------


def clickhouse_expr(c: Column) -> str:
    if c.op == "uint32":
        return f"toUInt32({c.source})"
    if c.op == "date":
        return f"parseDateTimeBestEffortUS({c.source})"
    if c.op == "split":
        return f"splitByChar(' ', {c.source})[{c.part}]"
    if c.op == "map":
        codes = ",".join(_q(k) for k, _ in c.mapping)
        labels = ",".join(_q(v) for _, v in c.mapping)
        return f"transform({c.source}, [{codes}], [{labels}])"
    if c.op == "flag":
        return f"{c.source} = {_q(c.flag)}"
    return c.source


def _select_item(expr: str, name: str) -> str:
    return name if expr == name else f"{expr} AS {name}"


def clickhouse_select_items() -> tuple[str, ...]:
    return tuple(_select_item(clickhouse_expr(c), c.name) for c in COLUMNS)


def render_clickhouse_sql() -> str:
    cols = ",\n".join(f"    {c.name} {c.ch_type}" for c in COLUMNS)
    items = ",\n".join(f"    {i}" for i in clickhouse_select_items())
    structure = ", ".join(f"{r} String" for r in RAW_COLUMNS)
    return (
        "-- LOD-01: ClickHouse's documented load for the UK price paid dataset, templated with\n"
        "-- {database}. Generated from transforms.py; do not edit by hand. The loader adds\n"
        "-- IF NOT EXISTS to the two CREATE statements, refuses an existing table whose columns\n"
        "-- differ (unless --force, which replaces it), and skips the INSERT when the table\n"
        "-- already has rows (unless --force, which truncates first).\n"
        "CREATE DATABASE {database};\n"
        "\n"
        f"CREATE TABLE {{database}}.{TABLE}\n"
        "(\n"
        f"{cols}\n"
        ")\n"
        "ENGINE = MergeTree\n"
        f"ORDER BY ({', '.join(CH_ORDER_BY)});\n"
        "\n"
        f"INSERT INTO {{database}}.{TABLE}\n"
        "SELECT\n"
        f"{items}\n"
        "FROM url(\n"
        f"    {_q(SOURCE_CSV_URL)},\n"
        "    'CSV',\n"
        f"    {_q(structure)}\n"
        ") SETTINGS max_http_get_redirects=10;\n"
    )


# ---- Snowflake --------------------------------------------------------------------------------


def snowflake_expr(c: Column) -> str:
    if c.op == "uint32":
        return f"TO_NUMBER({c.source}, 10, 0)"
    if c.op == "date":
        return f"TRY_TO_DATE(LEFT({c.source}, 10), 'YYYY-MM-DD')"
    if c.op == "split":
        return f"SPLIT_PART({c.source}, ' ', {c.part})"
    if c.op == "map":
        whens = " ".join(f"WHEN {_q(k)} THEN {_q(v)}" for k, v in c.mapping)
        return f"CASE {c.source} {whens} ELSE {c.source} END"
    if c.op == "flag":
        return f"IFF({c.source} = {_q(c.flag)}, 1, 0)"
    return c.source


def snowflake_select_items() -> tuple[str, ...]:
    return tuple(_select_item(snowflake_expr(c), c.name) for c in COLUMNS)


SF_FILE_FORMAT = f"{TABLE}_csv"
SF_STAGE = f"{TABLE}_stage"
SF_RAW = f"{TABLE}_raw"


def render_snowflake_sql() -> str:
    raw_cols = ",\n".join(f"    {r} VARCHAR" for r in RAW_COLUMNS)
    typed_cols = ",\n".join(f"    {c.name} {c.sf_type}" for c in COLUMNS)
    items = ",\n".join(f"    {i}" for i in snowflake_select_items())
    return (
        "-- LOD-02: Snowflake load for the UK price paid dataset, templated with {catalog}\n"
        "-- (database), {database} (DATABASE.SCHEMA), {local_file} (the gzipped snapshot on this\n"
        "-- machine) and {stage_file} (its name on the stage). Generated from transforms.py; do\n"
        "-- not edit by hand. CREATE statements are setup and always run: the file format, stage\n"
        "-- and raw table are replaced each time; the typed table is created if missing and the\n"
        "-- loader refuses it if its columns differ (unless --force, which replaces it). The rest\n"
        "-- is the load, skipped when the typed table already has rows (unless --force).\n"
        "-- Empty fields stay empty strings (EMPTY_FIELD_AS_NULL = FALSE, NULL_IF = ()), as in\n"
        "-- ClickHouse's String columns.\n"
        "CREATE DATABASE IF NOT EXISTS {catalog};\n"
        "\n"
        "CREATE SCHEMA IF NOT EXISTS {database};\n"
        "\n"
        f"CREATE OR REPLACE FILE FORMAT {{database}}.{SF_FILE_FORMAT}\n"
        "    TYPE = CSV\n"
        "    FIELD_OPTIONALLY_ENCLOSED_BY = '\"'\n"
        "    COMPRESSION = GZIP\n"
        "    EMPTY_FIELD_AS_NULL = FALSE\n"
        "    NULL_IF = ()\n"
        "    ENCODING = 'UTF8';\n"
        "\n"
        f"CREATE OR REPLACE STAGE {{database}}.{SF_STAGE}\n"
        f"    FILE_FORMAT = {{database}}.{SF_FILE_FORMAT};\n"
        "\n"
        f"CREATE OR REPLACE TABLE {{database}}.{SF_RAW}\n"
        "(\n"
        f"{raw_cols}\n"
        ");\n"
        "\n"
        f"CREATE TABLE IF NOT EXISTS {{database}}.{TABLE}\n"
        "(\n"
        f"{typed_cols}\n"
        ");\n"
        "\n"
        f"PUT 'file://{{local_file}}' @{{database}}.{SF_STAGE}"
        " AUTO_COMPRESS = FALSE OVERWRITE = TRUE;\n"
        "\n"
        f"COPY INTO {{database}}.{SF_RAW}\n"
        f"    FROM @{{database}}.{SF_STAGE}/{{stage_file}}\n"
        f"    FILE_FORMAT = (FORMAT_NAME = '{{database}}.{SF_FILE_FORMAT}')\n"
        "    ON_ERROR = ABORT_STATEMENT\n"
        "    FORCE = TRUE;\n"
        "\n"
        f"TRUNCATE TABLE {{database}}.{TABLE};\n"
        "\n"
        f"INSERT INTO {{database}}.{TABLE}\n"
        "SELECT\n"
        f"{items}\n"
        f"FROM {{database}}.{SF_RAW};\n"
    )


# ---- Python reference (tests only) ------------------------------------------------------------


def _python_value(c: Column, raw: dict[str, str]) -> object:
    v = raw[c.source]
    if c.op == "uint32":
        return int(v)
    if c.op == "date":
        return dt.date.fromisoformat(v[:10])
    if c.op == "split":
        parts = v.split(" ")
        return parts[c.part - 1] if c.part <= len(parts) else ""
    if c.op == "map":
        return dict(c.mapping).get(v, v)
    if c.op == "flag":
        return 1 if v == c.flag else 0
    return v


def apply_row(raw: Sequence[str]) -> dict[str, object]:
    """One raw CSV record (16 fields) to the typed row, by the same spec."""
    if len(raw) != len(RAW_COLUMNS):
        raise ValueError(f"expected {len(RAW_COLUMNS)} fields, got {len(raw)}")
    named = dict(zip(RAW_COLUMNS, raw, strict=True))
    return {c.name: _python_value(c, named) for c in COLUMNS}


def write_files(pack_dir: Path | None = None) -> None:  # pragma: no cover - maintainer helper
    root = pack_dir or Path(__file__).resolve().parent
    (root / "load" / "clickhouse.sql").write_text(
        render_clickhouse_sql(), encoding="utf-8", newline="\n"
    )
    (root / "load" / "snowflake.sql").write_text(
        render_snowflake_sql(), encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":  # pragma: no cover
    write_files()
