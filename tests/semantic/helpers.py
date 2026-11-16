"""Small semantic models for the N1.4 tests."""

from __future__ import annotations

import copy
from typing import Any

from agentic_analytics.semantic.model import SemanticModel

# Columns of a two-engine table, in the shape `pack_table_columns` returns.
COLUMNS: dict[str, dict[str, str]] = {
    "price": {"clickhouse": "UInt32", "snowflake": "NUMBER(10, 0)"},
    "date": {"clickhouse": "Date", "snowflake": "DATE"},
    "town": {"clickhouse": "LowCardinality(String)", "snowflake": "VARCHAR"},
    "type": {"clickhouse": "Enum8('flat' = 4)", "snowflake": "VARCHAR"},
}

BASE: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "mini",
    "description": "A tiny model. It's for tests; a back\\slash stays.",
    "table": {
        "name": "sales",
        "description": "One row per sale.",
        "synonyms": ["deals"],
    },
    "dimensions": [
        {
            "name": "town",
            "column": "town",
            "description": "Town in uppercase, e.g. 'LONDON'.",
            "synonyms": ["city", "buyer's town"],
            "sample_values": ["LONDON", "ST. IVES"],
        },
        {"name": "date", "column": "date", "kind": "time", "description": "Completion date."},
        {
            "name": "year",
            "column": "date",
            "kind": "time",
            "transform": "year",
            "description": "Calendar year.",
        },
        {
            "name": "month",
            "column": "date",
            "kind": "time",
            "transform": "month",
            "description": "First day of the month.",
        },
    ],
    "facts": [
        {
            "name": "price",
            "column": "price",
            "description": "Price in GBP.",
            "synonyms": ["sale price"],
        }
    ],
    "metrics": [
        {
            "name": "median_price",
            "agg": "median",
            "fact": "price",
            "description": "Median price, GBP.",
            "synonyms": ["middle price"],
        },
        {"name": "sales_count", "agg": "count", "fact": "price", "description": "Sales."},
    ],
}


def data(**changes: Any) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d.update(changes)
    return d


def mini(**changes: Any) -> SemanticModel:
    return SemanticModel.model_validate(data(**changes))
