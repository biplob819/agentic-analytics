"""Information parity between the semantic view and the descriptions block (SEM-04, C6).

Both generated texts are parsed back, never the model, so a bug in either generator shows up as
a difference: every item must appear in both with the identical comment text, the same set of
synonyms, and the same source (the block's Column or Aggregation line against the DDL
expression).
"""

from __future__ import annotations

import re

from agentic_analytics.semantic.prompt import parse_descriptions_block
from agentic_analytics.semantic.snowflake import ParsedItem, parse_semantic_view_ddl


class SemanticParityError(ValueError):
    """The view and the prompt block do not carry the same descriptions and synonyms."""


_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_DDL_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"{_ID}\.({_ID})"), "column {0}"),
    (re.compile(rf"YEAR\({_ID}\.({_ID})\)", re.I), "year of column {0}"),
    (re.compile(rf"DATE_TRUNC\('MONTH', {_ID}\.({_ID})\)", re.I), "month of column {0}"),
    (re.compile(rf"(COUNT|SUM|AVG|MEDIAN)\({_ID}\.({_ID})\)", re.I), "{0} of {1}"),
)
_BLOCK_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"Column: ({_ID})"), "column {0}"),
    (re.compile(rf"Column: ({_ID}) \(calendar year\)"), "year of column {0}"),
    (re.compile(rf"Column: ({_ID}) \(first day of the month\)"), "month of column {0}"),
    (re.compile(rf"Aggregation: (count|sum|average|median) of ({_ID})"), "{0} of {1}"),
)
_AGG_CANON = {"average": "avg"}


def _source(text: str, forms: tuple[tuple[re.Pattern[str], str], ...]) -> str | None:
    """What an item is computed from, in one form for both sides (`year of column date`)."""
    for pattern, template in forms:
        if m := pattern.fullmatch(text):
            groups = [g.lower() for g in m.groups()]
            groups = [_AGG_CANON.get(g, g) for g in groups]
            return template.format(*groups)
    return None


def _compare(
    view: dict[tuple[str, str], ParsedItem], block: dict[tuple[str, str], ParsedItem]
) -> tuple[str, ...]:
    issues: list[str] = []
    for key in sorted(view.keys() | block.keys()):
        label = f"{key[0]} {key[1]}"
        if key not in block:
            issues.append(f"{label}: only in the view")
            continue
        if key not in view:
            issues.append(f"{label}: only in the prompt block")
            continue
        v, b = view[key], block[key]
        if v.comment != b.comment:
            issues.append(f"{label}: comment differs (view {v.comment!r}, block {b.comment!r})")
        if key[0] in ("dimension", "fact", "metric"):
            vsrc, bsrc = _source(v.expr, _DDL_FORMS), _source(b.expr, _BLOCK_FORMS)
            if vsrc is None or bsrc is None or vsrc != bsrc:
                issues.append(f"{label}: source differs (view {v.expr!r}, block {b.expr!r})")
        vs, bs = set(v.synonyms), set(b.synonyms)
        if vs != bs:
            only_v = ", ".join(sorted(vs - bs)) or "-"
            only_b = ", ".join(sorted(bs - vs)) or "-"
            issues.append(
                f"{label}: synonyms differ (only in the view: {only_v}; "
                f"only in the block: {only_b})"
            )
    return tuple(issues)


def semantic_parity(ddl: str, block: str) -> tuple[str, ...]:
    """Every difference in descriptions and synonyms between the DDL text and the block text."""
    return _compare(parse_semantic_view_ddl(ddl).items, parse_descriptions_block(block))


def require_semantic_parity(ddl: str, block: str) -> None:
    issues = semantic_parity(ddl, block)
    if issues:
        raise SemanticParityError("semantic parity failed: " + "; ".join(issues))


__all__ = ["SemanticParityError", "require_semantic_parity", "semantic_parity"]
