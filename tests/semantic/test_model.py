"""SEM-01: the semantic model, the bundled semantic.yaml and its check against the load SQL."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentic_analytics.packs import load_pack
from agentic_analytics.semantic import (
    SemanticError,
    SemanticModel,
    check_columns,
    comment_text,
    load_semantic,
    pack_table_columns,
    parse_semantic,
)
from tests.semantic.helpers import BASE, COLUMNS, data, mini


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return load_semantic(load_pack("uk_price_paid"))


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "uk_price_paid"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _item(m: SemanticModel, name: str) -> Any:
    for it in (*m.dimensions, *m.facts, *m.metrics):
        if it.name == name:
            return it
    raise KeyError(name)


# ---- the bundled file ----------------------------------------------------------------------


def test_bundled_model_has_the_required_items(model: SemanticModel) -> None:
    assert model.table.name == "uk_price_paid"
    dims = [d.name for d in model.dimensions]
    assert dims == [
        "town",
        "district",
        "county",
        "locality",
        "street",
        "postcode1",
        "postcode2",
        "type",
        "duration",
        "is_new",
        "date",
        "year",
        "month",
    ]
    assert [f.name for f in model.facts] == ["price"]
    assert {m.name: (m.agg, m.fact) for m in model.metrics} == {
        "sales_count": ("count", "price"),
        "total_value": ("sum", "price"),
        "average_price": ("avg", "price"),
        "median_price": ("median", "price"),
    }
    date = _item(model, "date")
    assert date.kind == "time" and date.transform is None
    assert (_item(model, "year").transform, _item(model, "month").transform) == ("year", "month")


@pytest.mark.parametrize(
    ("name", "synonyms"),
    [
        ("town", {"city"}),
        ("district", {"borough"}),
        ("is_new", {"new build"}),
        ("type", {"property type", "house type"}),
        ("duration", {"tenure"}),
    ],
)
def test_bundled_synonyms(model: SemanticModel, name: str, synonyms: set[str]) -> None:
    assert synonyms <= set(_item(model, name).synonyms)


def test_owner_d67_synonyms_and_as_of_wording(model: SemanticModel) -> None:
    """OWNER D-67: "area" would pre-empt the ambiguous q-103 and "house prices" clashes with
    houses = terraced/semi-detached/detached (q-110); the as-of wording follows D-50."""
    every = {
        s.casefold()
        for item in (model.table, *model.dimensions, *model.facts, *model.metrics)
        for s in item.synonyms
    }
    assert "area" not in every
    assert not {"house price", "house prices"} & every
    assert "the last day of the month before the latest month in the data" in model.description
    assert "last complete month" not in model.description


def test_bundled_sample_values(model: SemanticModel) -> None:
    assert _item(model, "type").sample_values == (
        "terraced",
        "semi-detached",
        "detached",
        "flat",
        "other",
    )
    assert _item(model, "duration").sample_values == ("freehold", "leasehold", "unknown")
    towns = _item(model, "town").sample_values
    assert len(towns) >= 3 and all(t == t.upper() for t in towns)
    assert set(_item(model, "is_new").sample_values) == {"0", "1"}


def test_bundled_descriptions_state_the_conventions(model: SemanticModel) -> None:
    overview = model.description
    for phrase in ("uppercase", "GBP", "1995", "as-of date", "registered", "under-counted"):
        assert phrase in overview, phrase
    assert "completion date" in _item(model, "date").description
    assert "GBP" in _item(model, "price").description
    for place in ("town", "district", "county", "locality", "street"):
        assert "uppercase" in _item(model, place).description, place
    for value in ("terraced", "semi-detached", "detached", "flat", "other"):
        assert value in _item(model, "type").description
    for value in ("freehold", "leasehold", "unknown"):
        assert value in _item(model, "duration").description
    assert "1" in _item(model, "is_new").description


def test_bundled_columns_come_from_both_load_files() -> None:
    cols = pack_table_columns(load_pack("uk_price_paid"), "uk_price_paid")
    assert list(cols) == [
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
    ]
    assert cols["price"] == {"clickhouse": "UInt32", "snowflake": "NUMBER(10, 0)"}
    assert cols["date"] == {"clickhouse": "Date", "snowflake": "DATE"}
    assert cols["type"]["clickhouse"].startswith("Enum8(")


def test_model_is_frozen(model: SemanticModel) -> None:
    with pytest.raises(ValidationError):
        model.name = "other"  # type: ignore[misc]
    assert isinstance(model.dimensions, tuple)
    assert isinstance(model.dimensions[0].synonyms, tuple)


def test_comment_text_appends_sample_values(model: SemanticModel) -> None:
    town = _item(model, "town")
    text = comment_text(town)
    assert text.startswith(town.description)
    assert text.endswith("Sample values: LONDON, MANCHESTER, BIRMINGHAM, LEEDS, BRISTOL.")
    price = _item(model, "price")
    assert comment_text(price) == price.description


# ---- model validation ----------------------------------------------------------------------


def test_fixture_model_is_valid() -> None:
    assert mini().name == "mini"


def _bad(d: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        SemanticModel.model_validate(d)


def test_duplicate_name_across_kinds_refused() -> None:
    d = data()
    d["facts"][0]["name"] = "town"
    _bad(d, "name 'town' is used more than once")


def test_duplicate_dimension_name_refused() -> None:
    d = data()
    d["dimensions"].append(dict(d["dimensions"][1]))
    _bad(d, "name 'date' is used more than once")


def test_duplicate_synonym_refused_case_insensitive() -> None:
    d = data()
    d["facts"][0]["synonyms"] = ["CITY"]
    _bad(d, "synonym 'CITY' is used more than once")


def test_synonym_equal_to_a_name_refused() -> None:
    d = data()
    d["dimensions"][0]["synonyms"] = ["Price"]
    _bad(d, "synonym 'Price' is also the name")


def test_table_synonym_clash_refused() -> None:
    d = data()
    d["table"]["synonyms"] = ["city"]
    _bad(d, "synonym 'city' is used more than once")


def test_metric_over_unknown_fact_refused() -> None:
    d = data()
    d["metrics"][0]["fact"] = "cost"
    _bad(d, "metric 'median_price' uses unknown fact 'cost'")


def test_plain_time_dimension_required() -> None:
    d = data()
    d["dimensions"] = [x for x in d["dimensions"] if x["name"] != "date"]
    _bad(d, "needs a time dimension without a transform")


def test_transform_on_categorical_refused() -> None:
    d = data()
    d["dimensions"][0]["transform"] = "year"
    _bad(d, "transform needs kind: time")


@pytest.mark.parametrize(
    "text",
    ["", "   ", "two\nlines", "tab\there", " edge", "a\u2028b", "a\u2029b", "a\x85b", "a\x1cb"],
)
def test_bad_description_text_refused(text: str) -> None:
    d = data()
    d["dimensions"][0]["description"] = text
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


@pytest.mark.parametrize("syn", ['say "hi"', "a,b", "", "x\n", "a\u2028b"])
def test_bad_synonym_refused(syn: str) -> None:
    d = data()
    d["dimensions"][0]["synonyms"] = [syn]
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


@pytest.mark.parametrize("value", ["A\u2028B", "A\x85B", "A\nB"])
def test_line_separators_in_sample_values_refused(value: str) -> None:
    d = data()
    d["dimensions"][0]["sample_values"] = [value]
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


@pytest.mark.parametrize("text", ["a\x80b", "a\x9bb", "a\x9fb"])
def test_c1_control_characters_refused(text: str) -> None:
    """N1.4 verifier: "no control characters" covers C1 (U+0080-U+009F), not only C0."""
    for field in ("description", "sample_values"):
        d = data()
        d["dimensions"][0][field] = [text.upper()] if field == "sample_values" else text
        with pytest.raises(ValidationError):
            SemanticModel.model_validate(d)


@pytest.mark.parametrize("value", ["ST. IVES, CORNWALL", "O'NEILL", 'SAY "HI"'])
def test_sample_value_with_a_comma_or_quote_refused(value: str) -> None:
    """Sample values are joined with ", " in the comment text, so a comma would split one."""
    d = data()
    d["dimensions"][0]["sample_values"] = ["LONDON", value]
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


def test_duplicate_sample_values_refused() -> None:
    d = data()
    d["dimensions"][0]["sample_values"] = ["LONDON", "LONDON"]
    _bad(d, "duplicate sample value")


@pytest.mark.parametrize("name", ["Town", "1town", "town-x", "select me"])
def test_bad_names_refused(name: str) -> None:
    d = data()
    d["dimensions"][0]["name"] = name
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


def test_unknown_key_and_version_refused() -> None:
    _bad(data(extra=1), "extra")
    _bad(data(schema_version="2.0"), "schema_version")


def test_sample_values_must_be_text() -> None:
    d = data()
    d["dimensions"][0]["sample_values"] = [1, 0]
    with pytest.raises(ValidationError):
        SemanticModel.model_validate(d)


# ---- columns against the load SQL ----------------------------------------------------------


def test_check_columns_accepts_known_columns() -> None:
    assert check_columns(mini(), "sales", COLUMNS) == ()


def test_unknown_column_refused() -> None:
    d = data()
    d["dimensions"][0]["column"] = "city_name"
    issues = check_columns(SemanticModel.model_validate(d), "sales", COLUMNS)
    assert issues == ("dimension 'town': column 'city_name' is not in table sales",)


def test_non_numeric_fact_refused() -> None:
    d = data()
    d["facts"][0]["column"] = "town"
    issues = check_columns(SemanticModel.model_validate(d), "sales", COLUMNS)
    assert "fact 'price': column 'town' is not numeric on clickhouse" in issues
    assert "fact 'price': column 'town' is not numeric on snowflake" in issues


def test_time_dimension_on_text_column_refused() -> None:
    d = data()
    d["dimensions"][2]["column"] = "town"
    issues = check_columns(SemanticModel.model_validate(d), "sales", COLUMNS)
    assert "dimension 'year': column 'town' is not a date on clickhouse" in issues


def test_plain_time_dimension_on_text_column_refused() -> None:
    """A time dimension with no transform must sit on a date column too (N1.4 verifier)."""
    d = data()
    d["dimensions"][1]["column"] = "town"  # `date`: kind time, no transform
    issues = check_columns(SemanticModel.model_validate(d), "sales", COLUMNS)
    assert "dimension 'date': column 'town' is not a date on clickhouse" in issues
    assert "dimension 'date': column 'town' is not a date on snowflake" in issues


def test_time_dimension_on_a_datetime_column_refused() -> None:
    columns = {**COLUMNS, "date": {"clickhouse": "DateTime", "snowflake": "DATETIME"}}
    issues = check_columns(mini(), "sales", columns)
    for name in ("date", "year", "month"):
        for engine in ("clickhouse", "snowflake"):
            assert f"dimension {name!r}: column 'date' is not a date on {engine}" in issues
    date32 = {**COLUMNS, "date": {"clickhouse": "Date32", "snowflake": "DATE"}}
    assert check_columns(mini(), "sales", date32) == ()


def test_wrong_table_refused() -> None:
    issues = check_columns(mini(), "other", COLUMNS)
    assert issues[0] == "table 'sales' is not the pack's table 'other'"


def test_load_semantic_refuses_unknown_column(pack_copy: Path) -> None:
    p = pack_copy / "semantic.yaml"
    p.write_text(
        p.read_text(encoding="utf-8").replace("column: town", "column: post_town"),
        encoding="utf-8",
    )
    with pytest.raises(SemanticError, match="column 'post_town' is not in table uk_price_paid"):
        load_semantic(load_pack(pack_copy))


def test_load_semantic_refuses_a_column_missing_on_one_engine(pack_copy: Path) -> None:
    p = pack_copy / "load" / "snowflake.sql"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("    county VARCHAR\n);", "    shire VARCHAR\n);"), encoding="utf-8")
    with pytest.raises(SemanticError, match="engines disagree on the columns of uk_price_paid"):
        load_semantic(load_pack(pack_copy))


def test_load_semantic_refuses_missing_table(pack_copy: Path) -> None:
    p = pack_copy / "load" / "clickhouse.sql"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace(".uk_price_paid\n(", ".uk_price_paidx\n("), encoding="utf-8")
    with pytest.raises(SemanticError, match="no CREATE TABLE for uk_price_paid in load/clickhouse"):
        load_semantic(load_pack(pack_copy))


def test_load_semantic_refuses_duplicate_keys_and_non_mapping(pack_copy: Path) -> None:
    p = pack_copy / "semantic.yaml"
    p.write_text("name: a\nname: b\n", encoding="utf-8")
    with pytest.raises(SemanticError, match=r"semantic\.yaml.*duplicate key"):
        load_semantic(load_pack(pack_copy))
    p.write_text("- a\n", encoding="utf-8")
    with pytest.raises(SemanticError, match="top level must be a mapping"):
        load_semantic(load_pack(pack_copy))
    p.write_text("a: [\n", encoding="utf-8")
    with pytest.raises(SemanticError, match="invalid YAML"):
        load_semantic(load_pack(pack_copy))


def test_load_semantic_names_validation_errors(pack_copy: Path) -> None:
    p = pack_copy / "semantic.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\nbogus: 1\n", encoding="utf-8")
    with pytest.raises(SemanticError, match=r"(?s)semantic\.yaml.*bogus"):
        load_semantic(load_pack(pack_copy))


def test_parse_semantic_round_trip() -> None:
    import yaml

    text = yaml.safe_dump(BASE, sort_keys=False)
    assert parse_semantic(text) == mini()
