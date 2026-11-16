"""The descriptions prompt block and `semantic_hash` (SEM-03, D-8, Tech Spec §7).

`descriptions_block(model)` is the exact text the `descriptions` fix puts in the MCP agents'
prompt. It carries the same comment text and synonyms as the Snowflake semantic view (SEM-04),
so the agents and Cortex Analyst get the same information (C6). It is a pure function of the
model. `semantic_hash(model)` is the sha256 of the canonical model and goes on every record.
"""

from __future__ import annotations

import hashlib
import json
import re

from agentic_analytics.semantic.model import (
    Dimension,
    Fact,
    Item,
    Metric,
    SemanticModel,
    comment_text,
)
from agentic_analytics.semantic.snowflake import VIEW_KEY, ParsedItem

TITLE = "SEMANTIC DESCRIPTIONS"
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("dimension", "Dimensions (filter or group by these):"),
    ("fact", "Facts (numbers on each row):"),
    ("metric", "Metrics (aggregates over the rows):"),
)
_AGG_WORDS = {"count": "count", "sum": "sum", "avg": "average", "median": "median"}
_TRANSFORM_WORDS = {"year": "calendar year", "month": "first day of the month"}


def _synonyms(values: tuple[str, ...]) -> str:
    return ", ".join(f'"{s}"' for s in values) if values else "none"


def _source(model: SemanticModel, item: Item) -> str:
    if isinstance(item, Metric):
        fact = next(f for f in model.facts if f.name == item.fact)
        return f"Aggregation: {_AGG_WORDS[item.agg]} of {fact.name}"
    if isinstance(item, Dimension) and item.transform is not None:
        return f"Column: {item.column} ({_TRANSFORM_WORDS[item.transform]})"
    return f"Column: {item.column}"


def descriptions_block(model: SemanticModel) -> str:
    """The `descriptions` fix text: overview, table, then every dimension, fact and metric with
    its column (or derivation or aggregation), comment text and synonyms, in source order."""
    lines = [
        TITLE,
        f"Generated from the {model.name} semantic model: what each column means, its synonyms "
        "and sample values.",
        "",
        f"Overview: {model.description}",
        "",
        f"Table: {model.table.name}",
        f"  Description: {model.table.description}",
        f"  Synonyms: {_synonyms(model.table.synonyms)}",
    ]
    groups: dict[str, tuple[Dimension, ...] | tuple[Fact, ...] | tuple[Metric, ...]] = {
        "dimension": model.dimensions,
        "fact": model.facts,
        "metric": model.metrics,
    }
    for kind, header in _SECTIONS:
        items = groups[kind]
        if not items:
            continue
        lines += ["", header]
        for it in items:
            lines += [
                "",
                f"- {it.name}",
                f"  {_source(model, it)}",
                f"  Description: {comment_text(it)}",
                f"  Synonyms: {_synonyms(it.synonyms)}",
            ]
    return "\n".join(lines) + "\n"


_SYNONYM_LIST = re.compile(r'"[^"]*"(?:, "[^"]*")*')
_ITEM = re.compile(r"- ([a-z][a-z0-9_]*)")
_FIELD = re.compile(r"  (Column|Aggregation|Description|Synonyms): (.*)")


def _parse_synonyms(text: str, where: str) -> tuple[str, ...]:
    if text == "none":
        return ()
    if not _SYNONYM_LIST.fullmatch(text):
        raise ValueError(f"{where}: bad synonyms line {text!r}")
    return tuple(re.findall(r'"([^"]*)"', text))


def parse_descriptions_block(block: str) -> dict[tuple[str, str], ParsedItem]:
    """Read back a block written by `descriptions_block`; keys as in the DDL reader."""
    lines = block.split("\n")
    if len(lines) < 2 or lines[0] != TITLE or not lines[1].startswith("Generated from "):
        raise ValueError("not a descriptions block (missing the title lines)")
    headers = {h: k for k, h in _SECTIONS}
    out: dict[tuple[str, str], ParsedItem] = {}
    current: tuple[str, str] | None = None
    fields: dict[str, str] = {}
    kind: str | None = None

    def close() -> None:
        nonlocal current, fields
        if current is None:
            return
        missing = {"Description", "Synonyms"} - set(fields)
        if missing:
            raise ValueError(f"{current[0]} {current[1]}: missing {', '.join(sorted(missing))}")
        if current in out:
            raise ValueError(f"{current[0]} {current[1]} appears twice in the block")
        where = f"{current[0]} {current[1]}"
        out[current] = ParsedItem(
            next((f"{k}: {fields[k]}" for k in ("Column", "Aggregation") if k in fields), ""),
            fields["Description"],
            _parse_synonyms(fields["Synonyms"], where),
        )
        current, fields = None, {}

    for n, line in enumerate(lines[2:], start=3):
        if not line:
            continue
        if line.startswith("Overview: "):
            close()
            if VIEW_KEY in out:
                raise ValueError("the overview appears twice in the block")
            out[VIEW_KEY] = ParsedItem("", line[len("Overview: ") :], ())
        elif line.startswith("Table: "):
            close()
            current, kind = ("table", line[len("Table: ") :]), None
        elif line in headers:
            close()
            kind = headers[line]
        elif m := _ITEM.fullmatch(line):
            close()
            if kind is None:
                raise ValueError(f"line {n}: item {m.group(1)!r} outside a section")
            current = (kind, m.group(1))
        elif (m := _FIELD.fullmatch(line)) and current is not None:
            if m.group(1) in fields:
                raise ValueError(f"line {n}: {m.group(1)} given twice")
            fields[m.group(1)] = m.group(2)
        else:
            raise ValueError(f"line {n}: unexpected {line!r}")
    close()
    if VIEW_KEY not in out:
        raise ValueError("the block has no overview")
    return out


def canonical_json(model: SemanticModel) -> str:
    """The model as compact JSON with sorted keys and every default filled in."""
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def semantic_hash(model: SemanticModel) -> str:
    """sha256 of the canonical model: blind to YAML layout, comments and explicit defaults."""
    return hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


__all__ = [
    "TITLE",
    "canonical_json",
    "descriptions_block",
    "parse_descriptions_block",
    "semantic_hash",
]
