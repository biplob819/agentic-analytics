"""Loader SQL plumbing: rendering, splitting and the statement allow-list (LOD-01, LOD-02, LOD-04).

Loaders write, so the read-only guard does not apply; these are the rules that keep them to
fixed, templated SQL instead."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentic_analytics.load._common import (
    LoaderError,
    classify,
    plan,
    qualified,
    render,
    safe_path,
    schema_mismatch,
    split_statements,
    table_columns,
)

LOAD_DIR = Path(__file__).resolve().parents[2] / "src" / "agentic_analytics" / "load"


def test_render_fills_placeholders_and_refuses_missing_ones() -> None:
    assert render("CREATE DATABASE {database};", {"database": "uk", "x": "y"}) == (
        "CREATE DATABASE uk;"
    )
    with pytest.raises(ValueError, match=r"\['stage_file'\]"):
        render("PUT x @{database}/{stage_file}", {"database": "d"})


def test_split_respects_quotes_comments_and_doubled_quotes() -> None:
    sql = (
        "-- a comment; with a semicolon and an apostrophe's\n"
        "INSERT INTO t VALUES ('a;b', 'it''s; fine', \"q;\");\n"
        "SELECT 1 -- trailing; comment\n;\n;\n"
    )
    assert split_statements(sql) == [
        "INSERT INTO t VALUES ('a;b', 'it''s; fine', \"q;\")",
        "SELECT 1",
    ]


def test_split_refuses_an_unterminated_literal() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        split_statements("SELECT 'oops;")


@pytest.mark.parametrize(
    ("name", "parts", "ok"),
    [
        ("ANALYTICS.UK", 2, True),
        ("uk", 1, True),
        ("uk", 2, False),
        ("A.B.C", 2, False),
        ("A.B; DROP", 2, False),
        ("A.`B`", 2, False),
        ("", 1, False),
    ],
)
def test_qualified_names(name: str, parts: int, ok: bool) -> None:
    if ok:
        assert ".".join(qualified(name, parts, "db")) == name
    else:
        with pytest.raises(ValueError):
            qualified(name, parts, "db")


@pytest.mark.parametrize("path", ["/tmp/a'b.gz", "/tmp/x;y", "", "/tmp/$(rm)"])
def test_safe_path_refuses_characters_that_would_break_a_literal(path: str) -> None:
    with pytest.raises(ValueError):
        safe_path(path, "local file")


def test_safe_path_allows_ordinary_paths() -> None:
    assert safe_path("/private/tmp/data dir/pp-complete.csv.gz", "f")
    assert safe_path("C:\\Users\\x\\pp.csv.gz", "f")


@pytest.mark.parametrize(
    ("sql", "kind", "targets"),
    [
        ("CREATE DATABASE IF NOT EXISTS uk", "create_database", ("uk",)),
        ("CREATE TABLE uk.t\n(\n a UInt8\n)", "create_table", ("uk.t",)),
        ("TRUNCATE TABLE A.B.t", "truncate", ("A.B.t",)),
        ("PUT 'file:///x/y.gz' @A.B.s AUTO_COMPRESS = FALSE", "put", ("A.B.s",)),
        ("COPY INTO A.B.r FROM @A.B.s/y.gz FILE_FORMAT = (X)", "copy", ("A.B.r", "A.B.s")),
        ("INSERT INTO uk.t SELECT 1", "insert", ("uk.t",)),
        ("INSERT INTO p.n (a, b) VALUES (1, 'x')", "insert", ("p.n",)),
    ],
)
def test_classify_known_shapes(sql: str, kind: str, targets: tuple[str, ...]) -> None:
    step = classify(sql)
    assert (step.kind, step.targets) == (kind, targets)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE uk.uk_price_paid",
        "DELETE FROM uk.uk_price_paid WHERE 1",
        "ALTER TABLE uk.t DELETE WHERE 1",
        "GRANT ALL ON *.* TO x",
        "SELECT * FROM url('http://x', CSV)",
        "INSERT INTO FUNCTION url('http://x') SELECT 1",
        "CREATE USER x",
        "SYSTEM SHUTDOWN",
    ],
)
def test_classify_refuses_every_other_statement(sql: str) -> None:
    with pytest.raises(LoaderError, match="not allowed"):
        classify(sql)


def test_plan_refuses_objects_outside_the_allowed_set() -> None:
    allowed = {"create_database": ["uk"], "insert": ["uk.t"]}
    sql = "CREATE DATABASE uk; INSERT INTO uk.t VALUES (1);"
    assert [s.kind for s in plan(sql, allowed, engine="clickhouse")] == [
        "create_database",
        "insert",
    ]
    with pytest.raises(LoaderError, match=r"insert on system\.users"):
        plan("INSERT INTO system.users VALUES (1);", allowed, engine="clickhouse")
    with pytest.raises(LoaderError, match="truncate"):
        plan("TRUNCATE TABLE uk.t;", allowed, engine="clickhouse")


def test_loaders_never_import_the_read_only_executors() -> None:
    """Loaders use admin connections passed in; the executors are for read-only work (D-19)."""
    banned = {"agentic_analytics.warehouse.clickhouse", "agentic_analytics.warehouse.snowflake"}
    for path in LOAD_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in banned:
                names = {a.name for a in node.names}
                # the admin Snowflake connection reuses only the key loader
                assert names == {"load_private_key"}, (path.name, names)
            if isinstance(node, ast.ImportFrom) and node.module == "agentic_analytics.warehouse":
                raise AssertionError(f"{path.name} imports the warehouse package")


# ---- statement bodies (pre-verification fix 3) ----------------------------------------------

CH_ALLOWED = {"create_table": ["uk.t"], "insert": ["uk.t"], "create_database": ["uk"]}
SF_ALLOWED = {
    "create_stage": ["A.B.s"],
    "create_table": ["A.B.t", "A.B.raw"],
    "insert": ["A.B.t"],
}
DOC_INSERT = (
    "INSERT INTO uk.t SELECT toUInt32(a) AS p FROM url('http://h.example/pp.csv', 'CSV', "
    "'a String') SETTINGS max_http_get_redirects=10;"
)


def test_the_documented_url_insert_shape_is_allowed() -> None:
    steps = plan(DOC_INSERT, CH_ALLOWED, engine="clickhouse", select_source="url")
    assert [s.kind for s in steps] == ["insert"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO uk.t SELECT * FROM remote('evil.invalid', default.t);",
        "INSERT INTO uk.t SELECT * FROM remoteSecure('evil.invalid', default.t);",
        "INSERT INTO uk.t SELECT * FROM s3('https://b.s3/x.csv', 'CSV');",
        "INSERT INTO uk.t SELECT * FROM url('file:///etc/passwd', 'CSV', 'a String');",
        "INSERT INTO uk.t SELECT a FROM url('http://h.example/x', 'CSV', 'a String') "
        "UNION ALL SELECT a FROM url('http://e.invalid/y', 'CSV', 'a String');",
        "INSERT INTO uk.t SELECT (SELECT 1 FROM url('http://e.invalid', 'CSV', 'a String')) "
        "FROM url('http://h.example/x', 'CSV', 'a String');",
        "INSERT INTO uk.t SELECT a FROM url('http://h.example/x', 'CSV', 'a String') "
        "SETTINGS readonly = 0;",
        "INSERT INTO uk.t SELECT dictGet('d', 'x', toUInt64(1)) FROM "
        "url('http://h.example/x', 'CSV', 'a String');",
        "INSERT INTO uk.t SELECT * FROM uk.other;",
    ],
)
def test_clickhouse_insert_bodies_reaching_outside_are_refused(sql: str) -> None:
    with pytest.raises(LoaderError, match="not allowed"):
        plan(sql, CH_ALLOWED, engine="clickhouse", select_source="url")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE uk.t (a String) ENGINE = URL('http://e.invalid/x', 'CSV');",
        "CREATE TABLE uk.t (a String) ENGINE = S3('https://b.s3/x', 'CSV');",
        "CREATE TABLE uk.t (a String) ENGINE = MySQL('h:3306', 'db', 't', 'u', 'p');",
        "CREATE TABLE uk.t (a String) ENGINE = PostgreSQL('h:5432', 'db', 't', 'u', 'p');",
        "CREATE TABLE uk.t (a String) ENGINE = File(CSV);",
        "CREATE TABLE uk.t (a String) ENGINE = Remote('h', db, t);",
        "CREATE TABLE uk.t (a String) ENGINE = Memory;",
        "CREATE TABLE uk.t (a String) ENGINE = MergeTree ORDER BY a AS SELECT * FROM s3('x');",
        "CREATE TABLE uk.t (a String DEFAULT dictGet('d', 'x', 1)) ENGINE = MergeTree ORDER BY a;",
        "CREATE TABLE uk.t (a String) ENGINE = MergeTree ORDER BY a;"
        " INSERT INTO uk.t VALUES (url('http://e.invalid'));",
    ],
)
def test_clickhouse_external_engines_and_functions_are_refused(sql: str) -> None:
    with pytest.raises(LoaderError, match="not allowed"):
        plan(sql, CH_ALLOWED, engine="clickhouse")


def test_url_is_refused_where_only_values_are_allowed() -> None:
    with pytest.raises(LoaderError, match=r"url\(\)"):
        plan(DOC_INSERT, CH_ALLOWED, engine="clickhouse")


def test_function_names_inside_literals_are_data_not_code() -> None:
    sql = "INSERT INTO uk.t (a) VALUES ('see url(x), remote(y) and @stage');"
    assert plan(sql, CH_ALLOWED, engine="clickhouse")[0].kind == "insert"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE OR REPLACE STAGE A.B.s URL = 's3://bucket/path';",
        "CREATE OR REPLACE STAGE A.B.s STORAGE_INTEGRATION = s3_int URL = 's3://b';",
        "CREATE OR REPLACE STAGE A.B.s FILE_FORMAT = A.B.f CREDENTIALS = (AWS_KEY_ID = 'x');",
        "INSERT INTO A.B.t SELECT $1 FROM @A.B.s;",
        "INSERT INTO A.B.t SELECT * FROM A.B.other;",
        "INSERT INTO A.B.t SELECT * FROM TABLE(INFER_SCHEMA(LOCATION => '@A.B.s'));",
        "INSERT INTO A.B.t SELECT ext.fn(a) FROM A.B.raw;",
        "INSERT INTO A.B.t SELECT AI_COMPLETE('m', a) FROM A.B.raw;",
        "CREATE TABLE A.B.t (a VARCHAR) AS SELECT * FROM A.B.raw;",
        "CREATE OR REPLACE TABLE A.B.t (a VARCHAR) CLUSTER BY (a);",
    ],
)
def test_snowflake_bodies_reaching_outside_are_refused(sql: str) -> None:
    with pytest.raises(LoaderError, match="not allowed"):
        plan(sql, SF_ALLOWED, engine="snowflake", select_source="table", sources=["A.B.raw"])


# Verifier: syntax the engines read differently from the literal masker. Both engines treat a
# backslash inside a literal as an escape, so `'p\\', '` is ONE literal to them and two to the
# masker, which then hides the call that follows; a block comment, a backtick or a double-quoted
# name between a function's name and its `(` hid it from the call pattern.
@pytest.mark.parametrize(
    ("sql", "engine", "kw"),
    [
        (
            "INSERT INTO uk.t (a) VALUES (1, 'p\\', ', dictGetString(toString(1), toString(2), "
            "toUInt64(1)), '\\'');",
            "clickhouse",
            {},
        ),
        (
            "INSERT INTO uk.t SELECT dictGet/**/('d', 'x', toUInt64(1)) AS p FROM "
            "url('http://h.example/x', 'CSV', 'a String');",
            "clickhouse",
            {"select_source": "url"},
        ),
        (
            "INSERT INTO uk.t SELECT `dictGet`('d', 'x', toUInt64(1)) AS p FROM "
            "url('http://h.example/x', 'CSV', 'a String');",
            "clickhouse",
            {"select_source": "url"},
        ),
        (
            "INSERT INTO uk.t SELECT \"dictGet\"('d', 'x', toUInt64(1)) AS p FROM "
            "url('http://h.example/x', 'CSV', 'a String');",
            "clickhouse",
            {"select_source": "url"},
        ),
        (
            "INSERT INTO uk.t SELECT 1 AS p FROM url('http://h.example/x', 'CSV', 'a String') "
            "# SETTINGS readonly = 0\n;",
            "clickhouse",
            {"select_source": "url"},
        ),
        (
            "INSERT INTO A.B.t SELECT AI_COMPLETE/* x */('m', a) FROM A.B.raw;",
            "snowflake",
            {"select_source": "table", "sources": ["A.B.raw"]},
        ),
        (
            "INSERT INTO A.B.t SELECT \"AI_COMPLETE\"('m', a) FROM A.B.raw;",
            "snowflake",
            {"select_source": "table", "sources": ["A.B.raw"]},
        ),
    ],
)
def test_calls_hidden_by_escapes_comments_or_quoted_names_are_refused(
    sql: str, engine: str, kw: dict
) -> None:
    allowed = CH_ALLOWED if engine == "clickhouse" else SF_ALLOWED
    with pytest.raises(LoaderError, match=r"backslash|comment, backtick or quoted name"):
        plan(sql, allowed, engine=engine, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "tail",
    [
        "SETTINGS disk = disk(type = web, endpoint = 'https://e.invalid/')",
        "SETTINGS storage_policy = 's3_main'",
        "TTL toDate('2020-01-01') TO DISK 's3'",
    ],
)
def test_clickhouse_tables_take_no_settings_or_ttl(tail: str) -> None:
    """A web or S3 disk in a table's SETTINGS (or a TTL move to one) reaches outside."""
    sql = f"CREATE TABLE uk.t (a String) ENGINE = MergeTree ORDER BY a {tail};"
    with pytest.raises(LoaderError, match="other than a MergeTree engine"):
        plan(sql, CH_ALLOWED, engine="clickhouse")


def test_snowflake_internal_stage_and_staging_insert_are_allowed() -> None:
    sql = (
        "CREATE OR REPLACE STAGE A.B.s FILE_FORMAT = A.B.f;"
        "CREATE OR REPLACE TABLE A.B.raw (a VARCHAR);"
        "INSERT INTO A.B.t SELECT TO_NUMBER(a, 10, 0) AS p FROM A.B.raw;"
    )
    steps = plan(sql, SF_ALLOWED, engine="snowflake", select_source="table", sources=["A.B.raw"])
    assert [s.kind for s in steps] == ["create_stage", "create_table", "insert"]


# ---- schema comparison (pre-verification fix 4) ---------------------------------------------


def test_table_columns_reads_types_with_commas_and_quotes() -> None:
    cols = table_columns(
        "CREATE TABLE uk.t (a Enum8('x, y' = 1, 'z' = 2), b LowCardinality(String)) "
        "ENGINE = MergeTree ORDER BY a"
    )
    assert cols == [("a", "Enum8('x, y' = 1, 'z' = 2)"), ("b", "LowCardinality(String)")]


def test_schema_mismatch_normalises_engine_display_forms() -> None:
    ch_want = [("type", "Enum8('a' = 1, 'b' = 0)"), ("n", "UInt32")]
    assert schema_mismatch("clickhouse", ch_want, [("type", "Enum8('b' = 0, 'a' = 1)"),
                                                   ("n", "UInt32")]) is None  # fmt: skip
    assert "UInt64" in (schema_mismatch("clickhouse", ch_want, [ch_want[0], ("n", "UInt64")]) or "")
    sf_want = [("price", "NUMBER(10, 0)"), ("town", "VARCHAR")]
    sf_have = [("PRICE", "NUMBER(10,0)"), ("TOWN", "VARCHAR(16777216)")]
    assert schema_mismatch("snowflake", sf_want, sf_have) is None
    assert "columns" in (schema_mismatch("snowflake", sf_want, sf_have[:1]) or "")
    assert "BOOLEAN" in (schema_mismatch("snowflake", sf_want, [sf_have[0], ("TOWN", "BOOLEAN")])
                         or "")  # fmt: skip
