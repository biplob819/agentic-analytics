"""SEM-03: the descriptions prompt block and `semantic_hash`."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentic_analytics.packs import load_pack, read_pack_text
from agentic_analytics.semantic import (
    SemanticModel,
    comment_text,
    descriptions_block,
    load_semantic,
    parse_descriptions_block,
    parse_semantic,
    semantic_hash,
)
from tests.semantic.helpers import BASE, data, mini

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("AA_UPDATE_GOLDEN") == "1"


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return load_semantic(load_pack("uk_price_paid"))


def test_bundled_block_equals_golden(model: SemanticModel) -> None:
    text = descriptions_block(model)
    path = GOLDEN / "uk_price_paid.prompt.txt"
    if UPDATE:  # pragma: no cover - maintainer helper
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def test_block_is_deterministic(model: SemanticModel) -> None:
    again = load_semantic(load_pack("uk_price_paid"))
    assert again is not model
    assert descriptions_block(model) == descriptions_block(again) == descriptions_block(model)


def test_block_mentions_everything(model: SemanticModel) -> None:
    block = descriptions_block(model)
    assert model.description in block
    assert model.table.description in block
    for syn in model.table.synonyms:
        assert f'"{syn}"' in block
    for item in (*model.dimensions, *model.facts, *model.metrics):
        assert re.search(rf"^- {item.name}$", block, re.M), item.name
        assert item.description in block
        assert comment_text(item) in block
        for syn in item.synonyms:
            assert f'"{syn}"' in block, syn
    for item in model.dimensions:
        for v in item.sample_values:
            assert v in block
    assert "Aggregation: median of price" in block
    assert "Column: date (calendar year)" in block


def test_block_reader_round_trip() -> None:
    m = mini()
    parsed = parse_descriptions_block(descriptions_block(m))
    assert parsed[("view", "overview")].comment == m.description
    assert parsed[("table", "sales")].synonyms == ("deals",)
    assert parsed[("dimension", "town")].synonyms == ("city", "buyer's town")
    assert parsed[("dimension", "town")].comment == comment_text(m.dimensions[0])
    assert parsed[("metric", "sales_count")].synonyms == ()


def test_block_reader_refuses_malformed() -> None:
    with pytest.raises(ValueError):
        parse_descriptions_block("nothing here\n")
    block = descriptions_block(mini())
    with pytest.raises(ValueError):
        parse_descriptions_block(block.replace("  Synonyms: ", "  Synonym: ", 1))


# ---- semantic_hash -------------------------------------------------------------------------


def test_hash_shape_and_stability(model: SemanticModel) -> None:
    h = semantic_hash(model)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert semantic_hash(load_semantic(load_pack("uk_price_paid"))) == h


def test_hash_ignores_yaml_layout_comments_and_explicit_defaults() -> None:
    base = semantic_hash(mini())
    # key order reversed at every level, comments added
    reordered = yaml.safe_dump(_reverse(BASE), sort_keys=False)
    assert semantic_hash(parse_semantic("# a comment\n" + reordered)) == base
    d = data()
    d["dimensions"][0]["kind"] = "categorical"  # the default, written out
    d["facts"][0]["synonyms"] = ["sale price"]
    assert semantic_hash(SemanticModel.model_validate(d)) == base


def test_hash_ignores_yaml_comments_in_the_bundled_file() -> None:
    pack = load_pack("uk_price_paid")
    text = read_pack_text(pack, "semantic.yaml")
    stripped = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert semantic_hash(parse_semantic(stripped)) == semantic_hash(load_semantic(pack))


def _reverse(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _reverse(v[k]) for k in reversed(list(v))}
    if isinstance(v, list):
        return [_reverse(x) for x in v]
    return v


def _set(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    d = data()
    target: Any = d
    for p in path[:-1]:
        target = target[p]
    target[path[-1]] = value
    return d


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("description",), "A tiny model, changed."),
        (("table", "description"), "One row per deal."),
        (("table", "synonyms"), ["deals", "trades"]),
        (("dimensions", 0, "description"), "Town, uppercase."),
        (("dimensions", 0, "synonyms"), ["city"]),
        (("dimensions", 0, "sample_values"), ["LONDON"]),
        (("dimensions", 0, "column"), "type"),
        (("facts", 0, "synonyms"), ["sale price", "cost"]),
        (("metrics", 0, "agg"), "avg"),
        (("metrics", 1, "name"), "n_sales"),
        (("dimensions", 2, "transform"), "month"),
    ],
)
def test_hash_changes_with_content(path: tuple[Any, ...], value: Any) -> None:
    changed = SemanticModel.model_validate(_set(path, value))
    assert semantic_hash(changed) != semantic_hash(mini())
