"""Stateful fake warehouses for the loader tests (no network, no accounts).

`FakeClickHouse` stands in for a clickhouse-connect admin client: it keeps tables in memory,
honours `IF NOT EXISTS`, and simulates the documented `INSERT … FROM url(…)` by reading the CSV
fixture and applying the pack's transform spec. `FakeSnowflake` stands in for a DB-API connection:
`PUT` reads the gzipped file from disk, `COPY` parses it with the csv module into 16-field raw
rows (honouring the file format's empty-field and `NULL_IF` settings), and `INSERT … SELECT FROM
raw` applies the transform spec with SQL NULL propagation. Both remember each table's columns
for `DESCRIBE TABLE` (Snowflake shows its display types, e.g. `VARCHAR(16777216)`). Both answer
the pack's parity fingerprints from their rows, ClickHouse with lowercase column names and
Snowflake upper-case, as the real drivers do. Answers are real `clickhouse_connect`
`QueryResult`s and Snowflake `ResultMetadata` descriptions (EK L-17).
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import re
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlglot
from clickhouse_connect.driver.exceptions import DatabaseError
from sqlglot import exp

from agentic_analytics.load._common import table_columns
from agentic_analytics.load.parity import load_parity_spec, rendered_query
from agentic_analytics.packs import Pack
from agentic_analytics.packs.uk_price_paid.transforms import COLUMNS, RAW_COLUMNS, apply_row
from tests.warehouse.fakes import ch_result, sf_column

FIXTURE = Path(__file__).parent / "fixtures" / "pp-sample.csv"
Row = dict[str, object]
Transform = Callable[[list[Any]], Row]
TEXT_COLUMNS = (
    "postcode1",
    "postcode2",
    "addr1",
    "addr2",
    "street",
    "locality",
    "town",
    "district",
    "county",
)


def apply_nullable(raw: list[Any]) -> Row:
    """`apply_row` with SQL NULL propagation: a NULL raw field gives NULL in every column made
    from it, except the `IFF` flag, which is 0."""
    row = apply_row(["" if v is None else v for v in raw])
    named = dict(zip(RAW_COLUMNS, raw, strict=True))
    for c in COLUMNS:
        if named[c.source] is None and c.op != "flag":
            row[c.name] = None
    return row


def read_fixture(path: Path = FIXTURE) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return [r for r in csv.reader(f)]


def fingerprints(rows: list[Row]) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    """The parity fingerprints computed in Python from typed rows (columns, rows)."""

    def grouped(col: str) -> list[tuple[Any, ...]]:
        return sorted(Counter(r[col] for r in rows).items(), key=lambda kv: str(kv[0]))

    dates = [r["date"] for r in rows if r["date"] is not None]
    return {
        "row_count": (["n"], [(len(rows),)]),
        "sum_price": (["total_price"], [(sum(int(str(r["price"])) for r in rows),)]),
        "by_type": (["property_type", "n"], grouped("type")),
        "by_duration": (["tenure", "n"], grouped("duration")),
        "by_is_new": (["new_build", "n"], grouped("is_new")),
        "date_range": (
            ["min_date", "max_date"],
            [(str(min(dates)), str(max(dates)))] if dates else [(None, None)],  # type: ignore[type-var]
        ),
        "distinct_towns": (["towns"], [(len({r["town"] for r in rows} - {None}),)]),
        "per_year": (
            ["sale_year", "n"],
            sorted(
                Counter(
                    r["date"].year if isinstance(r["date"], dt.date) else None for r in rows
                ).items(),
                key=lambda kv: (kv[0] is None, kv[0] or 0),
            ),
        ),
        **{
            f"text_{c}": (
                ["distinct_values", "total_chars", "empty_values", "null_values"],
                [
                    (
                        len({r[c] for r in rows} - {None}),
                        sum(len(str(r[c])) for r in rows if r[c] is not None),
                        sum(1 for r in rows if r[c] == ""),
                        sum(1 for r in rows if r[c] is None),
                    )
                ],
            )
            for c in TEXT_COLUMNS
        },
    }


def _sf_display(typ: str) -> str:
    t = "".join(typ.split()).upper()
    return "VARCHAR(16777216)" if t == "VARCHAR" else t


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _py(v: Any) -> Any:
    # sqlglot's ClickHouse dialect wraps each VALUES cell in a one-element Tuple
    while isinstance(v, exp.Tuple) and len(v.expressions) == 1:
        v = v.expressions[0]
    return v.to_py()


def values_rows(sql: str, dialect: str) -> list[tuple[Any, ...]]:
    """The literal rows of an `INSERT … VALUES` statement."""
    tree = sqlglot.parse_one(sql, read=dialect)
    values = tree.find(exp.Values)
    assert values is not None, sql
    return [tuple(_py(v) for v in tup.expressions) for tup in values.expressions]


class _Engine:
    """Shared table store and fingerprint answering."""

    upper = False

    def __init__(self, pack: Pack, engine: str) -> None:
        self.pack = pack
        self.tables: dict[str, list[Any]] = {}
        self.schemas: dict[str, list[tuple[str, str]]] = {}
        self.databases: set[str] = set()
        self.statements: list[str] = []
        self._fp_by_sql = {
            _flat(rendered_query(pack, fp, engine)): fp.name  # type: ignore[arg-type]
            for fp in load_parity_spec(pack).fingerprints
        }

    def count(self, table: str) -> int:
        if table not in self.tables:
            raise LookupError(f"unknown table {table}")
        return len(self.tables[table])

    def describe(self, table: str) -> list[tuple[Any, ...]]:
        self.count(table)
        return [(n, t, "", "", "", "", "") for n, t in self.schemas[table]]

    def fingerprint(self, sql: str, table: str) -> tuple[list[str], list[tuple[Any, ...]]] | None:
        name = self._fp_by_sql.get(_flat(sql))
        if name is None:
            return None
        cols, rows = fingerprints(self.tables[table])[name]
        return ([c.upper() for c in cols] if self.upper else cols), rows


class FakeClickHouse(_Engine):
    def __init__(
        self,
        pack: Pack,
        *,
        source: Path = FIXTURE,
        transform: Transform = apply_nullable,
        fail_on: str | None = None,
    ) -> None:
        super().__init__(pack, "clickhouse")
        self.source = source
        self.transform = transform
        self.fail_on = fail_on
        self.queries: list[str] = []

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> Any:
        flat = _flat(sql)
        self.statements.append(sql)
        if self.fail_on and self.fail_on in flat:
            raise DatabaseError(f"Code: 999. planted failure on {self.fail_on}")
        if m := re.match(r"CREATE DATABASE (IF NOT EXISTS )?(\w+)$", flat):
            if m[2] in self.databases and not m[1]:
                raise DatabaseError(f"Code: 82. Database {m[2]} already exists")
            self.databases.add(m[2])
        elif m := re.match(r"CREATE (OR REPLACE )?TABLE (IF NOT EXISTS )?(\w+)\.(\w+) ?\(", flat):
            name = f"{m[3]}.{m[4]}"
            if m[3] not in self.databases:
                raise DatabaseError(f"Code: 81. Database {m[3]} does not exist")
            if name in self.tables and not m[1] and not m[2]:
                raise DatabaseError(f"Code: 57. Table {name} already exists")
            if m[1] or name not in self.tables:
                self.tables[name] = []
                self.schemas[name] = table_columns(sql)
        elif m := re.match(r"DROP TABLE (\S+)$", flat):
            self.count(m[1])
            del self.tables[m[1]], self.schemas[m[1]]
        elif m := re.match(r"TRUNCATE TABLE (\S+)$", flat):
            self.count(m[1])
            self.tables[m[1]] = []
        elif m := re.match(r"INSERT INTO (\S+) SELECT .* FROM url\(", flat):
            self.count(m[1])
            self.tables[m[1]] += [self.transform(r) for r in read_fixture(self.source)]
        elif m := re.match(r"INSERT INTO (\S+) \(.*\) VALUES", flat):
            self.count(m[1])
            self.tables[m[1]] += values_rows(sql, "clickhouse")
        else:
            raise AssertionError(f"unexpected command: {flat[:100]}")
        return None

    def query(self, sql: str, settings: dict[str, Any] | None = None) -> Any:
        flat = _flat(sql)
        self.queries.append(sql)
        if m := re.match(r"SELECT count\(\) FROM (\S+)$", flat):
            try:
                return ch_result(["count()"], [(self.count(m[1]),)])
            except LookupError as e:
                raise DatabaseError(f"Code: 60. {e}") from e
        if m := re.match(r"DESCRIBE TABLE (\S+)$", flat):
            cols = ["name", "type", "default_type", "default_expression", "comment", "codec", "ttl"]
            return ch_result(cols, self.describe(m[1]))
        table = f"{self.pack.manifest.databases['clickhouse']}.uk_price_paid"
        answer = self.fingerprint(sql, table)
        if answer is None:
            raise AssertionError(f"unexpected query: {flat[:100]}")
        return ch_result(*answer)


class FakeSnowflake(_Engine):
    upper = True

    def __init__(self, pack: Pack, *, transform: Transform = apply_nullable) -> None:
        super().__init__(pack, "snowflake")
        self.transform = transform
        self.formats: dict[str, str] = {}
        self.stages: dict[str, dict[str, bytes]] = {}
        self.objects: set[str] = set()
        self.cursors_open = 0

    def cursor(self) -> FakeSFCursor:
        self.cursors_open += 1
        return FakeSFCursor(self)

    def _typed_table(self) -> str:
        return f"{self.pack.manifest.databases['snowflake']}.uk_price_paid"

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]]]:
        flat = _flat(sql)
        self.statements.append(sql)
        if m := re.match(r"CREATE (DATABASE|SCHEMA) IF NOT EXISTS (\S+)", flat):
            self.objects.add(m[2])
        elif m := re.match(r"CREATE OR REPLACE FILE FORMAT (\S+) ", flat):
            self.formats[m[1]] = flat
        elif m := re.match(r"CREATE OR REPLACE STAGE (\S+)", flat):
            self.stages[m[1]] = {}
        elif m := re.match(r"CREATE (OR REPLACE )?TABLE (IF NOT EXISTS )?(\S+) ?\(", flat):
            name = m[3]
            if m[1] or name not in self.tables:
                self.tables[name] = []
                self.schemas[name] = [(n.upper(), _sf_display(t)) for n, t in table_columns(sql)]
        elif m := re.match(r"DROP TABLE (\S+)$", flat):
            self.count(m[1])
            del self.tables[m[1]], self.schemas[m[1]]
        elif m := re.match(r"DESCRIBE TABLE (\S+)$", flat):
            return ["name", "type", "kind", "null?", "default"], [
                r[:5] for r in self.describe(m[1])
            ]
        elif m := re.match(r"TRUNCATE TABLE (\S+)$", flat):
            self.count(m[1])
            self.tables[m[1]] = []
        elif m := re.match(
            r"PUT 'file://([^']+)' @(\S+) AUTO_COMPRESS = FALSE OVERWRITE = TRUE$", flat
        ):
            path = Path(m[1])
            self.stages[m[2]][path.name] = path.read_bytes()
        elif m := re.match(r"COPY INTO (\S+) FROM @(\S+?)/(\S+) .*FORCE = TRUE$", flat):
            data = gzip.decompress(self.stages[m[2]][m[3]]).decode("utf-8")
            rows: list[list[Any]] = list(csv.reader(io.StringIO(data, newline="")))
            if any(len(r) != 16 for r in rows):
                raise RuntimeError("COPY aborted: wrong field count")
            fmt = next(iter(self.formats.values()), "")
            if "NULL_IF = ('')" in fmt or "EMPTY_FIELD_AS_NULL = TRUE" in fmt:
                rows = [[None if v == "" else v for v in r] for r in rows]
            self.tables[m[1]] = rows
        elif m := re.match(r"INSERT INTO (\S+) SELECT .* FROM (\S+)$", flat):
            self.tables[m[1]] += [self.transform(r) for r in self.tables[m[2]]]
        elif m := re.match(r"INSERT INTO (\S+) \(.*\) VALUES", flat):
            self.count(m[1])
            self.tables[m[1]] += values_rows(sql, "snowflake")
        elif m := re.match(r"SELECT COUNT\(\*\) FROM (\S+)$", flat):
            return ["COUNT(*)"], [(self.count(m[1]),)]
        else:
            answer = self.fingerprint(sql, self._typed_table())
            if answer is None:
                raise AssertionError(f"unexpected statement: {flat[:100]}")
            return answer
        return None, []


class FakeSFCursor:
    def __init__(self, conn: FakeSnowflake) -> None:
        self.conn = conn
        self.description: list[Any] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> FakeSFCursor:
        cols, rows = self.conn.execute(sql)
        self.description = [sf_column(c) for c in cols] if cols is not None else None
        self._rows = rows
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.conn.cursors_open -= 1


def fixture_download(src: Path = FIXTURE) -> Callable[[str, Path], None]:
    """A downloader that copies the fixture instead of fetching the URL; records the URLs."""
    calls: list[str] = []

    def download(url: str, dest: Path) -> None:
        calls.append(url)
        shutil.copyfile(src, dest)

    download.calls = calls  # type: ignore[attr-defined]
    return download


def fixed_clock(start: dt.datetime | None = None) -> Callable[[], dt.datetime]:
    t = start or dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)
    return lambda: t
