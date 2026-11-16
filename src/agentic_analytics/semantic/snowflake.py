"""The Snowflake semantic view, generated from the semantic model (SEM-02), and a reader for it.

`semantic_view_ddl` is the only place that knows Snowflake's `CREATE SEMANTIC VIEW` grammar
(docs.snowflake.com/en/sql-reference/sql/create-semantic-view)::

    CREATE OR REPLACE SEMANTIC VIEW <db.schema.view>
      TABLES ( <alias> AS <db.schema.table> [WITH SYNONYMS = ('..', ..)] [COMMENT = '..'] )
      [FACTS ( <alias>.<name> AS <expr> [WITH SYNONYMS = (..)] [COMMENT = '..'], .. )]
      [DIMENSIONS ( .. )]
      [METRICS ( <alias>.<name> AS <aggregate expr> .. )]
      [COMMENT = '..'];

The generated DDL for the bundled pack ran unchanged on the trial account at the N1.4 live step
(2026-11-16): `MEDIAN` in a metric, same-name facts and dimensions, the names `date`, `type`,
`year` and `month`, a 790-character view comment, and doubled quotes and backslashes (read back
exactly by `DESCRIBE SEMANTIC VIEW`) are all accepted. The reader
(`parse_semantic_view_ddl`) understands exactly what the generator writes and is what the parity
check (SEM-04) uses, so a generator bug cannot hide behind the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentic_analytics.packs import Pack
from agentic_analytics.semantic.model import Dimension, Fact, Metric, SemanticModel, comment_text

_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
_FQN = re.compile(rf"{_IDENT}\.{_IDENT}\.{_IDENT}")
_ROLE = re.compile(_IDENT)
SF_ROLE = "ANALYTICS_RO"
VIEW_SUFFIX = "_SEMANTIC"
VIEW_KEY = ("view", "overview")  # the view comment, the prompt block's "Overview"

_AGG_SQL = {"count": "COUNT", "sum": "SUM", "avg": "AVG", "median": "MEDIAN"}
_TRANSFORM_SQL = {"year": "YEAR({col})", "month": "DATE_TRUNC('MONTH', {col})"}


def _fqn(name: str, what: str) -> str:
    if not isinstance(name, str) or not _FQN.fullmatch(name):
        raise ValueError(f"{what} must be DATABASE.SCHEMA.NAME of plain identifiers, got {name!r}")
    return name


def sql_string(text: str) -> str:
    """A Snowflake single-quoted literal. Snowflake reads backslash escapes inside these, so
    backslashes are doubled as well as single quotes."""
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _entry(head: str, synonyms: tuple[str, ...], comment: str) -> str:
    lines = [f"    {head}"]
    if synonyms:
        lines.append(f"      WITH SYNONYMS = ({', '.join(sql_string(s) for s in synonyms)})")
    lines.append(f"      COMMENT = {sql_string(comment)}")
    return "\n".join(lines)


def _expr(model: SemanticModel, item: Dimension | Fact | Metric) -> str:
    alias = model.table.name
    if isinstance(item, Metric):  # metrics aggregate the fact, as the prompt block says
        return f"{_AGG_SQL[item.agg]}({alias}.{item.fact})"
    col = f"{alias}.{item.column}"
    if isinstance(item, Dimension) and item.transform is not None:
        return _TRANSFORM_SQL[item.transform].format(col=col)
    return col


def semantic_view_ddl(model: SemanticModel, view_name: str, table_fqn: str) -> str:
    """`CREATE OR REPLACE SEMANTIC VIEW` for the model over `table_fqn` (SEM-02)."""
    view_name = _fqn(view_name, "view_name")
    table_fqn = _fqn(table_fqn, "table_fqn")
    alias = model.table.name
    sections: list[str] = [
        "  TABLES (\n"
        + _entry(f"{alias} AS {table_fqn}", model.table.synonyms, model.table.description)
        + "\n  )"
    ]
    for keyword, items in (
        ("FACTS", model.facts),
        ("DIMENSIONS", model.dimensions),
        ("METRICS", model.metrics),
    ):
        if not items:
            continue
        entries = [
            _entry(f"{alias}.{it.name} AS {_expr(model, it)}", it.synonyms, comment_text(it))
            for it in items
        ]
        sections.append(f"  {keyword} (\n" + ",\n".join(entries) + "\n  )")
    sections.append(f"  COMMENT = {sql_string(model.description)}")
    return f"CREATE OR REPLACE SEMANTIC VIEW {view_name}\n" + "\n".join(sections) + ";\n"


def semantic_view_grant_sql(view_name: str, role: str = SF_ROLE) -> str:
    """The explicit per-view grant (the N1.2 role SQL also grants future semantic views)."""
    view_name = _fqn(view_name, "view_name")
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        raise ValueError(f"role {role!r} is not a plain identifier")
    return f"GRANT SELECT ON SEMANTIC VIEW {view_name} TO ROLE {role};"


def default_names(pack: Pack, model: SemanticModel) -> tuple[str, str]:
    """(view, table) for the pack's Snowflake `DATABASE.SCHEMA`: `<TABLE>_SEMANTIC`, `<TABLE>`."""
    target = pack.manifest.databases.get("snowflake")
    if target is None:
        raise ValueError(f"pack {pack.ref} has no Snowflake database")
    table = f"{target}.{model.table.name.upper()}"
    return _fqn(table + VIEW_SUFFIX, "view_name"), _fqn(table, "table_fqn")


# ---- reader --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedItem:
    """One commented thing in a semantic view: its expression (or table), comment, synonyms."""

    expr: str
    comment: str | None
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class ParsedView:
    name: str
    items: dict[tuple[str, str], ParsedItem]


_TOKEN = re.compile(r"\s+|'|[A-Za-z_][A-Za-z0-9_$]*|\d+|[(),.=;*]|.", re.S)
_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r", "0": "\0"}


@dataclass(frozen=True)
class _Tok:
    kind: str  # "str", "word", "punct"
    value: str
    raw: str
    start: int
    end: int


def _tokenize(sql: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(sql)
    while i < n:
        m = _TOKEN.match(sql, i)
        assert m is not None  # the last alternative matches any character
        text = m.group(0)
        if text.isspace():
            i = m.end()
            continue
        if text == "'":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ValueError("unterminated string literal in DDL")
                ch = sql[j]
                if ch == "\\":
                    if j + 1 >= n:
                        raise ValueError("unterminated string literal in DDL")
                    buf.append(_ESCAPES.get(sql[j + 1], sql[j + 1]))
                    j += 2
                elif ch == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    buf.append(ch)
                    j += 1
            toks.append(_Tok("str", "".join(buf), sql[i:j], i, j))
            i = j
            continue
        kind = "word" if (text[0].isalpha() or text[0] == "_") else "punct"
        toks.append(_Tok(kind, text, text, i, m.end()))
        i = m.end()
    return toks


class _Reader:
    def __init__(self, sql: str) -> None:
        self.sql, self.toks, self.i = sql, _tokenize(sql), 0

    def peek(self, k: int = 0) -> _Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def is_word(self, *words: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t is not None and t.kind == "word" and t.value.upper() in words

    def is_punct(self, p: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t is not None and t.kind == "punct" and t.value == p

    def take(self) -> _Tok:
        t = self.peek()
        if t is None:
            raise ValueError("unexpected end of DDL")
        self.i += 1
        return t

    def expect_word(self, *words: str) -> None:
        t = self.take()
        if t.kind != "word" or t.value.upper() not in words:
            raise ValueError(f"expected {' or '.join(words)} in DDL, got {t.raw!r}")

    def expect_punct(self, p: str) -> None:
        t = self.take()
        if t.kind != "punct" or t.value != p:
            raise ValueError(f"expected {p!r} in DDL, got {t.raw!r}")

    def string(self) -> str:
        t = self.take()
        if t.kind != "str":
            raise ValueError(f"expected a string literal in DDL, got {t.raw!r}")
        return t.value

    def dotted(self) -> str:
        parts = [self.take()]
        while self.is_punct("."):
            self.take()
            parts.append(self.take())
        if any(p.kind != "word" for p in parts):
            raise ValueError("expected a dotted name in DDL")
        return ".".join(p.value for p in parts)


def _clauses(r: _Reader) -> tuple[tuple[str, ...], str | None]:
    """WITH SYNONYMS [=] ('..', ..) and COMMENT = '..', in either order."""
    synonyms: tuple[str, ...] = ()
    comment: str | None = None
    while True:
        if r.is_word("WITH") and r.is_word("SYNONYMS", k=1):
            r.take()
            r.take()
            if r.is_punct("="):
                r.take()
            r.expect_punct("(")
            vals = [r.string()]
            while r.is_punct(","):
                r.take()
                vals.append(r.string())
            r.expect_punct(")")
            synonyms = tuple(vals)
        elif r.is_word("COMMENT"):
            r.take()
            if r.is_punct("="):
                r.take()
            comment = r.string()
        else:
            return synonyms, comment


def _entry_list(r: _Reader, kind: str, items: dict[tuple[str, str], ParsedItem]) -> None:
    r.expect_punct("(")
    while True:
        name = r.dotted()
        r.expect_word("AS")
        if kind == "table":
            expr = r.dotted()
            key = name
        else:
            _, _, key = name.rpartition(".")
            depth, toks = 0, []
            while True:
                t = r.peek()
                if t is None:
                    raise ValueError("unexpected end of DDL")
                if depth == 0 and (
                    (t.kind == "punct" and t.value in ",)")
                    or (r.is_word("WITH") and r.is_word("SYNONYMS", k=1))
                    or r.is_word("COMMENT")
                ):
                    break
                if t.kind == "punct" and t.value == "(":
                    depth += 1
                elif t.kind == "punct" and t.value == ")":
                    depth -= 1
                toks.append(r.take())
            if not toks:
                raise ValueError(f"{kind} {key}: empty expression in DDL")
            expr = r.sql[toks[0].start : toks[-1].end]
        synonyms, comment = _clauses(r)
        if (kind, key) in items:
            raise ValueError(f"{kind} {key} appears twice in DDL")
        items[(kind, key)] = ParsedItem(expr, comment, synonyms)
        if r.is_punct(","):
            r.take()
            continue
        r.expect_punct(")")
        return


_SECTIONS = {"FACTS": "fact", "DIMENSIONS": "dimension", "METRICS": "metric"}


def parse_semantic_view_ddl(ddl: str) -> ParsedView:
    """Read back a `CREATE [OR REPLACE] SEMANTIC VIEW` as written by `semantic_view_ddl`.

    Keys are `("view", "overview")` (the view comment), `("table", alias)` and
    `(kind, name)` for each fact, dimension and metric."""
    r = _Reader(ddl)
    r.expect_word("CREATE")
    if r.is_word("OR"):
        r.take()
        r.expect_word("REPLACE")
    r.expect_word("SEMANTIC")
    r.expect_word("VIEW")
    name = r.dotted()
    items: dict[tuple[str, str], ParsedItem] = {}
    r.expect_word("TABLES")
    _entry_list(r, "table", items)
    if r.is_word("RELATIONSHIPS"):
        raise ValueError("RELATIONSHIPS are not generated and not read")
    for keyword, kind in _SECTIONS.items():
        if r.is_word(keyword):
            r.take()
            _entry_list(r, kind, items)
    view_comment: str | None = None
    if r.is_word("COMMENT"):
        r.take()
        r.expect_punct("=")
        view_comment = r.string()
    if r.is_punct(";"):
        r.take()
    if r.peek() is not None:
        raise ValueError(f"unexpected {r.take().raw!r} at the end of the DDL")
    items[VIEW_KEY] = ParsedItem(name, view_comment, ())
    return ParsedView(name, items)


__all__ = [
    "SF_ROLE",
    "ParsedItem",
    "ParsedView",
    "default_names",
    "parse_semantic_view_ddl",
    "semantic_view_ddl",
    "semantic_view_grant_sql",
    "sql_string",
]
