"""The bundled `uk_price_paid` pack: manifest, the one transform spec, probes and parity SQL
(LOD-01, LOD-02, LOD-03, LOD-04, LOD-07)."""

from __future__ import annotations

import re

import pytest
import sqlglot
from sqlglot import exp

from agentic_analytics.load._common import render, split_statements
from agentic_analytics.load.parity import load_parity_spec, rendered_query
from agentic_analytics.packs import load_pack, read_pack_text
from agentic_analytics.packs.uk_price_paid import transforms as tf
from agentic_analytics.warehouse.guard import check_sql
from agentic_analytics.warehouse.roles import pack_role_sql
from tests.load.fakes import values_rows

# ClickHouse docs, UK property prices example (verified 15 Nov 2026), copied verbatim. This is
# the independent oracle for LOD-01: the pack's SQL must equal it apart from the database name.
DOCUMENTED = (
    "CREATE DATABASE uk; CREATE TABLE uk.uk_price_paid (price UInt32, date Date, postcode1 "
    "LowCardinality(String), postcode2 LowCardinality(String), type Enum8('terraced' = 1, "
    "'semi-detached' = 2, 'detached' = 3, 'flat' = 4, 'other' = 0), is_new UInt8, duration "
    "Enum8('freehold' = 1, 'leasehold' = 2, 'unknown' = 0), addr1 String, addr2 String, street "
    "LowCardinality(String), locality LowCardinality(String), town LowCardinality(String), "
    "district LowCardinality(String), county LowCardinality(String)) ENGINE = MergeTree ORDER BY "
    "(postcode1, postcode2, addr1, addr2); INSERT INTO uk.uk_price_paid SELECT "
    "toUInt32(price_string) AS price, parseDateTimeBestEffortUS(time) AS date, "
    "splitByChar(' ', postcode)[1] AS postcode1, splitByChar(' ', postcode)[2] AS postcode2, "
    "transform(a, ['T','S','D','F','O'], ['terraced','semi-detached','detached','flat','other']) "
    "AS type, b = 'Y' AS is_new, transform(c, ['F','L','U'], ['freehold','leasehold','unknown']) "
    "AS duration, addr1, addr2, street, locality, town, district, county FROM url("
    "'http://prod1.publicdata.landregistry.gov.uk.s3-website-eu-west-1.amazonaws.com/"
    "pp-complete.csv', 'CSV', 'uuid_string String, price_string String, time String, postcode "
    "String, a String, b String, c String, addr1 String, addr2 String, street String, locality "
    "String, town String, district String, county String, d String, e String') SETTINGS "
    "max_http_get_redirects=10;"
)
ATTRIBUTION = (
    "Contains HM Land Registry data © Crown copyright and database right 2021. "
    "This data is licensed under the Open Government Licence v3.0."
)
OUTPUT_COLUMNS = (
    "price",
    "date",
    "postcode1",
    "postcode2",
    "type",
    "is_new",
    "duration",
    "addr1",
    "addr2",
    "street",
    "locality",
    "town",
    "district",
    "county",
)


def canonical(sql: str) -> str:
    """Comments dropped, whitespace collapsed, no space just inside parentheses."""
    body = "; ".join(split_statements(sql)) + ";"
    body = " ".join(body.split())
    return re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", body))


@pytest.fixture(scope="module")
def pack():
    return load_pack("uk_price_paid")


# ---- manifest -------------------------------------------------------------------------------


def test_manifest_values(pack) -> None:
    m = pack.manifest
    assert m.engines == ("clickhouse", "snowflake")
    assert dict(m.databases) == {"clickhouse": "uk", "snowflake": "ANALYTICS.UK"}
    assert m.tables == ("uk_price_paid",)
    assert m.as_of_rule == "last_complete_month" and m.as_of is None
    assert dict(m.quotas) == {
        "aggregation": 17,
        "time": 17,
        "geography": 17,
        "top_n": 17,
        "yoy": 16,
        "ambiguous": 16,
    }
    assert sum(m.quotas.values()) == 100
    assert m.licence == "OGL-3.0"
    assert m.attribution == ATTRIBUTION
    assert m.source_url == (
        "https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads"
    )


def test_readme_carries_the_same_attribution(pack) -> None:
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert pack.manifest.attribution in readme


# ---- LOD-01: documented ClickHouse load -----------------------------------------------------


def test_clickhouse_load_sql_equals_the_documented_statement_modulo_database(pack) -> None:
    text = read_pack_text(pack, "load/clickhouse.sql")
    assert canonical(render(text, {"database": "uk"})) == canonical(DOCUMENTED)
    other = canonical(render(text, {"database": "uk_test"}))
    assert other == canonical(DOCUMENTED).replace(" uk.", " uk_test.").replace(
        "CREATE DATABASE uk;", "CREATE DATABASE uk_test;"
    )


def test_canonical_form_notices_a_one_character_change(pack) -> None:
    text = render(read_pack_text(pack, "load/clickhouse.sql"), {"database": "uk"})
    assert canonical(text.replace("max_http_get_redirects=10", "max_http_get_redirects=1")) != (
        canonical(DOCUMENTED)
    )


def test_pack_sql_files_are_the_transform_spec_renderings(pack) -> None:
    assert read_pack_text(pack, "load/clickhouse.sql") == tf.render_clickhouse_sql()
    assert read_pack_text(pack, "load/snowflake.sql") == tf.render_snowflake_sql()


# ---- LOD-02: one transform spec drives both engines -----------------------------------------


def _insert_select(sql: str, dialect: str) -> exp.Select:
    stmts = [s for s in split_statements(sql) if s.upper().startswith("INSERT INTO")]
    assert len(stmts) == 1
    tree = sqlglot.parse_one(stmts[0], read=dialect)
    select = tree.find(exp.Select)
    assert select is not None
    return select


def test_both_renderings_produce_the_same_output_columns_in_the_same_order() -> None:
    ch = _insert_select(render(tf.render_clickhouse_sql(), {"database": "uk"}), "clickhouse")
    sf_sql = render(
        tf.render_snowflake_sql(),
        {"catalog": "A", "database": "A.B", "local_file": "/x.gz", "stage_file": "x.gz"},
    )
    sf = _insert_select(sf_sql, "snowflake")
    assert tuple(ch.named_selects) == tuple(sf.named_selects) == OUTPUT_COLUMNS
    assert tf.output_columns() == OUTPUT_COLUMNS


def test_typed_table_ddl_lists_the_output_columns_in_order_on_both_engines() -> None:
    def ddl_columns(sql: str, dialect: str, table: str) -> list[str]:
        for s in split_statements(sql):
            if re.match(rf"CREATE (OR REPLACE )?TABLE (IF NOT EXISTS )?\S+\.{table}\s*\(", s):
                tree = sqlglot.parse_one(s, read=dialect)
                return [c.name for c in tree.find_all(exp.ColumnDef)]
        raise AssertionError(f"no CREATE TABLE for {table}")

    ch = render(tf.render_clickhouse_sql(), {"database": "uk"})
    sf = render(
        tf.render_snowflake_sql(),
        {"catalog": "A", "database": "A.B", "local_file": "/x.gz", "stage_file": "x.gz"},
    )
    assert ddl_columns(ch, "clickhouse", "uk_price_paid") == list(OUTPUT_COLUMNS)
    assert ddl_columns(sf, "snowflake", "uk_price_paid") == list(OUTPUT_COLUMNS)
    raw = ddl_columns(sf, "snowflake", "uk_price_paid_raw")
    assert raw == list(tf.RAW_COLUMNS) and len(raw) == 16


def test_snowflake_raw_table_is_sixteen_varchars_matching_the_clickhouse_url_structure() -> None:
    sf = tf.render_snowflake_sql()
    for col in tf.RAW_COLUMNS:
        assert f"    {col} VARCHAR" in sf
    structure = re.search(r"'(uuid_string String[^']*)'", tf.render_clickhouse_sql())
    assert structure is not None
    assert [p.split()[0] for p in structure[1].split(", ")] == list(tf.RAW_COLUMNS)


def test_snowflake_transforms_mirror_the_clickhouse_ones() -> None:
    sf: dict[str, str] = dict(zip(OUTPUT_COLUMNS, tf.snowflake_select_items(), strict=True))
    assert sf["is_new"] == "IFF(b = 'Y', 1, 0) AS is_new"
    assert sf["date"] == "TRY_TO_DATE(LEFT(time, 10), 'YYYY-MM-DD') AS date"
    assert sf["postcode1"] == "SPLIT_PART(postcode, ' ', 1) AS postcode1"
    assert sf["postcode2"] == "SPLIT_PART(postcode, ' ', 2) AS postcode2"
    for col, codes in (("type", tf.TYPE_MAP), ("duration", tf.DURATION_MAP)):
        for code, label in codes:
            assert f"WHEN '{code}' THEN '{label}'" in sf[col]
    typed = {c.name: c.sf_type for c in tf.COLUMNS}
    assert typed["is_new"] == "NUMBER(1, 0)" and typed["type"] == "VARCHAR"
    assert "EMPTY_FIELD_AS_NULL = FALSE" in tf.render_snowflake_sql()


def test_enum_labels_are_exactly_the_mapping_targets() -> None:
    for col in tf.COLUMNS:
        if col.op == "map":
            labels = re.findall(r"'([^']+)' = \d+", col.ch_type)
            assert sorted(labels) == sorted(v for _, v in col.mapping)


def test_python_reference_transform_on_a_raw_record() -> None:
    raw = [
        "{X}",
        "87500",
        "1999-08-20 00:00",
        "SW11 6PB",
        "F",
        "Y",
        "L",
        "41",
        "FLAT 2",
        "ST JOHN'S HILL",
        "",
        "LONDON",
        "WANDSWORTH",
        "GREATER LONDON",
        "A",
        "A",
    ]
    row = tf.apply_row(raw)
    assert list(row) == list(OUTPUT_COLUMNS)
    assert row["price"] == 87500 and str(row["date"]) == "1999-08-20"
    assert (row["postcode1"], row["postcode2"]) == ("SW11", "6PB")
    assert (row["type"], row["is_new"], row["duration"]) == ("flat", 1, "leasehold")
    assert tf.apply_row([*raw[:3], "", *raw[4:]])["postcode2"] == ""
    with pytest.raises(ValueError):
        tf.apply_row(raw[:15])


def test_rendered_snowflake_sql_parses() -> None:
    sql = render(
        tf.render_snowflake_sql(),
        {"catalog": "A", "database": "A.B", "local_file": "/x.gz", "stage_file": "x.gz"},
    )
    for s in split_statements(sql):
        if s.upper().startswith(("INSERT", "TRUNCATE", "CREATE TABLE")):
            sqlglot.parse_one(s, read="snowflake")


# ---- LOD-04: probes -------------------------------------------------------------------------


def _probe_rows(pack, engine: str) -> list[tuple]:
    params = {"probes_database": "p", "catalog": "c"}
    sql = render(read_pack_text(pack, f"probes/{engine}.sql"), params)
    ins = [s for s in split_statements(sql) if s.upper().startswith("INSERT")]
    assert len(ins) == 1
    return values_rows(ins[0], engine)


def test_probes_plant_ten_identical_rows_on_both_engines_with_unique_markers(pack) -> None:
    ch, sf = _probe_rows(pack, "clickhouse"), _probe_rows(pack, "snowflake")
    assert ch == sf and len(ch) == 10
    markers = [f"INJ-{i:02d}" for i in range(1, 11)]
    for (note_id, _, town, note), marker in zip(ch, markers, strict=True):  # D-51: 4 columns
        assert town and marker in note
        assert sum(m in note for m in markers) == 1
        assert isinstance(note_id, int)


def test_probes_databases_are_the_ones_the_read_only_roles_grant(pack) -> None:
    assert "GRANT SELECT ON uk_probes.* TO analytics_ro;" in pack_role_sql(
        pack.manifest, "clickhouse"
    )
    assert "SCHEMA ANALYTICS.UK_PROBES TO ROLE ANALYTICS_RO" in pack_role_sql(
        pack.manifest, "snowflake"
    )


# ---- LOD-03: parity fingerprints ------------------------------------------------------------


TEXT_COLUMNS = (
    "postcode1", "postcode2", "addr1", "addr2", "street", "locality", "town", "district", "county",
)  # fmt: skip
EXPECTED_FINGERPRINTS = {
    *(f"text_{c}" for c in TEXT_COLUMNS),
    "row_count",
    "sum_price",
    "by_type",
    "by_duration",
    "by_is_new",
    "date_range",
    "distinct_towns",
    "per_year",
}


def test_parity_covers_the_catalog_fingerprints_and_every_query_is_guard_clean(pack) -> None:
    spec = load_parity_spec(pack)
    assert {f.name for f in spec.fingerprints} == EXPECTED_FINGERPRINTS
    for fp in spec.fingerprints:
        for engine in pack.manifest.engines:
            assert check_sql(rendered_query(pack, fp, engine), engine).ok


def test_parity_queries_return_identically_named_columns_on_both_engines(pack) -> None:
    for fp in load_parity_spec(pack).fingerprints:
        names = {}
        for engine in pack.manifest.engines:
            tree = sqlglot.parse_one(rendered_query(pack, fp, engine), read=engine)
            assert isinstance(tree, exp.Select)
            names[engine] = [n.casefold() for n in tree.named_selects]
        assert names["clickhouse"] == names["snowflake"], fp.name


FINGERPRINT_COLUMNS = {
    "row_count": set(),
    "sum_price": {"price"},
    "by_type": {"type"},
    "by_duration": {"duration"},
    "by_is_new": {"is_new"},
    "date_range": {"date"},
    "distinct_towns": {"town"},
    "per_year": {"date"},
    **{f"text_{c}": {c} for c in TEXT_COLUMNS},
}


def test_each_fingerprint_reads_exactly_its_own_column_on_both_engines(pack) -> None:
    """Verifier: the fakes answer a fingerprint by its name, never by running its SQL, so a
    query reading the wrong column (`text_county` counting `district` on both engines, which
    live parity would also pass) is caught only here."""
    for fp in load_parity_spec(pack).fingerprints:
        for engine in pack.manifest.engines:
            tree = sqlglot.parse_one(rendered_query(pack, fp, engine), read=engine)
            assert isinstance(tree, exp.Select)
            aliases = {n.casefold() for n in tree.named_selects}
            read = {c.name.casefold() for c in tree.find_all(exp.Column)} - aliases
            assert read == FINGERPRINT_COLUMNS[fp.name], (fp.name, engine, read)
            tables = {t.name.casefold() for t in tree.find_all(exp.Table)}
            assert tables == {"uk_price_paid"}, (fp.name, engine, tables)


def test_parity_queries_return_text_dates_and_text_enums(pack) -> None:
    spec = {f.name: f for f in load_parity_spec(pack).fingerprints}
    assert "toString(min(date))" in spec["date_range"].query("clickhouse")
    assert "TO_VARCHAR(MIN(date), 'YYYY-MM-DD')" in spec["date_range"].query("snowflake")
    assert "CAST(type AS String)" in spec["by_type"].query("clickhouse")
    assert "CAST(duration AS String)" in spec["by_duration"].query("clickhouse")


def test_every_text_column_of_the_spec_has_a_text_fingerprint(pack) -> None:
    names = {f.name for f in load_parity_spec(pack).fingerprints}
    text = [c.name for c in tf.COLUMNS if c.op in ("copy", "split")]
    assert sorted(text) == sorted(TEXT_COLUMNS)
    assert all(f"text_{c}" in names for c in text)


def test_snowflake_file_format_pins_empty_strings() -> None:
    fmt = next(s for s in split_statements(tf.render_snowflake_sql()) if "FILE FORMAT" in s)
    flat = " ".join(fmt.split())
    assert "NULL_IF = ()" in flat and "EMPTY_FIELD_AS_NULL = FALSE" in flat
