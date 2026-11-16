"""The semantic model: `packs/<pack>/semantic.yaml` (SEM-01, D-8).

One file per pack describes the benchmark table for both the Snowflake semantic view (SEM-02)
and the descriptions prompt block (SEM-03). `load_semantic(pack)` reads it with the
duplicate-key-refusing YAML loader, validates it into a frozen, strict `SemanticModel` and checks
every column against the `CREATE TABLE` in each of the pack's load SQL files.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from agentic_analytics.load._common import split_statements
from agentic_analytics.packs import Pack, PackError, read_pack_text
from agentic_analytics.schemas._base import Model
from agentic_analytics.schemas.config import _UniqueKeyLoader

SEMANTIC_FILE = "semantic.yaml"

# Lowercase SQL identifiers: safe unquoted in Snowflake DDL and in ClickHouse.
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
# One line of printable text with no edge spaces (no C0 or C1 control characters, so no
# newlines).
_LINE = r"^[^\x00-\x1f\x7f-\x9f\s](?:[^\x00-\x1f\x7f-\x9f]*[^\x00-\x1f\x7f-\x9f\s])?$"


def _one_line(v: str) -> str:
    """Refuse every character `str.splitlines` splits on (U+0085, U+2028, U+2029 included)."""
    if v.splitlines() != [v]:
        raise ValueError("text must be a single line")
    return v


Text = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2000, pattern=_LINE),
    AfterValidator(_one_line),
]
# Synonyms are written inside double quotes in the prompt block and joined with commas.
_SYNONYM = r"^[A-Za-z0-9](?:[A-Za-z0-9 '&/.\-]*[A-Za-z0-9.])?$"
Synonym = Annotated[
    str, StringConstraints(max_length=80, pattern=_SYNONYM), AfterValidator(_one_line)
]
SampleValue = Annotated[
    str,
    StringConstraints(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f-\x9f,'\"]+$"),
    AfterValidator(_one_line),
]
Synonyms = Annotated[tuple[Synonym, ...], Field(default=())]

DimensionKind = Literal["categorical", "time"]
Transform = Literal["year", "month"]
Agg = Literal["count", "sum", "avg", "median"]


class SemanticError(ValueError):
    """semantic.yaml could not be read, validated or matched to the pack's tables."""


class _Item(Model):
    name: Ident
    description: Text
    synonyms: Synonyms

    @field_validator("synonyms")
    @classmethod
    def _unique_synonyms(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for s in v:
            if s.casefold() in seen:
                raise ValueError(f"duplicate synonym {s!r}")
            seen.add(s.casefold())
        return v


class SemanticTable(Model):
    name: Ident
    description: Text
    synonyms: Synonyms


class Dimension(_Item):
    column: Ident
    kind: DimensionKind = "categorical"
    transform: Transform | None = None
    sample_values: tuple[SampleValue, ...] = ()

    @field_validator("sample_values")
    @classmethod
    def _unique_values(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate sample value")
        return v

    @model_validator(mode="after")
    def _transform_on_time(self) -> Dimension:
        if self.transform is not None and self.kind != "time":
            raise ValueError(f"dimension {self.name!r}: a transform needs kind: time")
        return self


class Fact(_Item):
    column: Ident


class Metric(_Item):
    agg: Agg
    fact: Ident


Item = Dimension | Fact | Metric


class SemanticModel(Model):
    """One pack's semantic source. Names are unique across dimensions, facts and metrics;
    synonyms (casefolded) are unique across the view, the table's included, and never a name."""

    schema_version: Literal["1.0"]
    name: Ident
    description: Text
    table: SemanticTable
    dimensions: Annotated[tuple[Dimension, ...], Field(min_length=1)]
    facts: tuple[Fact, ...] = ()
    metrics: tuple[Metric, ...] = ()

    @property
    def items(self) -> tuple[Item, ...]:
        return (*self.dimensions, *self.facts, *self.metrics)

    @model_validator(mode="after")
    def _cross_checks(self) -> SemanticModel:
        names: set[str] = set()
        for it in self.items:
            if it.name in names:
                raise ValueError(f"name {it.name!r} is used more than once")
            names.add(it.name)
        folded_names = {n.casefold(): n for n in names}
        seen: set[str] = set()
        for owner, synonyms in (
            (f"table {self.table.name!r}", self.table.synonyms),
            *((f"{_kind(it)} {it.name!r}", it.synonyms) for it in self.items),
        ):
            for s in synonyms:
                key = s.casefold()
                if key in folded_names:
                    raise ValueError(
                        f"synonym {s!r} is also the name {folded_names[key]!r} (on {owner})"
                    )
                if key in seen:
                    raise ValueError(f"synonym {s!r} is used more than once")
                seen.add(key)
        facts = {f.name for f in self.facts}
        for m in self.metrics:
            if m.fact not in facts:
                raise ValueError(f"metric {m.name!r} uses unknown fact {m.fact!r}")
        if not any(d.kind == "time" and d.transform is None for d in self.dimensions):
            raise ValueError("the model needs a time dimension without a transform")
        return self


def _kind(item: Item) -> str:
    if isinstance(item, Dimension):
        return "dimension"
    if isinstance(item, Fact):
        return "fact"
    return "metric"


def item_kind(item: Item) -> Literal["dimension", "fact", "metric"]:
    return _kind(item)  # type: ignore[return-value]


def comment_text(item: Item) -> str:
    """The one text shown for an item in both the view (`COMMENT`) and the prompt block:
    its description, then its sample values when it has any (Snowflake's DDL has no field for
    them, so they ride in the comment and both sides see them, C6)."""
    values = item.sample_values if isinstance(item, Dimension) else ()
    if not values:
        return item.description
    return f"{item.description} Sample values: {', '.join(values)}."


# ---- reading -------------------------------------------------------------------------------


def parse_semantic(text: str, where: str = SEMANTIC_FILE) -> SemanticModel:
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise SemanticError(f"{where}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SemanticError(f"{where}: top level must be a mapping")
    try:
        return SemanticModel.model_validate(data)
    except ValidationError as exc:
        raise SemanticError(f"{where}: {exc}") from exc


_CREATE_TABLE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\S+\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.I,
)


def _split_top(body: str) -> list[str]:
    """Split on commas at paren depth 0, outside single quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = False
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == "'":
                quote = False
            continue
        if ch == "'":
            quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _table_body(stmt: str, start: int) -> str:
    depth = 0
    quote = False
    for i in range(start, len(stmt)):
        ch = stmt[i]
        if quote:
            quote = ch != "'"
        elif ch == "'":
            quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return stmt[start + 1 : i]
    raise SemanticError("unbalanced parentheses in CREATE TABLE")


def columns_from_sql(sql: str, table: str) -> dict[str, str] | None:
    """Column name to type for `CREATE TABLE … <table> (…)` in one SQL file, or None."""
    for stmt in split_statements(sql):
        m = _CREATE_TABLE.match(stmt)
        if not m or m.group(1).lower() != table.lower():
            continue
        cols: dict[str, str] = {}
        for part in _split_top(_table_body(stmt, m.end() - 1)):
            name, _, typ = part.partition(" ")
            cols[name.lower()] = typ.strip()
        return cols
    return None


def pack_table_columns(pack: Pack, table: str) -> dict[str, dict[str, str]]:
    """Columns of `table` from every engine's `load/<engine>.sql`: name -> {engine: type}.
    The engines must list the same columns in the same order."""
    per_engine: dict[str, dict[str, str]] = {}
    for engine in pack.manifest.engines:
        rel = f"load/{engine}.sql"
        try:
            sql = read_pack_text(pack, rel)
        except PackError as exc:
            raise SemanticError(str(exc)) from exc
        cols = columns_from_sql(sql, table)
        if cols is None:
            raise SemanticError(f"no CREATE TABLE for {table} in {rel}")
        per_engine[engine] = cols
    orders = {tuple(c) for c in per_engine.values()}
    if len(orders) != 1:
        detail = "; ".join(f"{e}: {', '.join(c)}" for e, c in per_engine.items())
        raise SemanticError(f"engines disagree on the columns of {table} ({detail})")
    names = next(iter(orders))
    return {n: {e: per_engine[e][n] for e in per_engine} for n in names}


_NUMERIC = re.compile(
    r"^(U?Int\d+|Float\d+|Decimal\d*|NUMBER|NUMERIC|INT|INTEGER|BIGINT|FLOAT|DOUBLE)\b", re.I
)
_DATE = re.compile(r"^(Date|Date32|DATE)\b(?!Time)")


def check_columns(
    model: SemanticModel, table: str, columns: Mapping[str, Mapping[str, str]]
) -> tuple[str, ...]:
    """Problems with the model's columns against a table's columns (name -> {engine: type})."""
    issues: list[str] = []
    if model.table.name != table:
        issues.append(f"table {model.table.name!r} is not the pack's table {table!r}")
    for it in model.items:
        if isinstance(it, Metric):
            continue
        where = f"{item_kind(it)} {it.name!r}"
        types = columns.get(it.column)
        if types is None:
            issues.append(f"{where}: column {it.column!r} is not in table {table}")
            continue
        for engine, typ in types.items():
            if isinstance(it, Fact) and not _NUMERIC.match(typ):
                issues.append(f"{where}: column {it.column!r} is not numeric on {engine}")
            if (
                isinstance(it, Dimension)
                and (it.kind == "time" or it.transform)
                and not _DATE.match(typ)
            ):
                issues.append(f"{where}: column {it.column!r} is not a date on {engine}")
    return tuple(issues)


def load_semantic(pack: Pack) -> SemanticModel:
    """The pack's semantic.yaml, validated and checked against its load SQL."""
    try:
        text = read_pack_text(pack, SEMANTIC_FILE)
    except PackError as exc:
        raise SemanticError(str(exc)) from exc
    where = f"{pack.ref} {SEMANTIC_FILE}"
    model = parse_semantic(text, where)
    if model.table.name not in pack.manifest.tables:
        raise SemanticError(
            f"{where}: table {model.table.name!r} is not one of the pack's tables "
            f"({', '.join(pack.manifest.tables)})"
        )
    issues = check_columns(model, model.table.name, pack_table_columns(pack, model.table.name))
    if issues:
        raise SemanticError(f"{where}: " + "; ".join(issues))
    return model


__all__ = [
    "SEMANTIC_FILE",
    "Dimension",
    "Fact",
    "Item",
    "Metric",
    "SemanticError",
    "SemanticModel",
    "SemanticTable",
    "check_columns",
    "columns_from_sql",
    "comment_text",
    "item_kind",
    "load_semantic",
    "pack_table_columns",
    "parse_semantic",
]
