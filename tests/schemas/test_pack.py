from datetime import date

import pytest
from pydantic import ValidationError

from tests.schemas.factories import pack, run_summary


def test_bundled_pack_shape_is_valid():
    p = pack()
    assert p.ref == "uk_price_paid@1.0"
    assert sum(p.quotas.values()) == 100


@pytest.mark.parametrize(
    "kw",
    [
        {"name": "UK-Price"},
        {"version": "v1"},
        {"engines": []},
        {"engines": ["clickhouse", "clickhouse"]},
        {"engines": ["duckdb"]},
        {"databases": {"clickhouse": "uk"}},  # snowflake engine without a database
        {"as_of_rule": "fixed"},  # fixed without a date
        {"as_of": date(2026, 9, 30)},  # date without the fixed rule
        {"quotas": {"vibes": 3}},
        {"quotas": {"time": 0}},
        {"tables": ["t", "t"]},
        {"tables": ["uk_price_paid", "UK_PRICE_PAID"]},
        {"source_url": "ftp://x"},
        {"source_url": 'https://"><script>'},
        {"version": "\u0661.\u0660"},
        {"engines": ["clickhouse", "clickhouse"], "databases": {"clickhouse": "uk"}},
        {"schema_version": "2.0"},
    ],
)
def test_pack_invariants(kw):
    with pytest.raises(ValidationError):
        pack(**kw)


def test_single_engine_pack():
    p = pack(engines=["clickhouse"], databases={"clickhouse": "uk"})
    assert p.engines == ("clickhouse",)


def test_run_summary_carries_pack_and_gold_mode():
    assert run_summary(gold_mode="single_engine").gold_mode == "single_engine"
    with pytest.raises(ValidationError):
        run_summary(gold_mode="vibes")
    with pytest.raises(ValidationError):
        run_summary(pack="uk_price_paid")  # no version
    with pytest.raises(ValidationError):
        run_summary(pack="uk_price_paid@\u0661.\u0660")
