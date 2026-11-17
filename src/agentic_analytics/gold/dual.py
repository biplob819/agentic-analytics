"""Dual authorship helper (QST-04, D-9, Tech Spec §5 step 3, C3).

GPT Sol sees only the question text, the table DDL and the as-of line (the agents' exact wording,
so "last year" means the last full calendar year before the as-of date), with no tools, and writes
ClickHouse SQL. `gpt_sol_sql(question_text, ddl=..., as_of=..., model=...)` sends that one turn
through an N1.6 adapter (`make_gpt_sol()` builds the real one with `models.make_agent`) and
returns the SQL it wrote. `compare_dual(question, gpt_sql, executor, as_of=...)` runs both
versions on ClickHouse and compares them with S2.1's matcher at the question's tolerance;
`dual_authorship(...)` does that for every answer-expected question and returns the rows of the
disagreement report the owner reviews (QST-05). Nothing here edits the bank.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from agentic_analytics.gold.check import GOLD_ROW_CAP, cross_engine_mismatch
from agentic_analytics.load._common import split_statements
from agentic_analytics.models.base import Effort
from agentic_analytics.packs import Pack, read_pack_text
from agentic_analytics.questions import bind_as_of
from agentic_analytics.schemas import Execution, Question, ResultSet

GPT_SOL_MODEL = "gpt-6.1-sol"
SYSTEM = (
    "You write one ClickHouse SELECT statement that answers the user's question about the "
    "table below. Use exact functions only: quantileExactInclusive(p) for medians and "
    "percentiles, uniqExact for distinct counts, never median, quantile or uniq. Treat the "
    "as-of date as today. Reply with the SQL only, in one ```sql block, and nothing else."
)
_FENCE = re.compile(r"```(?:sql|SQL)?\s*\n?(.*?)```", re.S)


class DualAuthorError(RuntimeError):
    """The model returned no SQL, or refused."""


def table_ddl(pack: Pack, engine: str = "clickhouse") -> str:
    """The pack's CREATE TABLE statements for `engine`, with the database filled in."""
    db = pack.manifest.databases[engine]  # type: ignore[index]
    text = read_pack_text(pack, f"load/{engine}.sql")
    stmts = [s for s in split_statements(text) if re.match(r"(?is)^\s*CREATE\s+TABLE", s)]
    return ";\n\n".join(s.replace("{database}", db).strip() for s in stmts) + ";"


def user_prompt(question_text: str, ddl: str, as_of: date) -> str:
    """The DDL, the agents' own as-of line (A2.1 `as_of_line`: the date and what "last year",
    "this year" and "the last 12 months" mean relative to it, D-10) and the question."""
    from agentic_analytics.backends.prompts import as_of_line

    return f"Table DDL:\n{ddl}\n\n{as_of_line(as_of)}\n\nQuestion: {question_text}"


def extract_sql(text: str) -> str | None:
    m = _FENCE.search(text)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    return sql or None


@dataclass(frozen=True)
class DualDraft:
    sql: str
    model_snapshot: str
    cost_usd: float


async def gpt_sol_sql(question_text: str, *, ddl: str, as_of: date, model: Any) -> DualDraft:
    """One tool-free turn: the question text, DDL and as-of line in; ClickHouse SQL out."""
    conv = model.start(SYSTEM, user_prompt(question_text, ddl, as_of), ())
    turn = await model.step(conv)
    if turn.stop == "refusal":
        raise DualAuthorError(f"the model refused ({turn.refusal_category})")
    sql = extract_sql(turn.text or "")
    if sql is None:
        raise DualAuthorError("the model returned no SQL")
    return DualDraft(sql, turn.model_snapshot, float(turn.cost_usd))


def make_gpt_sol(
    prices: Any = None, *, model: str = GPT_SOL_MODEL, effort: Effort = "medium", client: Any = None
) -> Any:
    """The real GPT Sol adapter (needs OPENAI_API_KEY at the first call); effort medium, as the
    live QST-04 pass ran."""
    from agentic_analytics.cost import model_prices
    from agentic_analytics.models import make_agent

    return make_agent(
        model, effort, prices if prices is not None else model_prices(), client=client
    )


@dataclass(frozen=True)
class DualResult:
    question_id: str
    gpt_sql: str
    agree: bool
    reason: str | None  # why they disagree (or why they could not be compared)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _result(ex: Execution) -> ResultSet | None:
    if ex.error is not None or ex.result is None or ex.truncated:
        return None
    return ex.result


def compare_dual(question: Question, gpt_sql: str, executor: Any, *, as_of: date) -> DualResult:
    """Run the gold and GPT Sol's SQL on ClickHouse; agree when S2.1's matcher says so."""
    if question.gold_sql is None:
        raise ValueError(f"{question.id} has no gold SQL")
    gold_ex = executor.execute(
        bind_as_of(question.gold_sql["clickhouse"], as_of), row_cap=GOLD_ROW_CAP
    )
    gpt_ex = executor.execute(gpt_sql, row_cap=GOLD_ROW_CAP)
    gold, gpt = _result(gold_ex), _result(gpt_ex)
    if gold is None:
        return DualResult(question.id, gpt_sql, False, f"gold failed ({gold_ex.error})")
    if gpt is None:
        why = gpt_ex.error or f"more than {GOLD_ROW_CAP} rows"
        return DualResult(question.id, gpt_sql, False, f"GPT Sol's SQL failed ({why})")
    reason = cross_engine_mismatch(question, gold, gpt)
    return DualResult(question.id, gpt_sql, reason is None, reason)


async def dual_authorship(
    questions: Sequence[Question], *, model: Any, executor: Any, ddl: str, as_of: date
) -> list[DualResult]:
    """QST-04 over every answer-expected question: the rows of the disagreement report."""
    out: list[DualResult] = []
    for q in questions:
        if q.expected != "answer" or q.gold_sql is None:
            continue
        try:
            draft = await gpt_sol_sql(q.text, ddl=ddl, as_of=as_of, model=model)
        except DualAuthorError as exc:
            out.append(DualResult(q.id, "", False, str(exc)))
            continue
        out.append(compare_dual(q, draft.sql, executor, as_of=as_of))
    return out


__all__ = [
    "GPT_SOL_MODEL",
    "SYSTEM",
    "DualAuthorError",
    "DualDraft",
    "DualResult",
    "compare_dual",
    "dual_authorship",
    "extract_sql",
    "gpt_sol_sql",
    "make_gpt_sol",
    "table_ddl",
    "user_prompt",
]
