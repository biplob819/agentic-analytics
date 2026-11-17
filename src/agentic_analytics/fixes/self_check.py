"""The `self-check` fix (FIX-04, Tech Spec §7, B4, E8): implausibility rules over the result.

`make_self_check(executor, rules)` returns the async hook A2.1's `ask_mcp(self_check=...)`
accepts: `await hook(sql)` runs the agent's final SQL once through the read-only executor (the
N1.2 guard runs first, so nothing but one SELECT can execute) and returns one feedback message
when a rule fires, or None when the result looks plausible. The agent loop sends that message at
most once, and only while MCP calls, a model turn and wall clock remain, so the six-call cap
holds (A2.1 counts the skipped checks).

Rules (`fixes/self_check.yaml` holds their thresholds, name patterns and message templates):

- `zero_rows`: the result has no rows, unless the question expects an empty result
  (`await hook(sql, expects_empty=True)`; the runner passes the question's flag).
- `zero_aggregate`: every numeric cell is 0 (a `count()` or `sum()` whose filter matched
  nothing, such as a lowercase town), unless the question expects an empty result.
- `null_aggregate`: a NULL in an aggregate column (an output whose expression aggregates).
- `price_out_of_range`: a column whose NAME denotes a price level (the `price` pattern: avg,
  mean, average or median together with price) and not a change or scaled value (the
  `price_exclude` pattern: change, diff, pct, ratio, a `_k` suffix ...) outside
  `price_range` (£1,000 to £10m; 1995 street-level medians can be a few thousand pounds).
- `count_above_rows`: a `count`/`countIf`/`uniq*` output above the table's row count.
- `date_out_of_range`: an ISO date cell outside the table's min..max date, or a year column
  (named like `year` or computed with `toYear`) holding a 4-digit year outside that range.
- `change_beyond_limit`: only a column explicitly named as a percentage change (the `change`
  pattern: `pct_change`, `yoy_pct`) beyond ±`max_change_pct` (500, percent units).
- `execution_failed`: the SQL failed with a SQL error or was refused by the guard. Transport
  failures and timeouts are not the model's fault and give no feedback.

The table's row count and date range come from `stats_sql`, run once per hook (lazily, through
the same executor). If that query fails, the two rules that need it are skipped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import threading
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

import sqlglot
from pydantic import Field, ValidationError, field_validator, model_validator
from sqlglot import exp

from agentic_analytics.fixes.examples import FixError, load_yaml
from agentic_analytics.packs import Pack, PackError, read_pack_text
from agentic_analytics.schemas import Execution, ResultSet
from agentic_analytics.schemas._base import Model, NonBlank, Versioned

SELF_CHECK_FILE = "fixes/self_check.yaml"
SELF_CHECK_ROW_CAP = 1_000
RULE_IDS: tuple[str, ...] = (
    "zero_rows",
    "zero_aggregate",
    "null_aggregate",
    "price_out_of_range",
    "count_above_rows",
    "date_out_of_range",
    "change_beyond_limit",
    "execution_failed",
)
# placeholders each rule's message template may use
_FIELDS: dict[str, frozenset[str]] = {
    "zero_rows": frozenset(),
    "zero_aggregate": frozenset(),
    "null_aggregate": frozenset({"column"}),
    "price_out_of_range": frozenset({"column", "value", "min", "max"}),
    "count_above_rows": frozenset({"column", "value", "rows"}),
    "date_out_of_range": frozenset({"column", "value", "min", "max"}),
    "change_beyond_limit": frozenset({"column", "value", "limit"}),
    "execution_failed": frozenset({"error"}),
}
_PLACEHOLDER = re.compile(r"\{([a-z_]*)\}")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_AGG_PREFIXES = (
    "count",
    "sum",
    "avg",
    "median",
    "quantile",
    "uniq",
    "min",
    "max",
    "any",
    "stddev",
    "var",
    "arg",
    "grouparray",
    "percentile",
)
_COUNT_PREFIXES = ("count", "uniq")
_WRAPPERS = frozenset(
    {"round", "floor", "ceil", "tofloat64", "tofloat32", "toint64", "touint64", "toint32"}
)


class Hook(Protocol):
    """`await hook(sql)` (A2.1's call) or `await hook(sql, expects_empty=True)`."""

    def __call__(self, sql: str, *, expects_empty: bool = False) -> Awaitable[str | None]: ...


class PriceRange(Model):
    min: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max: Annotated[float, Field(gt=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _ordered(self) -> PriceRange:
        if self.min >= self.max:
            raise ValueError("price_range.min must be below price_range.max")
        return self


class NamePatterns(Model):
    price: NonBlank
    price_exclude: NonBlank
    change: NonBlank
    year: NonBlank

    @field_validator("price", "price_exclude", "change", "year")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"bad regular expression {v!r}: {exc}") from exc
        return v


class SelfCheckRules(Versioned):
    engine: Literal["clickhouse"]
    stats_sql: NonBlank  # one row: row_count, min_date, max_date
    price_range: PriceRange
    max_change_pct: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    name_patterns: NamePatterns
    preamble: NonBlank
    closing: NonBlank
    messages: dict[str, NonBlank]

    @model_validator(mode="after")
    def _messages(self) -> SelfCheckRules:
        missing = [r for r in RULE_IDS if r not in self.messages]
        unknown = [r for r in self.messages if r not in RULE_IDS]
        if missing or unknown:
            raise ValueError(f"messages: missing {missing}, unknown {unknown}")
        for rule, text in self.messages.items():
            used = set(_PLACEHOLDER.findall(text))
            bad = used - _FIELDS[rule]
            if bad:
                raise ValueError(f"messages.{rule}: unknown placeholders {sorted(bad)}")
        return self


def parse_rules(text: str, where: str = SELF_CHECK_FILE) -> SelfCheckRules:
    data = load_yaml(text, where)
    try:
        return SelfCheckRules.model_validate(data)
    except ValidationError as exc:
        raise FixError(f"{where}: {exc}") from exc


def load_rules(pack: Pack) -> SelfCheckRules:
    try:
        text = read_pack_text(pack, SELF_CHECK_FILE)
    except PackError as exc:
        raise FixError(str(exc)) from exc
    return parse_rules(text, f"{pack.ref} {SELF_CHECK_FILE}")


# -- what each output column is -------------------------------------------------------------


@dataclass(frozen=True)
class ColumnKind:
    aggregate: bool = False
    count: bool = False
    year: bool = False


def _fname(node: exp.Expr) -> str:
    if isinstance(
        node, exp.Anonymous | exp.AnonymousAggFunc | exp.ParameterizedAgg | exp.CombinedAggFunc
    ):
        return (node.name or "").casefold()
    return node.key.casefold()


def _is_agg(node: exp.Expr) -> bool:
    if isinstance(node, exp.AggFunc):
        return True
    return isinstance(node, exp.Func) and _fname(node).startswith(_AGG_PREFIXES)


def _unwrap(node: exp.Expr) -> exp.Expr:
    while True:
        if isinstance(node, exp.Alias | exp.Paren | exp.Round | exp.Cast | exp.TryCast):
            inner = node.this
        elif isinstance(node, exp.Anonymous) and _fname(node) in _WRAPPERS and node.expressions:
            inner = node.expressions[0]
        else:
            return node
        if not isinstance(inner, exp.Expr):
            return node
        node = inner


def _kind(projection: exp.Expr) -> ColumnKind:
    head = _unwrap(projection)
    aggregate = any(_is_agg(n) for n in projection.find_all(exp.Func))
    name = _fname(head) if isinstance(head, exp.Func) else ""
    count = isinstance(head, exp.Count | exp.CountIf) or (
        isinstance(head, exp.Func) and name.startswith(_COUNT_PREFIXES)
    )
    year = isinstance(head, exp.Year) or name in ("toyear", "year")
    return ColumnKind(aggregate, count, year)


def column_kinds(sql: str, columns: tuple[str, ...]) -> list[ColumnKind]:
    """Per output column, what its expression is; plain kinds when the SQL cannot be read."""
    plain = [ColumnKind() for _ in columns]
    try:
        root = sqlglot.parse_one(sql, read="clickhouse")
    except Exception:
        return plain
    if not isinstance(root, exp.Query):
        return plain
    select = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select is None:
        return plain
    projections = list(select.expressions)
    if len(projections) != len(columns) or any(isinstance(p, exp.Star) for p in projections):
        return plain
    return [_kind(p) for p in projections]


# -- the rules ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TableStats:
    row_count: int
    min_date: dt.date
    max_date: dt.date


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _as_date(v: Any) -> dt.date | None:
    if not isinstance(v, str):
        return None
    m = _ISO_DATE.match(v)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v)


def price_level_column(name: str, rules: SelfCheckRules) -> bool:
    pats = rules.name_patterns
    return re.search(pats.price, name) is not None and re.search(pats.price_exclude, name) is None


def judge(
    sql: str,
    result: ResultSet,
    rules: SelfCheckRules,
    stats: TableStats | None,
    *,
    expects_empty: bool = False,
) -> list[tuple[str, dict[str, str]]]:
    """Every rule hit on one result: (rule id, message fields), at most one per rule."""
    hits: dict[str, dict[str, str]] = {}

    def hit(rule: str, **fields: Any) -> None:
        hits.setdefault(rule, {k: _fmt(v) for k, v in fields.items()})

    if not result.rows:
        if not expects_empty:
            hit("zero_rows")
        return list(hits.items())
    numbers = [n for row in result.rows for v in row if (n := _num(v)) is not None]
    if numbers and all(n == 0 for n in numbers) and not expects_empty:
        hit("zero_aggregate")
    kinds = column_kinds(sql, result.columns)
    pats = rules.name_patterns
    lo, hi = rules.price_range.min, rules.price_range.max
    for j, col in enumerate(result.columns):
        kind = kinds[j]
        price_level = price_level_column(col, rules)
        change_named = re.search(pats.change, col) is not None
        year_named = re.search(pats.year, col) is not None
        for row in result.rows:
            v = row[j]
            if v is None:
                if kind.aggregate:
                    hit("null_aggregate", column=col)
                continue
            n = _num(v)
            if n is not None:
                if price_level and not lo <= n <= hi:
                    hit("price_out_of_range", column=col, value=v, min=lo, max=hi)
                if kind.count and stats is not None and n > stats.row_count:
                    hit("count_above_rows", column=col, value=v, rows=stats.row_count)
                if change_named and abs(n) > rules.max_change_pct:
                    hit("change_beyond_limit", column=col, value=v, limit=rules.max_change_pct)
                if (
                    (kind.year or year_named)
                    and stats is not None
                    and float(n).is_integer()
                    and 1000 <= n <= 9999
                    and not stats.min_date.year <= n <= stats.max_date.year
                ):
                    lo_y, hi_y = stats.min_date.year, stats.max_date.year
                    hit("date_out_of_range", column=col, value=int(n), min=lo_y, max=hi_y)
            d = _as_date(v)
            if d is not None and stats is not None and not stats.min_date <= d <= stats.max_date:
                hit(
                    "date_out_of_range",
                    column=col,
                    value=v,
                    min=stats.min_date.isoformat(),
                    max=stats.max_date.isoformat(),
                )
    return list(hits.items())


def feedback(hits: list[tuple[str, dict[str, str]]], rules: SelfCheckRules) -> str | None:
    if not hits:
        return None
    order = {r: i for i, r in enumerate(RULE_IDS)}
    lines = [rules.preamble.strip()]
    for rule, fields in sorted(hits, key=lambda h: order[h[0]]):
        lines.append("- " + rules.messages[rule].strip().format(**fields))
    lines.append(rules.closing.strip())
    return "\n".join(lines)


def judge_execution(
    sql: str,
    execution: Execution,
    rules: SelfCheckRules,
    stats: TableStats | None,
    *,
    expects_empty: bool = False,
) -> str | None:
    """The feedback for one execution of the agent's SQL, or None."""
    if execution.error in ("sql_error", "guard"):
        return feedback([("execution_failed", {"error": execution.error})], rules)
    if execution.error is not None or execution.result is None:
        return None  # transport or timeout: not the model's fault
    hits = judge(sql, execution.result, rules, stats, expects_empty=expects_empty)
    return feedback(hits, rules)


def stats_from(execution: Execution) -> TableStats | None:
    r = execution.result
    if execution.error is not None or r is None or len(r.rows) != 1:
        return None
    row = dict(zip((c.casefold() for c in r.columns), r.rows[0], strict=True))
    count, lo, hi = (
        row.get("row_count"),
        _as_date(row.get("min_date")),
        _as_date(row.get("max_date")),
    )
    if isinstance(count, bool) or not isinstance(count, int) or lo is None or hi is None:
        return None
    return TableStats(count, lo, hi)


def make_self_check(
    executor: Any, rules: SelfCheckRules, *, row_cap: int = SELF_CHECK_ROW_CAP
) -> Hook:
    """The hook for `ask_mcp(self_check=...)`: `await hook(sql) -> feedback | None`; the runner
    binds the question's flag with `functools.partial(hook, expects_empty=q.expects_empty)`.

    The executor is N1.2's read-only executor (sync; run in a worker thread). Table stats are
    fetched on the first call that has a result and reused; a failed stats query skips the rules
    that need them for that call only and is retried on the next.
    """
    if getattr(executor, "engine", None) != rules.engine:
        raise FixError(f"the self-check needs a {rules.engine} executor")
    lock = threading.Lock()  # not an asyncio lock: the hook may outlive one event loop
    cache: dict[str, TableStats | None] = {}

    def stats() -> TableStats | None:
        with lock:
            if cache.get("stats") is None:  # a failed stats query is retried on the next call
                cache["stats"] = stats_from(executor.execute(rules.stats_sql, row_cap=1))
            return cache["stats"]

    async def hook(sql: str, *, expects_empty: bool = False) -> str | None:
        execution: Execution = await asyncio.to_thread(executor.execute, sql, row_cap=row_cap)
        # the stats only feed rules on a result: a failed query needs none
        st = await asyncio.to_thread(stats) if execution.error is None else None
        return judge_execution(sql, execution, rules, st, expects_empty=expects_empty)

    return hook


__all__ = [
    "RULE_IDS",
    "SELF_CHECK_FILE",
    "ColumnKind",
    "Hook",
    "SelfCheckRules",
    "TableStats",
    "column_kinds",
    "feedback",
    "judge",
    "judge_execution",
    "load_rules",
    "make_self_check",
    "parse_rules",
    "price_level_column",
    "stats_from",
]
