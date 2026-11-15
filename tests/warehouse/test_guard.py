"""EXE-01: the SQL guard allows one read query and refuses everything else."""

from __future__ import annotations

import pytest
import sqlglot
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlglot import expressions as sqlglot_exp

from agentic_analytics.warehouse import guard
from agentic_analytics.warehouse.guard import GuardVerdict, check_sql

pytestmark = pytest.mark.safety

CH_HOSTILE: list[str] = [
    # stacked statements, hidden behind comments and strings
    "SELECT 1; DROP TABLE uk.uk_price_paid",
    "SELECT 1 /* ; */ ; DROP TABLE x",
    "SELECT '; DROP TABLE x'; DROP TABLE x",
    "SELECT 1;\n-- harmless\nINSERT INTO uk.t VALUES (1)",
    "SELECT 1;; DROP TABLE x",
    # DDL / DML / control statements
    "DROP TABLE uk.uk_price_paid",
    "TRUNCATE TABLE uk.uk_price_paid",
    "INSERT INTO uk.uk_price_paid SELECT * FROM uk.uk_price_paid",
    "INSERT INTO uk.t SELECT * FROM url('http://evil/x.csv', CSV)",
    "ALTER TABLE uk.uk_price_paid DELETE WHERE 1",
    "ALTER TABLE uk.uk_price_paid UPDATE price = 0 WHERE 1",
    "CREATE TABLE uk.x (a UInt8) ENGINE = Memory",
    "CREATE TABLE uk.x ENGINE = Memory AS SELECT * FROM uk.uk_price_paid",
    "DELETE FROM uk.uk_price_paid WHERE 1",
    "RENAME TABLE uk.a TO uk.b",
    "OPTIMIZE TABLE uk.uk_price_paid FINAL",
    "SET readonly = 0",
    "USE uk",
    "SYSTEM SHUTDOWN",
    "SYSTEM DROP DNS CACHE",
    "KILL QUERY WHERE 1",
    "GRANT ALL ON *.* TO analytics_ro",
    "REVOKE SELECT ON uk.* FROM analytics_ro",
    "EXPLAIN SELECT 1",
    "SHOW TABLES",
    "DESCRIBE TABLE uk.uk_price_paid",
    "DETACH TABLE uk.uk_price_paid",
    # outfile, settings and format
    "SELECT * FROM uk.uk_price_paid INTO OUTFILE '/tmp/x.csv'",
    "SELECT 1 SETTINGS readonly = 0",
    "SELECT * FROM uk.uk_price_paid SETTINGS max_execution_time = 100000",
    "SELECT * FROM uk.uk_price_paid FORMAT CSV",
    # SETTINGS / FORMAT on set operations, subqueries and CTEs (verifier round 1)
    "SELECT 1 UNION ALL SELECT 2 SETTINGS readonly=0",
    "(SELECT 1) SETTINGS readonly=0",
    "WITH a AS (SELECT 1 UNION ALL SELECT 2 SETTINGS readonly=0) SELECT * FROM a",
    "SELECT 1 EXCEPT SELECT 2 SETTINGS max_execution_time=1000",
    "SELECT 1 UNION ALL SELECT 2 FORMAT TSV",
    # the system database
    "SELECT * FROM system.query_log",
    "SELECT user, query FROM system.processes",
    "SELECT * FROM `system`.users",
    # table functions refused only by the row-generator allow-list
    "SELECT * FROM s3Queue('https://bucket/x.csv', CSV)",
    "SELECT * FROM ytsaurus('http://proxy', '//tmp/t', 'tok', 'a Int32')",
    "SELECT * FROM someFutureTableFunction(1)",
    # dict* functions
    "SELECT dictIsIn('regions', toUInt64(1), toUInt64(2))",
    "SELECT dictGetHierarchy('regions', toUInt64(1))",
    # round 2: shapes the parser mis-reads, IN over a table, introspection, nested comments
    "SELECT * FROM uk.t ARRAY JOIN (SELECT groupArray(name) FROM system.users) AS n",
    "SELECT * FROM uk.t LEFT ARRAY JOIN (SELECT groupArray(x) FROM s3Queue('x')) AS n",
    "SELECT * FROM uk.t ARRAY JOIN (SELECT groupArray(x) FROM ytsaurus('a', 'b', 'c')) AS n",
    "SELECT * FROM uk.t ARRAY JOIN (SELECT groupArray(x) FROM arrowFlight('h:1', 'd')) AS n",
    "SELECT * FROM uk.t ARRAY JOIN (SELECT groupArray(x) FROM format(CSV, '1')) AS n",
    "SELECT * FROM uk.t ARRAY JOIN (SELECT groupArray(x) FROM 'x') AS n",
    "SELECT * FROM uk.t WHERE town IN system.users",
    "SELECT * FROM uk.t WHERE town GLOBAL IN system.users",
    "SELECT * FROM uk.t WHERE town IN (system.users)",
    "SELECT hasColumnInTable('system', 'users', 'name')",
    "SELECT getSetting('readonly')",
    "SELECT currentRoles()",
    "SELECT 1 /* /* */ FROM url('http://x', CSV) -- */",
    "SELECT 1 FORMAT JSON",
    # round 3: a table function on the right of IN
    "SELECT * FROM uk.t WHERE town IN ytsaurus('x')",
    "SELECT * FROM uk.t WHERE town IN (ytsaurus('x'))",
    "SELECT * FROM uk.t WHERE town GLOBAL IN s3Queue('x')",
    # round 4: table-function names refused as calls anywhere
    "SELECT * FROM uk.t WHERE notIn(town, s3Queue('x'))",
    "SELECT * FROM uk.t WHERE globalIn(town, ytsaurus('h', '//t', 'tok'))",
    "SELECT * FROM uk.t WHERE globalNotIn(town, ytsaurus('h', '//t', 'tok'))",
    "SELECT * FROM uk.t WHERE nullIn(town, s3Queue('x'))",
    "SELECT * FROM uk.t WHERE notNullIn(town, arrowFlight('h:1', 'd'))",
    "SELECT * FROM uk.t WHERE globalNullIn(town, s3Queue('x'))",
    "SELECT * FROM uk.t WHERE globalNotNullIn(town, primes(10))",
    "SELECT * FROM uk.t WHERE town IN ((s3Queue('x')))",
    "SELECT * FROM uk.t WHERE town IN (((ytsaurus('x'))))",
    "SELECT * FROM uk.t WHERE town IN numbers(3)",
    "SELECT numbers(3)",
    "SELECT * FROM uk.t, dictionary('d')",
    # table functions that reach outside, in every position
    "SELECT * FROM url('http://x', CSV)",
    "SELECT * FROM URL('http://x', CSV)",
    "SELECT * FROM `url`('http://x', CSV)",
    "SELECT * FROM s3('https://bucket/x.csv')",
    "SELECT * FROM s3Cluster('default', 'https://bucket/x.csv')",
    "SELECT * FROM file('/etc/passwd', 'LineAsString')",
    "SELECT file('/etc/passwd')",
    "SELECT * FROM remote('10.0.0.1', db.t)",
    "SELECT * FROM remoteSecure('10.0.0.1:9440', db.t, 'u', 'p')",
    "SELECT * FROM cluster('default', db.t)",
    "SELECT * FROM clusterAllReplicas('default', system.tables)",
    "SELECT * FROM input('a String')",
    "SELECT * FROM jdbc('jdbc:mysql://h/db', 't')",
    "SELECT * FROM odbc('DSN=x', 't')",
    "SELECT * FROM mysql('h:3306', 'db', 't', 'u', 'p')",
    "SELECT * FROM postgresql('h:5432', 'db', 't', 'u', 'p')",
    "SELECT * FROM hdfs('hdfs://h/x', 'CSV')",
    "SELECT * FROM azureBlobStorage('conn', 'c', 'x.csv')",
    "SELECT * FROM gcs('https://storage.googleapis.com/b/x.csv')",
    "SELECT * FROM icebergS3('https://bucket/table')",
    "SELECT * FROM iceberg('https://bucket/table')",
    "SELECT * FROM deltaLake('https://bucket/table')",
    "SELECT * FROM hudi('https://bucket/table')",
    "SELECT * FROM executable('script.sh', TSV, 'a String')",
    "SELECT * FROM dictionary('d')",
    "SELECT dictGet('d', 'a', toUInt64(1))",
    "SELECT * FROM generateRandom('a Int32')",
    "SELECT * FROM merge('uk', '.*')",
    "SELECT sleepEachRow(3) FROM numbers(100)",
    "SELECT * FROM (SELECT * FROM url('http://x', CSV))",
    "WITH a AS (SELECT * FROM file('x')) SELECT * FROM a",
    "SELECT (SELECT count() FROM url('http://x', CSV))",
    "SELECT * FROM uk.t WHERE town IN (SELECT * FROM s3('https://b/x'))",
    "SELECT * FROM uk.t AS u JOIN remote('h', db, t) AS r ON u.id = r.id",
    "SELECT 1 UNION ALL SELECT * FROM url('http://x', CSV)",
    "SELECT uk.myfn(price) FROM uk.uk_price_paid",
    # unicode and character tricks
    "SELECT * FROM ｕｒｌ('http://x', CSV)",  # fullwidth url
    "SELECT \uff46\uff49\uff4c\uff45('/etc/passwd')",  # fullwidth scalar file(): NFKC only
    "SELECT * FROM u​rl('http://x', CSV)",  # zero-width space
    "SELECT 1 ‮; DROP TABLE x",  # bidi override
    "SELECT 1\x00; DROP TABLE x",
    # empty and unparseable
    "",
    "   \n\t ",
    "-- just a comment",
    "/* nothing */",
    "SELEC 1",
    "SELECT * FROM (",
]

SF_HOSTILE: list[str] = [
    "SELECT 1; DROP TABLE ANALYTICS.UK.UK_PRICE_PAID",
    "SELECT 1 /* ; */ ; DELETE FROM t",
    "DROP TABLE ANALYTICS.UK.UK_PRICE_PAID",
    "TRUNCATE TABLE ANALYTICS.UK.UK_PRICE_PAID",
    "INSERT INTO t SELECT * FROM t",
    "UPDATE t SET price = 0",
    "DELETE FROM t",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "CREATE TABLE x AS SELECT 1",
    "CREATE OR REPLACE TABLE x CLONE ANALYTICS.UK.UK_PRICE_PAID",
    "ALTER SESSION SET QUERY_TAG = 'x'",
    "ALTER WAREHOUSE w SET WAREHOUSE_SIZE = 'X6LARGE'",
    "USE ROLE ACCOUNTADMIN",
    "GRANT ROLE ACCOUNTADMIN TO USER me",
    "CALL my_proc()",
    "EXECUTE IMMEDIATE 'DROP TABLE x'",
    "BEGIN DROP TABLE x; END",
    "COPY INTO t FROM @stage",
    "COPY INTO @stage FROM t",
    "PUT file:///tmp/x @stage",
    "GET @stage file:///tmp",
    "LIST @stage",
    "REMOVE @stage",
    "SHOW TABLES",
    "DESCRIBE TABLE t",
    "EXPLAIN SELECT 1",
    "SELECT * FROM @mystage/x.csv",
    "SELECT $1 FROM @mystage",
    "SELECT * FROM @~",
    "SELECT * FROM @%t",
    "SELECT GET_PRESIGNED_URL(@stage, 'f.csv')",
    "SELECT BUILD_SCOPED_FILE_URL(@stage, 'f.csv')",
    "SELECT SYSTEM$WHITELIST()",
    "SELECT system$cancel_all_queries(1)",
    "SELECT * FROM TABLE(INFER_SCHEMA(LOCATION => '@stage'))",
    "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    "SELECT * FROM TABLE(my_db.my_schema.external_fn(1))",
    "SELECT ANALYTICS.UK.ext_call(price) FROM t",
    "SELECT 1 INTO :v",
    # quoted stage paths and positional stage columns (verifier round 1)
    "SELECT $1 FROM '@~/f.csv'",
    "SELECT $1 FROM '@%orders'",
    "SELECT $1 FROM '@db.sch.s/p/'",
    "SELECT * FROM '@~/f.csv'",
    "SELECT * FROM 'plain_string_table'",
    # Cortex AI functions spend credits
    "SELECT AI_COMPLETE('mistral-large2', 'hi')",
    "SELECT AI_CLASSIFY(TOWN, ['north', 'south']) FROM T",
    "SELECT COMPLETE('mistral-large2', 'hi')",
    "SELECT SNOWFLAKE.CORTEX.SUMMARIZE(TOWN) FROM T",
    "SELECT TO_FILE('@s/f.pdf')",
    "SELECT TO_FILE(F) FROM T",
    # refused only by the row-generator allow-list
    "SELECT * FROM TABLE(SOME_FUTURE_UDTF(1))",
    "SELECT * FROM t; SELECT * FROM t",
    "",
    "SELEC 1",
]

CH_BENIGN: list[str] = [
    "SELECT 1",
    "SELECT 1;",
    "(SELECT 1)",
    "SELECT medianExact(price) FROM uk.uk_price_paid",
    "SELECT toYear(date) AS y, count() FROM uk.uk_price_paid GROUP BY y ORDER BY y",
    "SELECT town, avg(price) FROM uk.uk_price_paid WHERE type = 'detached' GROUP BY town",
    "SELECT quantileExact(0.5)(price) FROM uk.uk_price_paid",
    "SELECT countIf(price > 1000000), argMax(town, price) FROM uk.uk_price_paid",
    "WITH t AS (SELECT town, price FROM uk.uk_price_paid) SELECT town, max(price) FROM t "
    "GROUP BY town",
    "WITH 2024 AS yr SELECT count() FROM uk.uk_price_paid WHERE toYear(date) = yr",
    "SELECT town, price, row_number() OVER (PARTITION BY town ORDER BY price DESC) AS rn "
    "FROM uk.uk_price_paid",
    "SELECT district, sum(price) OVER (ORDER BY district) FROM uk.uk_price_paid",
    "SELECT * FROM (SELECT town, count() AS n FROM uk.uk_price_paid GROUP BY town) WHERE n > 100",
    "SELECT town FROM uk.uk_price_paid WHERE town IN (SELECT town FROM uk.uk_price_paid "
    "WHERE price > 5000000)",
    "SELECT 'a' AS k UNION ALL SELECT 'b'",
    "SELECT town FROM uk.uk_price_paid UNION DISTINCT SELECT district FROM uk.uk_price_paid",
    "SELECT * FROM uk.uk_price_paid FINAL LIMIT 10",
    "SELECT * FROM uk.uk_price_paid ORDER BY price DESC LIMIT 5 BY town",
    "SELECT * FROM uk.uk_price_paid SAMPLE 0.1",
    "SELECT number FROM numbers(10)",
    "SELECT u.town, p.note FROM uk.uk_price_paid AS u JOIN uk_probes.listing_notes AS p "
    "ON u.town = p.town",
    "SELECT formatDateTime(date, '%Y-%m') AS ym, count() FROM uk.uk_price_paid GROUP BY ym",
    "SELECT toStartOfMonth(date) AS m, avg(price) FROM uk.uk_price_paid GROUP BY m",
    "SELECT round(avg(price), 2) FROM uk.uk_price_paid WHERE date >= '2024-01-01'",
    "SELECT domain('http://x.com/a')",  # URL-parsing function, not the url() table function
    "SELECT 'url(http://x)' AS s",
    "SELECT 1 -- ; DROP TABLE x",
    "SELECT /* DROP TABLE x */ 1",
    "SELECT '; DROP TABLE x' AS s",
    "SELECT `town` FROM `uk`.`uk_price_paid`",
    "SELECT x FROM uk.uk_price_paid ARRAY JOIN [1, 2] AS x",
    "SELECT multiIf(price < 100000, 'low', price < 500000, 'mid', 'high') AS band, count() "
    "FROM uk.uk_price_paid GROUP BY band",
    "SELECT town, uniqExact(postcode1) FROM uk.uk_price_paid GROUP BY town HAVING count() > 10",
    "SELECT dateDiff('day', min(date), max(date)) FROM uk.uk_price_paid",
    "SELECT 'Ünïcödé' AS name",
    "SELECT county, topK(3)(town) FROM uk.uk_price_paid GROUP BY county",
    "SELECT * FROM uk.uk_price_paid WHERE town ILIKE '%manchester%' LIMIT 3",
    "SELECT toYear(date) AS y, medianExact(price) AS m, "
    "lagInFrame(m) OVER (ORDER BY y) AS prev FROM uk.uk_price_paid GROUP BY y",
    "SELECT 1 INTERSECT SELECT 1",
    "SELECT 1; -- trailing comment",
    "SELECT 1;\n-- note",
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'uk'",
    "SELECT `settings` FROM uk.uk_price_paid",
    # round 2: deny-listed names are fine as table and column names (user packs, OWNER D-29)
    "SELECT * FROM uk.dictionary_terms",
    "SELECT * FROM uk.input",
    "SELECT format, settings, system FROM uk.uk_price_paid",
    "SELECT town FROM uk.uk_price_paid ORDER BY format DESC",
    "SELECT x FROM uk.uk_price_paid LEFT ARRAY JOIN [1, 2] AS x",
    "SELECT * FROM uk.a AS a JOIN (SELECT 1 AS id) AS b ON a.id = b.id",
    # round 3: ARRAY JOIN over array functions, IN over values, '/*' in a line comment
    "SELECT tag FROM uk.t ARRAY JOIN splitByChar(',', tags) AS tag",
    "SELECT i FROM uk.t ARRAY JOIN arrayEnumerate(arr) AS i",
    "SELECT x FROM uk.t LEFT ARRAY JOIN range(3) AS x",
    "SELECT x FROM uk.t ARRAY JOIN arrayMap(y -> y * 2, arr) AS x",
    "SELECT * FROM uk.t WHERE d IN (today())",
    "SELECT * FROM uk.t WHERE d IN (toDate('2024-01-01'))",
    "SELECT * FROM uk.t WHERE y IN (toYear(now()))",
    "SELECT * FROM uk.t WHERE m IN (toYYYYMM(today()))",
    "SELECT * FROM uk.t WHERE lower(town) IN (lower('x'))",
    "SELECT * FROM uk.t JOIN numbers(3) AS n ON 1",
    "SELECT format('{} {}', town, county) FROM uk.t",
    "SELECT * FROM uk.t WHERE lower(town) IN (lower('X'), 'y')",
    "SELECT * FROM uk.t WHERE town IN tuple('a', 'b')",
    "SELECT 1 -- see /* this",
    "SELECT 1 EXCEPT SELECT 2",
]

SF_BENIGN: list[str] = [
    "SELECT 1",
    "SELECT MEDIAN(PRICE) FROM ANALYTICS.UK.UK_PRICE_PAID",
    "SELECT YEAR(DATE) AS Y, COUNT(*) FROM ANALYTICS.UK.UK_PRICE_PAID GROUP BY Y ORDER BY Y",
    "SELECT TOWN, PRICE FROM ANALYTICS.UK.UK_PRICE_PAID "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY TOWN ORDER BY PRICE DESC) = 1",
    "WITH T AS (SELECT TOWN, PRICE FROM ANALYTICS.UK.UK_PRICE_PAID) "
    "SELECT TOWN, MAX(PRICE) FROM T GROUP BY TOWN",
    "SELECT 'a' AS K UNION ALL SELECT 'b'",
    "SELECT * FROM (SELECT TOWN, COUNT(*) AS N FROM ANALYTICS.UK.UK_PRICE_PAID GROUP BY TOWN) "
    "WHERE N > 100",
    "SELECT DATE_TRUNC('month', DATE) AS M, AVG(PRICE) FROM ANALYTICS.UK.UK_PRICE_PAID GROUP BY M",
    "SELECT * FROM TABLE(FLATTEN(INPUT => PARSE_JSON('[1, 2]')))",
    "SELECT F.VALUE FROM ANALYTICS.UK.UK_PRICE_PAID T, LATERAL FLATTEN(INPUT => "
    "ARRAY_CONSTRUCT(T.TOWN)) F",
    "SELECT SEQ4() FROM TABLE(GENERATOR(ROWCOUNT => 10))",
    "SELECT * FROM ANALYTICS.UK.UK_PRICE_PAID SAMPLE (10)",
    "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY PRICE) FROM ANALYTICS.UK.UK_PRICE_PAID",
    "SELECT IFF(PRICE > 1000000, 'high', 'low') AS B, COUNT(*) FROM ANALYTICS.UK.UK_PRICE_PAID "
    "GROUP BY B",
    "SELECT TOWN FROM ANALYTICS.UK.UK_PRICE_PAID WHERE TOWN ILIKE '%leeds%' LIMIT 3",
    "SELECT 'user@example.com' AS EMAIL",
    "SELECT 1 -- ; DROP TABLE X",
    "SELECT LAG(P) OVER (ORDER BY Y) FROM (SELECT YEAR(DATE) Y, MEDIAN(PRICE) P "
    "FROM ANALYTICS.UK.UK_PRICE_PAID GROUP BY Y)",
    "SELECT CURRENT_ROLE()",
    "SELECT 1; -- trailing comment",
    "SELECT TRANSLATE(TOWN, 'ae', 'AE') FROM ANALYTICS.UK.UK_PRICE_PAID",
    "SELECT * FROM ANALYTICS.UK.SENTIMENT",
    "SELECT AI_SCORES, PROMPT FROM ANALYTICS.UK.AI_SCORES",
    "SELECT * FROM T WHERE HANDLE LIKE '@%'",
    "SELECT '@handle' AS H",
    "SELECT $1 FROM VALUES (1), (2)",
    "SELECT DATEDIFF('day', MIN(DATE), MAX(DATE)) FROM ANALYTICS.UK.UK_PRICE_PAID",
]


def test_corpus_sizes_meet_the_spec() -> None:
    assert len(CH_HOSTILE) + len(SF_HOSTILE) >= 40
    assert len(CH_BENIGN) + len(SF_BENIGN) >= 40
    assert len(CH_HOSTILE) >= 40 and len(CH_BENIGN) >= 40


@pytest.mark.parametrize("sql", CH_HOSTILE)
def test_clickhouse_hostile_refused(sql: str) -> None:
    v = check_sql(sql, "clickhouse")
    assert not v.ok and v.reason, sql


@pytest.mark.parametrize("sql", SF_HOSTILE)
def test_snowflake_hostile_refused(sql: str) -> None:
    v = check_sql(sql, "snowflake")
    assert not v.ok and v.reason, sql


@pytest.mark.parametrize("sql", CH_BENIGN)
def test_clickhouse_benign_allowed(sql: str) -> None:
    assert check_sql(sql, "clickhouse") == GuardVerdict(True), sql


@pytest.mark.parametrize("sql", SF_BENIGN)
def test_snowflake_benign_allowed(sql: str) -> None:
    assert check_sql(sql, "snowflake") == GuardVerdict(True), sql


def test_reasons_name_the_problem() -> None:
    assert "one statement" in (check_sql("SELECT 1; SELECT 2", "clickhouse").reason or "")
    assert "url" in (check_sql("SELECT * FROM url('x', CSV)", "clickhouse").reason or "")
    assert "SETTINGS" in (check_sql("SELECT 1 SETTINGS readonly=0", "clickhouse").reason or "")
    assert "parse" in (check_sql("SELEC 1", "clickhouse").reason or "")
    assert "@stage" in (check_sql("SELECT * FROM @s", "snowflake").reason or "")
    assert "U+200B" in (check_sql("SELECT u​rl()", "clickhouse").reason or "")


def test_unknown_engine_and_non_string_refused() -> None:
    assert not check_sql("SELECT 1", "postgres").ok  # type: ignore[arg-type]
    assert not check_sql(None, "clickhouse").ok  # type: ignore[arg-type]
    assert not check_sql("SELECT " + "1+" * 60_000 + "1", "clickhouse").ok


def test_verdict_is_truthy_only_when_ok() -> None:
    assert check_sql("SELECT 1", "clickhouse")
    assert not check_sql("DROP TABLE x", "clickhouse")


_WRITES = [
    "DROP TABLE uk.t",
    "INSERT INTO uk.t VALUES (1)",
    "DELETE FROM uk.t WHERE 1",
    "ALTER TABLE uk.t DELETE WHERE 1",
    "TRUNCATE TABLE uk.t",
    "CREATE TABLE uk.x (a Int8) ENGINE = Memory",
    "UPDATE uk.t SET a = 1",
]
_SEPARATORS = [";", "; ", ";\n", " ;", "; -- c\n", ";/* x */", ";;"]


@settings(max_examples=150, deadline=None)
@given(
    read=st.sampled_from([r for r in CH_BENIGN if "--" not in r]),  # no line comment eats DML
    write=st.sampled_from(_WRITES),
    sep=st.sampled_from(_SEPARATORS),
    write_first=st.booleans(),
    engine=st.sampled_from(["clickhouse", "snowflake"]),
)
def test_property_never_allows_a_statement_with_dml(
    read: str, write: str, sep: str, write_first: bool, engine: str
) -> None:
    """Tech Spec §11: the guard never allows input whose parse contains DML."""
    sql = f"{write}{sep}{read}" if write_first else f"{read.rstrip(';')}{sep}{write}"
    assert not check_sql(sql, engine).ok  # type: ignore[arg-type]


# ---- isolated layers (each test fails if its own layer is removed; EK L-1) ---------------------

_SET_OP_SETTINGS = [
    "SELECT 1 UNION ALL SELECT 2 SETTINGS readonly=0",
    "(SELECT 1) SETTINGS readonly=0",
    "WITH a AS (SELECT 1 UNION ALL SELECT 2 SETTINGS readonly=0) SELECT * FROM a",
    "SELECT 1 EXCEPT SELECT 2 SETTINGS max_execution_time=1000",
    "SELECT 1 UNION ALL SELECT 2 FORMAT TSV",
]


@pytest.mark.parametrize("sql", _SET_OP_SETTINGS)
def test_tree_layer_alone_refuses_settings_and_format_on_every_query_node(sql: str) -> None:
    problem = guard._check_tree(sqlglot.parse_one(sql, read="clickhouse"), "clickhouse")
    assert problem and ("SETTINGS" in problem or "FORMAT" in problem)


@pytest.mark.parametrize("sql", _SET_OP_SETTINGS)
def test_token_layer_alone_refuses_settings_and_format(sql: str) -> None:
    problem = guard._token_problem(sqlglot.tokenize(sql, read="clickhouse"), "clickhouse", sql)
    assert problem and ("SETTINGS" in problem or "FORMAT" in problem)


def test_token_layer_allows_format_function_calls() -> None:
    sql = "SELECT formatDateTime(now(), '%Y') AS y"
    assert guard._token_problem(sqlglot.tokenize(sql, read="clickhouse"), "clickhouse", sql) is None


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT * FROM 'plain_string_table'", "string literal is not a table"),
        ("SELECT GET_PRESIGNED_URL(@stage, 'f.csv') AS u", "function get_presigned_url"),
        ("SELECT * FROM TABLE(SOME_FUTURE_UDTF(1))", "table function"),
        ("SELECT COMPLETE('m', 'x')", "function complete"),
    ],
)
def test_snowflake_rules_each_name_their_reason(sql: str, fragment: str) -> None:
    assert fragment in (check_sql(sql, "snowflake").reason or "")


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT * FROM s3Queue('https://bucket/x.csv', CSV)", "table function s3queue"),
        ("SELECT * FROM someFutureTableFunction(1)", "table function somefuturetablefunction"),
        ("SELECT \uff46\uff49\uff4c\uff45('/etc/passwd')", "function file"),
        ("SELECT * FROM system.query_log", "system database"),
        ("SELECT dictIsIn('r', toUInt64(1), toUInt64(2))", "function dictisin"),
    ],
)
def test_clickhouse_rules_each_name_their_reason(sql: str, fragment: str) -> None:
    assert fragment in (check_sql(sql, "clickhouse").reason or "")


def test_trailing_semicolon_then_comment_is_one_statement_but_more_sql_is_not() -> None:
    assert check_sql("SELECT 1; -- note", "clickhouse").ok
    assert not check_sql("SELECT 1; -- note\nDROP TABLE x", "clickhouse").ok


def test_known_limitation_limit_by_then_limit_is_refused_as_unparseable() -> None:
    """sqlglot cannot parse ClickHouse `LIMIT n BY x LIMIT m`: a safe false positive (spec)."""
    v = check_sql("SELECT * FROM uk.t ORDER BY price DESC LIMIT 5 BY town LIMIT 100", "clickhouse")
    assert not v.ok and "parse" in (v.reason or "")


# ---- round 2: each layer on its own (the other layer would mask a removed check) -------------


def _tree(sql: str, engine: str = "clickhouse") -> str | None:
    return guard._check_tree(sqlglot.parse_one(sql, read=engine), engine)  # type: ignore[arg-type]


def _toks(sql: str, engine: str = "clickhouse") -> str | None:
    return guard._token_problem(sqlglot.tokenize(sql, read=engine), engine, sql)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT * FROM t ARRAY JOIN (SELECT groupArray(x) FROM ytsaurus('a')) AS n", "ARRAY JOIN"),
        ("SELECT * FROM t LEFT ARRAY JOIN (SELECT [1] AS a) AS n", "ARRAY JOIN"),
        ("SELECT * FROM t WHERE a IN system.users", "system"),
        ("SELECT * FROM t WHERE a IN (system.users)", "system"),
        ("SELECT * FROM t, dictionary('d')", "table function call"),
    ],
)
def test_tree_layer_alone_refuses_round2_shapes(sql: str, fragment: str) -> None:
    assert fragment in (_tree(sql) or "")


def test_tree_layer_alone_refuses_snowflake_string_tables() -> None:
    assert "string literal" in (_tree("SELECT * FROM '@~/f.csv'", "snowflake") or "")


def test_tree_layer_refuses_unknown_from_targets() -> None:
    sub = sqlglot.parse_one("SELECT 1 FROM t", read="clickhouse")
    sub.args["from_"].set("this", sqlglot_exp.column("x"))  # the shape seen inside ARRAY JOIN
    assert "FROM/JOIN target Column" in (guard._check_tree(sub, "clickhouse") or "")


@pytest.mark.parametrize(
    ("sql", "engine", "fragment"),
    [
        ("SELECT 1 INTO :v", "snowflake", "INTO"),  # the tree catches it too (Into node)
        ("SELECT * FROM t ARRAY JOIN (SELECT x FROM s3Queue('x')) AS n", "clickhouse", "s3queue"),
        ("SELECT * FROM t JOIN arrowFlight('h:1', 'd') AS f ON 1", "clickhouse", "arrowflight"),
        ("SELECT * FROM t WHERE a IN system.users", "clickhouse", "system"),
        ("SELECT 1 /* a /* b */ c */", "clickhouse", "nested comments"),
        ("SELECT * FROM TABLE(FLATTEN(INPUT => [1]))", "snowflake", None),
        ("SELECT * FROM numbers(3)", "clickhouse", None),
        ("SELECT format, settings FROM t ORDER BY format DESC", "clickhouse", None),
    ],
)
def test_token_layer_alone(sql: str, engine: str, fragment: str | None) -> None:
    got = _toks(sql, engine)
    if fragment is None:
        assert got is None
    else:
        assert fragment in (got or "")


def test_only_numeric_positional_parameters_are_allowed() -> None:
    root = sqlglot.parse_one("SELECT $1 FROM VALUES (1)", read="snowflake")
    assert guard._check_tree(root, "snowflake") is None
    param = root.find(sqlglot_exp.Parameter)
    assert param is not None
    param.set("this", sqlglot_exp.Literal.string("1"))  # not a positional column
    assert "@stage" in (guard._check_tree(root, "snowflake") or "")


# ---- round 3: isolated --------------------------------------------------------------------------


_TABLE_FN_CALLS = [
    "SELECT * FROM t WHERE a IN ytsaurus('x')",
    "SELECT * FROM t WHERE a GLOBAL IN s3Queue('x')",
    "SELECT * FROM t WHERE notIn(a, s3Queue('x'))",
    "SELECT * FROM t WHERE globalIn(a, ytsaurus('x'))",
    "SELECT * FROM t WHERE globalNotIn(a, ytsaurus('x'))",
    "SELECT * FROM t WHERE nullIn(a, s3Queue('x'))",
    "SELECT * FROM t WHERE notNullIn(a, arrowFlight('h:1', 'd'))",
    "SELECT * FROM t WHERE globalNullIn(a, s3Queue('x'))",
    "SELECT * FROM t WHERE globalNotNullIn(a, primes(10))",
    "SELECT * FROM t WHERE a IN ((s3Queue('x')))",
    "SELECT * FROM t WHERE a IN (((ytsaurus('x'))))",
    "SELECT * FROM t WHERE a IN numbers(3)",
]


@pytest.mark.parametrize("sql", _TABLE_FN_CALLS)
def test_table_function_names_are_refused_as_calls_anywhere(sql: str) -> None:
    """Round 4: name-based. These names are on no other deny list, so only this rule catches
    them; the reason names the table function."""
    assert "table function" in (_tree(sql) or "") and "not allowed here" in (_tree(sql) or "")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM numbers(3)",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM t JOIN zeros(2) AS z ON 1",
        "SELECT * FROM t WHERE d IN (today())",
        "SELECT * FROM t WHERE d IN (toDate('2024-01-01'))",
        "SELECT * FROM t WHERE y IN (toYear(now()))",
        "SELECT * FROM t WHERE m IN (toYYYYMM(today()))",
        "SELECT * FROM t WHERE lower(a) IN (lower('x'))",
        "SELECT * FROM t WHERE a IN (someUnknownScalar(1))",  # accepted residual risk (spec)
    ],
)
def test_generators_as_sources_and_scalars_in_in_are_allowed(sql: str) -> None:
    assert _tree(sql) is None and check_sql(sql, "clickhouse").ok


def test_every_listed_table_function_is_caught_by_name() -> None:
    for name in sorted(guard.CLICKHOUSE_TABLE_FUNCTIONS):
        assert guard._table_function_call_problem(
            sqlglot_exp.Anonymous(this=name, expressions=[]), "clickhouse", frozenset()
        ), name
    assert {
        "s3queue",
        "ytsaurus",
        "arrowflight",
        "primes",
        "url",
    } <= guard.CLICKHOUSE_TABLE_FUNCTIONS
    assert "format" not in guard.CLICKHOUSE_TABLE_FUNCTIONS  # also a string function


def test_token_layer_skips_array_join_expressions() -> None:
    sql = "SELECT x FROM t LEFT ARRAY JOIN range(3) AS x"
    assert _toks(sql) is None
    assert "table function" in (_toks("SELECT x FROM t JOIN range(3) AS x ON 1") or "")


def test_line_comment_mentioning_block_start_is_not_nested() -> None:
    assert _toks("SELECT 1 -- see /* this") is None
    assert "nested" in (_toks("SELECT 1 /* a /* b */ c */") or "")


def test_snowflake_stage_file_functions_are_denied() -> None:
    for fn in (
        "TO_FILE",
        "BUILD_SCOPED_FILE_URL",
        "BUILD_STAGE_FILE_URL",
        "GET_STAGE_LOCATION",
        "GET_RELATIVE_PATH",
        "GET_ABSOLUTE_PATH",
        "GET_PRESIGNED_URL",
    ):
        v = check_sql(f"SELECT {fn}('x')", "snowflake")
        assert not v.ok and f"function {fn.lower()}" in (v.reason or ""), fn


def test_table_function_families_are_caught_by_prefix() -> None:
    """Future variants of the lake-format families (not listed by name) are still table calls."""
    for name in ("deltaLakeGCS", "hudiS3", "icebergFuture", "paimonGCS"):
        assert name.casefold() not in guard.CLICKHOUSE_TABLE_FUNCTIONS
        node = sqlglot_exp.Anonymous(this=name, expressions=[])
        assert guard._table_function_call_problem(node, "clickhouse", frozenset()), name


# ClickHouse docs, table functions, fetched 2026-11-15 (frozen copy; EK L-10: an independent list)
DOCUMENTED_CH_TABLE_FUNCTIONS = (
    "arrowFlight", "azureBlobStorage", "azureBlobStorageCluster", "bigquery", "clusterAllReplicas",
    "deltaLake", "deltaLakeCluster", "dictionary", "elasticsearch", "executable", "eval", "file",
    "fileCluster", "filesystem", "format", "fuzzJSON", "fuzzQuery", "gcs", "generateRandom",
    "generateSeries", "generate_series", "hdfs", "hdfsCluster", "hudi", "hudiCluster", "iceberg",
    "icebergCluster", "input", "jdbc", "loop", "merge", "mergeTreeCodecBlockCounts",
    "mergeTreeIndex", "mergeTreeProjection", "mergeTreeTextIndex", "mongodb", "mysql", "null",
    "numbers", "odbc", "paimon", "paimonCluster", "postgresql", "primes", "prometheusQuery",
    "prometheusQueryRange", "redis", "remote", "remoteSecure", "s3", "s3Cluster", "sqlite",
    "timeSeriesData", "timeSeriesMetricFamilies", "timeSeriesSamples", "timeSeriesSelector",
    "timeSeriesTags", "timeSeriesTimeRanges", "url", "urlCluster", "values", "view", "ytsaurus",
    "zeros",
)  # fmt: skip


@pytest.mark.parametrize("name", [n for n in DOCUMENTED_CH_TABLE_FUNCTIONS if n != "format"])
def test_every_documented_table_function_is_refused_as_a_call_in_in(name: str) -> None:
    assert name.casefold() in guard.CLICKHOUSE_TABLE_FUNCTIONS
    for sql in (
        f"SELECT * FROM uk.t WHERE town IN {name}('x')",
        f"SELECT * FROM uk.t WHERE notIn(town, {name}('x'))",
    ):
        assert not check_sql(sql, "clickhouse").ok, sql


def test_format_is_refused_as_a_source_but_allowed_as_a_string_function() -> None:
    assert not check_sql("SELECT * FROM format(CSV, '1')", "clickhouse").ok
    assert check_sql("SELECT format('{}-{}', town, county) FROM uk.t", "clickhouse").ok


def test_primes_is_a_generator_source_only() -> None:
    assert check_sql("SELECT * FROM primes(10)", "clickhouse").ok
    assert not check_sql("SELECT * FROM uk.t WHERE n IN primes(10)", "clickhouse").ok


def test_values_is_a_source_only() -> None:
    assert check_sql("SELECT * FROM values('a Int', 1, 2)", "clickhouse").ok
    assert "values" in (_tree("SELECT * FROM t WHERE notIn(a, values('x'))") or "")
