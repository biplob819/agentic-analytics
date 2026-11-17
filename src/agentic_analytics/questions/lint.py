"""Writing-rule lint for gold SQL, quota check and `pack check` (QST-02, QST-03, QST-08).

Every rule works offline: sqlglot parses each engine's SQL in its own dialect, so no warehouse
account is needed. The rules (Tech Spec §5 step 2, plus what cross-engine agreement needs):

- `parse`: each engine's SQL parses in that engine's dialect (after `'{as_of}'` is bound to a
  sample date).
- `guard`: each engine's SQL passes the executor's SQL guard (`warehouse.guard.check_sql`).
- `approximate_function`: no approximate or non-deterministic aggregate (ClickHouse `median`,
  `quantile`, `uniq`, `any`, `topK`, ...; Snowflake `APPROX_*`, `HLL`, `ANY_VALUE`), including
  ClickHouse combinator forms such as `medianIf` (H1).
- `interpolated_quantile`: on ClickHouse, medians and percentiles use `quantileExactInclusive(p)`.
  `medianExact` / `quantileExact` return one middle element (the upper one for an even count),
  while Snowflake's `MEDIAN` and `PERCENTILE_CONT` interpolate, so they disagree on every even-sized
  group. For the same reason Snowflake `PERCENTILE_DISC` is refused.
- `enum_min_max`: no `min`/`max` over an enum column (ClickHouse orders `Enum8` by value,
  Snowflake alphabetically). Enum columns come from the pack's ClickHouse DDL.
- `enum_order`: on ClickHouse, no `ORDER BY` directly on an enum column; use `toString(col)`.
- `literal_case`: string literals compared with `town`, `district`, `county`, `locality` or
  `street` are uppercase, as stored.
- `as_of_placeholder`: the only placeholder is `'{as_of}'`, quoted, and both engines use it or
  neither does (D-10).
- `top_n_limit`: a top-N question's outermost query ends in `LIMIT n` with n equal to `top_n`.
- `top_n_order`: every query level with a LIMIT (subqueries included, and a top-N question's
  outermost query) has an `ORDER BY` with a tie-break: at least two keys, the last a plain column
  (GLD tie check, Tech Spec §5 step 5.3). A top-N question may not use OFFSET (`top_n_limit`).
- `limit_without_top_n`: an answer question outside `top_n` has no LIMIT (the tie check would not
  run on it).
- `snowflake_division`: on Snowflake, every `/` has a `CAST(... AS DOUBLE)` operand. Snowflake
  divides NUMBERs to a fixed scale, ClickHouse in Float64, so a bare division differs between the
  engines by more than the exact-match tolerance.
- `output_columns`: both engines return the same column names in the same order.
- `unknown_table`, `unknown_column`: SQL reads only the pack's tables (questions) or the pack's
  tables plus its probes tables (probes), and only their columns or names the query defines.
- `duplicate`: no two questions share text or gold SQL.
- Tolerance coverage (D-43, D-51), per output column, following aliases through CTEs and
  subqueries: `median_tolerance` (computed with `quantileExactInclusive`, `MEDIAN` or
  `PERCENTILE_CONT`), `average_tolerance` (`avg`/`AVG`) and `ratio_tolerance` (any division,
  `* 100`, or a name with pct/percent/share/ratio/rate/points) columns must be in
  `tolerance.columns` with `rel` >= 0.005; ratio columns also need `abs` >= 0.01 in percent units
  or >= 0.0001 as a fraction, and the question text must state the unit ("percent", or "between
  0 and 1"). `exact_columns`: a covered column must be one of those (counts, sums, min/max and
  plain values stay exact) and must exist.
- `arg_extreme`: no `argMax`/`argMin` (Snowflake `MAX_BY`/`MIN_BY`): they pick an arbitrary row
  on ties.
- `invented_label`: answer gold returns no `CASE`/`IF` column of invented string labels; it
  returns one numeric column per group, or stored values verbatim.
- `literal_case` also covers case-sensitive `LIKE` patterns.

`check_pack(pack)` is the core of `agentic-analytics pack check` (QST-08): it validates the
manifest, both JSONL files, every lint, the probes' markers against the planted rows, an optional
`semantic.yaml` (parsed only; its schema belongs to N1.4) and the category quotas.
"""

from __future__ import annotations

import itertools
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.tokenizer_core import TokenType

from agentic_analytics.packs import Pack, PackError, load_pack, pack_file
from agentic_analytics.questions.bank import (
    AS_OF_PLACEHOLDER,
    PROBES_FILE,
    QUESTIONS_FILE,
    BankError,
    load_probes,
    load_questions,
    placeholder_problem,
    uses_as_of,
)
from agentic_analytics.schemas import ENGINES, DatasetPack, Engine, Probe, Question
from agentic_analytics.schemas.pack import CATEGORIES
from agentic_analytics.warehouse.guard import check_sql

SAMPLE_AS_OF = date(2026, 9, 30)  # any month end; only used to make the SQL parse

RULES: tuple[str, ...] = (
    "parse",
    "guard",
    "approximate_function",
    "interpolated_quantile",
    "enum_min_max",
    "enum_order",
    "literal_case",
    "as_of_placeholder",
    "top_n_limit",
    "top_n_order",
    "limit_without_top_n",
    "snowflake_division",
    "output_columns",
    "unknown_table",
    "unknown_column",
    "duplicate",
    "median_tolerance",
    "average_tolerance",
    "ratio_tolerance",
    "exact_columns",
    "arg_extreme",
    "invented_label",
)
RATIO_REL = 0.005  # FR7, D-43, D-51
PERCENT_ABS = 0.01  # D-51: a hundredth of a percentage point
FRACTION_ABS = 0.0001  # D-51: the same, for a share between 0 and 1
AVG_FUNCTIONS: dict[str, frozenset[str]] = {
    "clickhouse": frozenset({"avg", "avgif"}),
    "snowflake": frozenset({"avg"}),
}
EXACT_AGGREGATES: dict[str, frozenset[str]] = {
    "clickhouse": frozenset(
        {"count", "countif", "sum", "sumif", "uniqexact", "uniqexactif", "min", "max"}
    ),
    "snowflake": frozenset({"count", "count_if", "sum", "min", "max"}),
}
ARG_EXTREME: dict[str, frozenset[str]] = {
    "clickhouse": frozenset({"argmax", "argmin"}),
    "snowflake": frozenset({"max_by", "min_by", "arg_max", "arg_min"}),
}
_RATIO_NAME = re.compile(r"(^|_)(pct|percent|share|ratio|rate|points)(_|$)")
_PERCENT_NAME = re.compile(r"(^|_)(pct|percent|points)(_|$)")
# Functions whose result is a median or percentile (D-43), by engine, casefolded.
MEDIAN_FUNCTIONS: dict[str, frozenset[str]] = {
    "clickhouse": frozenset({"quantileexactinclusive", "quantileexactinclusiveif"}),
    "snowflake": frozenset({"median", "percentile_cont"}),
}

# Approximate or non-deterministic aggregates, by base name (ClickHouse combinators stripped).
CLICKHOUSE_APPROX = frozenset(
    {
        "median",
        "quantile",
        "quantiles",
        "mediandeterministic",
        "quantiledeterministic",
        "quantilesdeterministic",
        "mediantiming",
        "quantiletiming",
        "quantilestiming",
        "mediantimingweighted",
        "quantiletimingweighted",
        "quantilestimingweighted",
        "mediantdigest",
        "quantiletdigest",
        "quantilestdigest",
        "mediantdigestweighted",
        "quantiletdigestweighted",
        "quantilestdigestweighted",
        "medianbfloat16",
        "quantilebfloat16",
        "quantilesbfloat16",
        "medianbfloat16weighted",
        "quantilebfloat16weighted",
        "quantilesbfloat16weighted",
        "mediandd",
        "quantiledd",
        "quantilesdd",
        "quantileinterpolatedweighted",
        "quantilesinterpolatedweighted",
        "uniq",
        "uniqcombined",
        "uniqcombined64",
        "uniqhll12",
        "uniqtheta",
        "topk",
        "topkweighted",
        "approx_top_k",
        "approx_top_count",
        "approx_top_sum",
        "any",
        "anylast",
        "any_value",
        "anyheavy",
        "first_value",
        "last_value",
        "grouparraysample",
        "grouparraylast",
        "grouparray",
        # time and randomness: the result depends on when or how often it runs (D-10: '{as_of}')
        "today",
        "yesterday",
        "now",
        "now64",
        "nowinblock",
        "rand",
        "rand32",
        "rand64",
        "randconstant",
        "generateuuidv4",
    }
)
SNOWFLAKE_APPROX = frozenset(
    {
        "current_date",
        "current_timestamp",
        "current_time",
        "localtimestamp",
        "localtime",
        "sysdate",
        "getdate",
        "systimestamp",
        "uuid_string",
        "seq1",
        "seq2",
        "seq4",
        "seq8",
        "mode",
        "approx_count_distinct",
        "approx_percentile",
        "approx_percentile_accumulate",
        "approx_percentile_combine",
        "approx_percentile_estimate",
        "approx_top_k",
        "approx_top_k_accumulate",
        "approx_top_k_combine",
        "approx_top_k_estimate",
        "approximate_similarity",
        "approximate_jaccard_index",
        "hll",
        "hll_accumulate",
        "hll_combine",
        "hll_estimate",
        "any_value",
        "minhash",
        "sample",
        "random",
        "uniform",
    }
)
# Exact but non-interpolating: disagree with Snowflake's MEDIAN / PERCENTILE_CONT.
CLICKHOUSE_MIDDLE_ELEMENT = frozenset(
    {
        "medianexact",
        "quantileexact",
        "quantilesexact",
        "medianexacthigh",
        "quantileexacthigh",
        "quantilesexacthigh",
        "medianexactlow",
        "quantileexactlow",
        "quantilesexactlow",
        "medianexactweighted",
        "quantileexactweighted",
        "quantilesexactweighted",
        "quantileexactexclusive",
        "quantilesexactexclusive",
    }
)
SNOWFLAKE_MIDDLE_ELEMENT = frozenset({"percentile_disc"})
# ClickHouse aggregate-function combinators, longest first so "OrDefault" wins over "Or...".
_COMBINATORS = (
    "simplestate",
    "ordefault",
    "resample",
    "distinct",
    "foreach",
    "argmin",
    "argmax",
    "ornull",
    "array",
    "state",
    "merge",
    "map",
    "if",
)
_MIN_MAX = frozenset({"min", "max", "minif", "maxif", "minornull", "maxornull"})
_FLOAT_TYPES = frozenset({exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT})
_ENUM_TYPES = frozenset(
    {
        exp.DataType.Type.ENUM,
        exp.DataType.Type.ENUM8,
        exp.DataType.Type.ENUM16,
    }
)
UPPERCASE_COLUMNS = frozenset({"town", "district", "county", "locality", "street"})
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class LintIssue:
    item_id: str
    rule: str
    message: str
    engine: Engine | None = None
    where: str = "gold_sql"

    def __str__(self) -> str:
        eng = f" [{self.engine}]" if self.engine else ""
        return f"{self.item_id} {self.where}{eng}: {self.rule}: {self.message}"


@dataclass(frozen=True)
class LintContext:
    """What the lint knows about a pack: its tables, columns and enum columns, per engine."""

    tables: Mapping[str, frozenset[str]]  # engine -> fully qualified names, casefolded
    probe_tables: Mapping[str, frozenset[str]] = field(default_factory=dict)
    columns: frozenset[str] | None = None  # casefolded; None skips `unknown_column`
    enum_columns: frozenset[str] = frozenset()
    uppercase_columns: frozenset[str] = UPPERCASE_COLUMNS


# ----------------------------------------------------------------------------- pack context


def _ddl_tables(text: str, engine: Engine) -> dict[str, tuple[list[str], set[str]]]:
    """CREATE TABLE statements in a pack SQL file: table name -> (columns, enum columns)."""
    from agentic_analytics.load._common import split_statements

    rendered = _PLACEHOLDER.sub(lambda m: f"x_{m.group(1)}", text)
    out: dict[str, tuple[list[str], set[str]]] = {}
    for stmt in split_statements(rendered):
        try:
            node = sqlglot.parse_one(stmt, read=engine, error_level=ErrorLevel.RAISE)
        except Exception:  # PUT, COPY and other statements sqlglot cannot read are irrelevant
            continue
        if not isinstance(node, exp.Create) or (node.kind or "").upper() != "TABLE":
            continue
        schema = node.this
        if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
            continue
        cols: list[str] = []
        enums: set[str] = set()
        for col in schema.expressions:
            if isinstance(col, exp.ColumnDef):
                cols.append(col.name.casefold())
                kind = col.args.get("kind")
                if isinstance(kind, exp.DataType) and kind.this in _ENUM_TYPES:
                    enums.add(col.name.casefold())
        out[schema.this.name.casefold()] = (cols, enums)
    return out


def _read_optional(pack: Pack, relpath: str) -> str | None:
    try:
        return pack_file(pack, relpath).read_text(encoding="utf-8")
    except (PackError, OSError, UnicodeDecodeError):
        return None


def probe_databases(manifest: DatasetPack) -> dict[str, str]:
    """Probes databases per engine (LOD-04): ClickHouse `<db>_probes`, Snowflake `<DB>_PROBES`."""
    out: dict[str, str] = {}
    for engine, db in manifest.databases.items():
        out[engine] = f"{db}_PROBES" if engine == "snowflake" else f"{db}_probes"
    return out


def context_for(pack: Pack) -> LintContext:
    """Tables from `pack.yaml`; columns and enum columns from the pack's load and probes DDL."""
    m = pack.manifest
    pack_tables = {t.casefold() for t in m.tables}
    tables = {e: frozenset(f"{m.databases[e]}.{t}".casefold() for t in m.tables) for e in m.engines}
    columns: set[str] = set()
    enums: set[str] = set()
    probe_names: set[str] = set()
    for engine in m.engines:
        for relpath, is_probe in ((f"load/{engine}.sql", False), (f"probes/{engine}.sql", True)):
            text = _read_optional(pack, relpath)
            if text is None:
                continue
            for name, (cols, enum_cols) in _ddl_tables(text, engine).items():
                if is_probe:
                    probe_names.add(name)
                elif name not in pack_tables:
                    continue
                columns.update(cols)
                enums.update(enum_cols)
    pdbs = probe_databases(m)
    probe_tables = {
        e: frozenset(f"{pdbs[e]}.{t}".casefold() for t in probe_names) for e in m.engines
    }
    return LintContext(
        tables=tables,
        probe_tables=probe_tables,
        columns=frozenset(columns) if columns else None,
        enum_columns=frozenset(enums),
    )


# ------------------------------------------------------------------------------ SQL helpers


def _base_name(name: str) -> str:
    """ClickHouse function name with aggregate combinators stripped: medianIf -> median."""
    n = name.casefold()
    changed = True
    while changed:
        changed = False
        for suffix in _COMBINATORS:
            if n.endswith(suffix) and len(n) > len(suffix) + 2:
                n = n[: -len(suffix)]
                changed = True
                break
    return n


def function_names(sql: str, engine: Engine) -> list[str]:
    """Names written immediately before an opening parenthesis, from the token stream (so the
    names are as written, not as sqlglot normalises them; string literals never match)."""
    try:
        tokens = sqlglot.tokenize(sql, read=engine)
    except Exception:
        return []
    names = []
    skip = {TokenType.L_PAREN, TokenType.R_PAREN, TokenType.COMMA, TokenType.STRING}
    for prev, cur in itertools.pairwise(tokens):
        if (
            cur.token_type == TokenType.L_PAREN
            and prev.token_type not in skip
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prev.text)
        ):
            names.append(prev.text)
    return names


def _unparen(node: exp.Expr | None) -> exp.Expr | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _column_name(node: exp.Expr | None) -> str | None:
    node = _unparen(node)
    return node.name.casefold() if isinstance(node, exp.Column) else None


def _is_float_cast(node: exp.Expr) -> bool:
    for c in node.find_all(exp.Cast):
        to = c.args.get("to")
        if isinstance(to, exp.DataType) and to.this in _FLOAT_TYPES:
            return True
    return False


def _cte_names(root: exp.Expr) -> set[str]:
    return {c.alias_or_name.casefold() for c in root.find_all(exp.CTE)}


def _table_name(t: exp.Table) -> str:
    parts = [p for p in (t.catalog, t.db, t.name) if p]
    return ".".join(parts).casefold()


def _defined_names(root: exp.Expr) -> set[str]:
    names: set[str] = set()
    for a in root.find_all(exp.Alias):
        names.add(a.alias.casefold())
    for ta in root.find_all(exp.TableAlias):
        if ta.name:
            names.add(ta.name.casefold())
        for col in ta.columns:
            names.add(col.name.casefold())
    return names


def _limit_value(node: exp.Expr) -> int | str | None:
    """The outermost LIMIT: None if absent, an int, or 'non-literal'."""
    limit = node.args.get("limit")
    if limit is None:
        return None
    expr = limit.expression if isinstance(limit, exp.Limit) else limit
    expr = _unparen(expr)
    if isinstance(expr, exp.Literal) and not expr.is_string:
        try:
            return int(expr.this)
        except ValueError:
            return "non-literal"
    return "non-literal"


def _output_names(root: exp.Expr) -> list[str] | None:
    if isinstance(root, exp.Query):
        try:
            return [n.casefold() for n in root.named_selects]
        except Exception:
            return None
    return None


# ------------------------------------------------------------------------------- the rules


def _lint_sql(
    sql: str,
    engine: Engine,
    ctx: LintContext,
    *,
    allowed_tables: frozenset[str],
    issue: Any,
) -> exp.Expr | None:
    """Single-engine rules. Returns the parsed tree (None if it did not parse)."""
    problem = placeholder_problem(sql)
    if problem:
        issue("as_of_placeholder", problem)
        return None
    bound = sql.replace(AS_OF_PLACEHOLDER, SAMPLE_AS_OF.isoformat())
    try:
        parsed = sqlglot.parse(bound, read=engine, error_level=ErrorLevel.RAISE)
    except Exception as exc:
        issue("parse", f"does not parse as {engine} SQL ({type(exc).__name__}: {exc})"[:300])
        return None
    statements = [p for p in parsed if p is not None]
    if len(statements) != 1:
        issue("parse", f"expected one statement, found {len(statements)}")
        return None
    root = statements[0]
    verdict = check_sql(bound, engine)
    if not verdict:
        issue("guard", f"refused by the SQL guard: {verdict.reason}")

    # keyword forms (Snowflake `CURRENT_DATE` with no parentheses) are not function tokens
    timed = SNOWFLAKE_APPROX | CLICKHOUSE_APPROX
    if not any(n.casefold() in timed for n in function_names(bound, engine)):
        for node in root.find_all(
            exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.CurrentDatetime
        ):
            issue(
                "approximate_function",
                f"{node.sql(dialect=engine)} depends on when the query runs; use '{{as_of}}'",
            )
            break
    names = function_names(bound, engine)
    for name in names:
        folded = name.casefold()
        if folded in ARG_EXTREME[engine] or (
            engine == "clickhouse" and _base_name(folded) in ARG_EXTREME[engine]
        ):
            issue(
                "arg_extreme",
                f"{name}() picks an arbitrary row when the extreme value is tied; rank with "
                "ORDER BY and a tie-break instead",
            )
            continue
        if engine == "clickhouse":
            base = _base_name(folded)
            if folded in CLICKHOUSE_APPROX or base in CLICKHOUSE_APPROX:
                issue("approximate_function", f"{name}() is approximate or non-deterministic")
            elif folded in CLICKHOUSE_MIDDLE_ELEMENT or base in CLICKHOUSE_MIDDLE_ELEMENT:
                issue(
                    "interpolated_quantile",
                    f"{name}() returns a middle element, Snowflake's MEDIAN/PERCENTILE_CONT "
                    "interpolate; use quantileExactInclusive(p)",
                )
        else:
            if folded in SNOWFLAKE_APPROX:
                issue("approximate_function", f"{name}() is approximate or non-deterministic")
            elif folded in SNOWFLAKE_MIDDLE_ELEMENT:
                issue(
                    "interpolated_quantile",
                    f"{name}() does not interpolate; ClickHouse gold uses quantileExactInclusive",
                )

    # enum_min_max
    for node in root.find_all(exp.Func):
        arg: exp.Expr | None = None
        if isinstance(node, exp.Min | exp.Max):
            arg = node.this
        elif isinstance(node, exp.Anonymous | exp.CombinedAggFunc | exp.AnonymousAggFunc):
            fname = (node.name or "").casefold()
            if fname in _MIN_MAX and node.expressions:
                arg = node.expressions[0]
        col = _column_name(arg)
        if col and col in ctx.enum_columns:
            issue("enum_min_max", f"min/max over the enum column {col!r} differs between engines")

    # enum_order (ClickHouse orders Enum8 by value)
    if engine == "clickhouse" and ctx.enum_columns:
        enum_aliases = {
            a.alias.casefold()
            for a in root.find_all(exp.Alias)
            if _column_name(a.this) in ctx.enum_columns
        }
        for o in root.find_all(exp.Ordered):
            col = _column_name(o.this)
            if col and (col in ctx.enum_columns or col in enum_aliases):
                issue(
                    "enum_order",
                    f"ORDER BY the enum column {col!r} sorts by enum value on ClickHouse; "
                    f"use toString({col})",
                )

    # literal_case
    for node in root.find_all(exp.EQ, exp.NEQ, exp.In):
        if isinstance(node, exp.In):
            col = _column_name(node.this)
            values = list(node.expressions)
        else:
            left, right = _unparen(node.left), _unparen(node.right)
            if isinstance(right, exp.Column):
                left, right = right, left
            col = _column_name(left)
            values = [right] if right is not None else []
        if col not in ctx.uppercase_columns:
            continue
        for v in values:
            if isinstance(v, exp.Literal) and v.is_string and v.this != v.this.upper():
                issue(
                    "literal_case",
                    f"{col} literal {v.this!r} must be uppercase as stored ({v.this.upper()!r})",
                )

    for like in root.find_all(exp.Like):  # LIKE is case-sensitive; ILIKE is not
        col = _column_name(like.this)
        pat = _unparen(like.expression)
        if (
            col in ctx.uppercase_columns
            and isinstance(pat, exp.Literal)
            and pat.is_string
            and pat.this != pat.this.upper()
        ):
            issue(
                "literal_case",
                f"{col} LIKE pattern {pat.this!r} must be uppercase as stored "
                f"({pat.this.upper()!r})",
            )

    # snowflake_division
    if engine == "snowflake":
        for d in root.find_all(exp.Div):
            if not (_is_float_cast(d.left) or _is_float_cast(d.right)):
                issue(
                    "snowflake_division",
                    f"division {d.sql(dialect='snowflake')[:80]!r} has no CAST(... AS DOUBLE) "
                    "operand; Snowflake would round to a fixed scale",
                )

    # unknown_table / unknown_column
    ctes = _cte_names(root)
    for t in root.find_all(exp.Table):
        if not t.name:
            continue
        name = _table_name(t)
        if not t.db and name in ctes:
            continue
        if name not in allowed_tables:
            issue("unknown_table", f"reads {name!r}, not one of {sorted(allowed_tables)}")
    if ctx.columns is not None:
        known = ctx.columns | _defined_names(root)
        unknown = sorted(
            {c.name.casefold() for c in root.find_all(exp.Column) if c.name} - known - {"*"}
        )
        for c in unknown:
            issue("unknown_column", f"column {c!r} is neither in the pack's tables nor defined")
    return root


def _lint_pair(
    item_id: str,
    sql_by_engine: Mapping[str, str],
    ctx: LintContext,
    *,
    where: str,
    allowed: Mapping[str, frozenset[str]],
    top_n: int | None,
    forbid_limit: bool,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    roots: dict[str, exp.Expr] = {}
    for engine in ENGINES:
        sql = sql_by_engine.get(engine)
        if sql is None:
            continue

        def issue(rule: str, message: str, _engine: Engine = engine) -> None:
            issues.append(LintIssue(item_id, rule, message, _engine, where))

        root = _lint_sql(
            sql, engine, ctx, allowed_tables=allowed.get(engine, frozenset()), issue=issue
        )
        if root is None:
            continue
        roots[engine] = root
        limit = _limit_value(root)
        if top_n is not None:
            if limit is None:
                issue("top_n_limit", f"top-N question has no outermost LIMIT {top_n}")
            elif limit != top_n:
                issue("top_n_limit", f"outermost LIMIT is {limit}, top_n is {top_n}")
        elif forbid_limit and any(True for _ in root.find_all(exp.Limit)):
            issue("limit_without_top_n", "LIMIT outside a top-N question (no tie check runs)")
        if top_n is not None and any(True for _ in root.find_all(exp.Offset)):
            issue("top_n_limit", "a top-N question may not skip rows with OFFSET")
        # every query level with a LIMIT (and a top-N question's outermost query) needs a tie-break
        limited: list[exp.Expr] = [
            s for s in root.find_all(exp.Select) if s.args.get("limit") is not None
        ]
        if (limit is not None or top_n is not None) and not any(s is root for s in limited):
            limited.insert(0, root)
        for sel in limited:
            order = sel.args.get("order")
            keys = list(order.expressions) if isinstance(order, exp.Order) else []
            level = "outermost" if sel is root else "subquery"
            if len(keys) < 2:
                issue(
                    "top_n_order",
                    f"a LIMIT needs an ORDER BY with a tie-break (two or more keys) ({level})",
                )
            else:
                last = _unparen(keys[-1].this)
                if not isinstance(last, exp.Column) and not (
                    engine == "clickhouse"
                    and isinstance(last, exp.Anonymous)
                    and last.name.casefold() == "tostring"
                ):
                    issue(
                        "top_n_order",
                        f"the last ORDER BY key must be a plain column (the tie-break) ({level})",
                    )

    present = [e for e in ENGINES if e in sql_by_engine]
    if len(present) == len(ENGINES):
        flags = {e: uses_as_of(sql_by_engine[e]) for e in present}
        if len(set(flags.values())) > 1:
            issues.append(
                LintIssue(
                    item_id,
                    "as_of_placeholder",
                    "only one engine uses '{as_of}': " + ", ".join(e for e in present if flags[e]),
                    None,
                    where,
                )
            )
    if len(roots) == len(ENGINES):
        names = {e: _output_names(r) for e, r in roots.items()}
        if None not in names.values() and len({tuple(v or ()) for v in names.values()}) > 1:
            issues.append(
                LintIssue(
                    item_id,
                    "output_columns",
                    "engines return different columns: "
                    + "; ".join(f"{e}={names[e]}" for e in ENGINES),
                    None,
                    where,
                )
            )
    return issues


def _uses_function(node: exp.Expr, engine: Engine, names: frozenset[str]) -> bool:
    return any(n.casefold() in names for n in function_names(node.sql(dialect=engine), engine))


def _is_times_100(node: exp.Expr) -> bool:
    for m in node.find_all(exp.Mul):
        for side in (m.left, m.right):
            side = _unparen(side)
            if isinstance(side, exp.Literal) and not side.is_string:
                try:
                    if float(side.this) == 100:
                        return True
                except ValueError:
                    pass
    return False


def column_kinds(sql: str, engine: Engine) -> dict[str, frozenset[str]]:
    """For each output column (casefolded name; unnamed columns by their SQL), what it is
    computed from, following aliases through CTEs and subqueries: `median`, `avg`, `ratio` (a
    division), `percent` (times 100, or named pct/percent/points), `count` (exact aggregates).
    Names like pct/share/ratio/rate also mark a ratio."""
    root = sqlglot.parse_one(sql.replace(AS_OF_PLACEHOLDER, SAMPLE_AS_OF.isoformat()), read=engine)
    defs: dict[str, list[exp.Expr]] = {}
    for a in root.find_all(exp.Alias):
        defs.setdefault(a.alias.casefold(), []).append(a.this)

    def features(expr: exp.Expr, visiting: frozenset[str]) -> set[str]:
        out: set[str] = set()
        fnames = {n.casefold() for n in function_names(expr.sql(dialect=engine), engine)}
        if fnames & MEDIAN_FUNCTIONS[engine]:
            out.add("median")
        if fnames & AVG_FUNCTIONS[engine]:
            out.add("avg")
        if fnames & EXACT_AGGREGATES[engine]:
            out.add("count")
        if any(True for _ in expr.find_all(exp.Div)):
            out.add("ratio")
        if _is_times_100(expr):
            out.add("percent")
        for col in expr.find_all(exp.Column):
            name = col.name.casefold()
            if name in defs and name not in visiting:
                for d in defs[name]:
                    if d is not expr:
                        out |= features(d, visiting | {name})
        return out

    if not isinstance(root, exp.Query):
        return {}
    result: dict[str, frozenset[str]] = {}
    for sel in root.selects:
        name = (sel.alias_or_name or sel.sql(dialect=engine)).casefold()
        inner = sel.this if isinstance(sel, exp.Alias) else sel
        kinds = features(inner, frozenset())
        if _RATIO_NAME.search(name):
            kinds.add("ratio")
        if _PERCENT_NAME.search(name):
            kinds.add("percent")
        result[name] = frozenset(kinds)
    return result


def median_columns(sql: str, engine: Engine) -> list[str]:
    """Gold columns computed from a median or percentile; D-43 puts them under tolerance."""
    return [n for n, k in column_kinds(sql, engine).items() if "median" in k]


def tolerant_columns(sql: str, engine: Engine) -> dict[str, str]:
    """Columns that need a tolerance and why: 'median', 'average', 'percent' or 'fraction'."""
    out: dict[str, str] = {}
    for name, kinds in column_kinds(sql, engine).items():
        if "percent" in kinds:
            out[name] = "percent"
        elif "ratio" in kinds:
            out[name] = "fraction"
        elif "median" in kinds:
            out[name] = "median"
        elif "avg" in kinds:
            out[name] = "average"
    return out


def suggest_tolerance(gold_sql: Mapping[str, str]) -> dict[str, Any]:
    """The tolerance D-43/D-51 require for this gold: rel 0.005 on every median, average and
    ratio column, abs 0.01 for percent units or 0.0001 for fractions; exact otherwise."""
    reasons: dict[str, str] = {}
    for engine in ENGINES:  # union: a column either engine computes inexactly is covered
        for col, why in tolerant_columns(gold_sql[engine], engine).items():
            reasons.setdefault(col, why)
    if not reasons:
        return {"rel": 1e-9, "abs": 1e-9, "columns": None}
    if "percent" in reasons.values():
        abs_ = PERCENT_ABS
    elif "fraction" in reasons.values():
        abs_ = FRACTION_ABS
    else:
        abs_ = 1e-9
    return {"rel": RATIO_REL, "abs": abs_, "columns": list(reasons)}


def _invented_labels(sql: str, engine: Engine) -> list[str]:
    root = sqlglot.parse_one(sql.replace(AS_OF_PLACEHOLDER, SAMPLE_AS_OF.isoformat()), read=engine)
    if not isinstance(root, exp.Query):
        return []
    out = []
    for sel in root.selects:
        inner = _unparen(sel.this if isinstance(sel, exp.Alias) else sel)
        results: list[Any] = []
        if isinstance(inner, exp.Case):
            results = [i.args.get("true") for i in inner.args.get("ifs") or []]
            results.append(inner.args.get("default"))
        elif isinstance(inner, exp.If):
            results = [inner.args.get("true"), inner.args.get("false")]
        if any(isinstance(r, exp.Literal) and r.is_string for r in results):
            out.append((sel.alias_or_name or sel.sql(dialect=engine)).casefold())
    return out


_UNIT_WORDS = {"percent": ("percent",), "fraction": ("between 0 and 1", "fraction")}


def _tolerance_issues(q: Question) -> list[LintIssue]:
    """D-43 / D-51: tolerated columns are exactly the median, average and ratio columns."""
    if q.gold_sql is None:
        return []
    covered = {c.casefold() for c in q.tolerance.columns or ()}
    text = q.text.casefold()
    issues: list[LintIssue] = []
    all_kinds: set[str] = set()
    all_reasons: set[str] = set()
    parsed = 0
    for engine in ENGINES:

        def issue(rule: str, message: str, _engine: Engine = engine) -> None:
            issues.append(LintIssue(q.id, rule, message, _engine))

        try:
            kinds = column_kinds(q.gold_sql[engine], engine)
            reasons = tolerant_columns(q.gold_sql[engine], engine)
            labels = _invented_labels(q.gold_sql[engine], engine)
        except Exception:  # unparseable SQL is already reported by `parse`
            continue
        parsed += 1
        for col in labels:
            issue(
                "invented_label",
                f"column {col!r} compares invented string labels; return one numeric column "
                "per group or the stored values",
            )
        rule_for = {
            "median": "median_tolerance",
            "average": "average_tolerance",
            "percent": "ratio_tolerance",
            "fraction": "ratio_tolerance",
        }
        for col, why in reasons.items():
            rule = rule_for[why]
            if col not in covered:
                issue(rule, f"{why} column {col!r} is not in tolerance.columns")
                continue
            if q.tolerance.rel < RATIO_REL:
                issue(rule, f"tolerance.rel {q.tolerance.rel} is below {RATIO_REL} for {col!r}")
            need_abs = {"percent": PERCENT_ABS, "fraction": FRACTION_ABS}.get(why)
            if need_abs is not None:
                if q.tolerance.abs < need_abs:
                    issue(rule, f"{why} column {col!r} needs tolerance.abs >= {need_abs}")
                if not any(w in text for w in _UNIT_WORDS[why]):
                    words = " or ".join(repr(w) for w in _UNIT_WORDS[why])
                    issue(rule, f"{why} column {col!r}: the text must state the unit ({words})")
        all_kinds |= set(kinds)
        all_reasons |= set(reasons)
    if not parsed:
        return issues
    for col in sorted(covered):  # judged over both engines: either one makes a column inexact
        if col not in all_kinds:
            issues.append(
                LintIssue(
                    q.id,
                    "exact_columns",
                    f"tolerance names {col!r}, which the gold does not return",
                )
            )
        elif col not in all_reasons:
            issues.append(
                LintIssue(
                    q.id,
                    "exact_columns",
                    f"{col!r} is an exact count, sum or value and must not be under tolerance",
                )
            )
    return issues


def lint_question(q: Question, ctx: LintContext) -> list[LintIssue]:
    """All single-question rules over gold_sql, or over each interpretation (QST-02, QST-03)."""
    if q.gold_sql is not None:
        return _lint_pair(
            q.id,
            q.gold_sql,
            ctx,
            where="gold_sql",
            allowed=ctx.tables,
            top_n=q.top_n,
            forbid_limit=q.category != "top_n",
        ) + _tolerance_issues(q)
    issues: list[LintIssue] = []
    for i, interp in enumerate(q.interpretations):
        issues += _lint_pair(
            q.id,
            interp,
            ctx,
            where=f"interpretations[{i}]",
            allowed=ctx.tables,
            top_n=None,
            forbid_limit=False,
        )
    return issues


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def lint_questions(questions: Sequence[Question], ctx: LintContext) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for q in questions:
        issues += lint_question(q, ctx)
    seen_text: dict[str, str] = {}
    seen_gold: dict[str, str] = {}
    for q in questions:
        t = _norm(q.text)
        if t in seen_text:
            issues.append(
                LintIssue(q.id, "duplicate", f"same text as {seen_text[t]}", None, "text")
            )
        seen_text.setdefault(t, q.id)
        if q.gold_sql is not None:
            g = "\x00".join(_norm(q.gold_sql[e]) for e in sorted(q.gold_sql))
            if g in seen_gold:
                issues.append(LintIssue(q.id, "duplicate", f"same gold SQL as {seen_gold[g]}"))
            seen_gold.setdefault(g, q.id)
    return issues


def lint_probe(p: Probe, ctx: LintContext) -> list[LintIssue]:
    """Probe gold may also read the probes tables (LOD-04); no top-N rules apply."""
    if p.gold_sql is None:
        return []
    allowed = {
        e: ctx.tables.get(e, frozenset()) | ctx.probe_tables.get(e, frozenset()) for e in ENGINES
    }
    return _lint_pair(
        p.id, p.gold_sql, ctx, where="gold_sql", allowed=allowed, top_n=None, forbid_limit=True
    )


def lint_probes(probes: Sequence[Probe], ctx: LintContext) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for p in probes:
        issues += lint_probe(p, ctx)
    return issues


# ------------------------------------------------------------------------- quotas and probes


def check_quotas(questions: Iterable[Question], manifest: DatasetPack) -> list[str]:
    """Each category must have at least its quota of questions (the draft has more; the gold
    check's seeded selection trims to the quota, Tech Spec §5 step 6)."""
    counts = Counter(q.category for q in questions)
    problems = []
    for cat in CATEGORIES:
        quota = manifest.quotas.get(cat)
        have = counts.get(cat, 0)
        if quota is None:
            if have:
                problems.append(f"quotas: {have} {cat} questions but no quota for {cat}")
            continue
        if have < quota:
            problems.append(f"quotas: {cat} has {have} questions, below its quota of {quota}")
    return problems


def check_probe_markers(probes: Iterable[Probe], pack: Pack) -> list[str]:
    """Each injection-in-data probe's marker must be planted in every engine's probes SQL."""
    problems = []
    texts = {e: _read_optional(pack, f"probes/{e}.sql") for e in pack.manifest.engines}
    for p in probes:
        if p.kind != "injection_data" or p.injection_marker is None:
            continue
        for engine, text in texts.items():
            if text is None:
                problems.append(f"{p.id}: no probes/{engine}.sql holds the planted rows")
            elif p.injection_marker not in text:
                problems.append(
                    f"{p.id}: marker {p.injection_marker!r} is not planted in probes/{engine}.sql"
                )
    return problems


# ------------------------------------------------------------------------------- pack check


@dataclass(frozen=True)
class PackReport:
    pack: str
    questions: int
    probes: int
    issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack": self.pack,
            "ok": self.ok,
            "questions": self.questions,
            "probes": self.probes,
            "issues": list(self.issues),
        }


def check_pack(pack: Pack | str | os.PathLike[str]) -> PackReport:
    """Validate a pack with no accounts (QST-08, Tech Spec §2.7)."""
    if not isinstance(pack, Pack):
        try:
            pack = load_pack(pack)
        except PackError as exc:
            return PackReport(str(pack), 0, 0, (f"pack.yaml: {exc}",))
    issues: list[str] = []
    ctx = context_for(pack)
    questions: tuple[Question, ...] = ()
    probes: tuple[Probe, ...] = ()
    try:
        questions = load_questions(pack)
    except (PackError, BankError) as exc:
        issues.append(f"{QUESTIONS_FILE}: {exc}")
    try:
        probes = load_probes(pack)
    except (PackError, BankError) as exc:
        issues.append(f"{PROBES_FILE}: {exc}")
    issues += [str(i) for i in lint_questions(questions, ctx)]
    issues += [str(i) for i in lint_probes(probes, ctx)]
    issues += check_probe_markers(probes, pack)
    if questions:
        issues += check_quotas(questions, pack.manifest)
    semantic = _read_optional(pack, "semantic.yaml")
    if semantic is not None:
        try:
            data = yaml.safe_load(semantic)
        except yaml.YAMLError as exc:
            issues.append(f"semantic.yaml: invalid YAML ({exc})")
        else:
            if not isinstance(data, dict):
                issues.append("semantic.yaml: top level must be a mapping")
    return PackReport(pack.ref, len(questions), len(probes), tuple(issues))


__all__ = [
    "RULES",
    "SAMPLE_AS_OF",
    "LintContext",
    "LintIssue",
    "PackReport",
    "check_pack",
    "check_probe_markers",
    "check_quotas",
    "column_kinds",
    "context_for",
    "function_names",
    "lint_probe",
    "lint_probes",
    "lint_question",
    "lint_questions",
    "median_columns",
    "probe_databases",
    "suggest_tolerance",
    "tolerant_columns",
]
