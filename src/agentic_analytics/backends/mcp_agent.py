"""One tool loop for the MCP agents, Claude and GPT alike (A2.1, Tech Spec §2.4 and §2.6).

- Both models get the same system prompt, the toolbox's three read tools plus `final_answer`,
  and the question as the user turn (AGT-01, AGT-02).
- `final_answer` takes SQL and an answer, or a clarification with suggestions. A violation
  gets one tool error naming the field; a second ends with `schema`. A turn that ends without
  it gets one reminder, then `schema` (AGT-03).
- Budgets: the toolbox's six MCP calls (the call past them is refused with a tool error and the
  model gets one turn to finish), `max_turns` model turns and `wall_s` seconds; past any of
  them the question ends with `step_budget`. A `max_tokens` stop is `step_budget` too: a
  resource limit was hit and the cut turn's tool calls are never run. A refusal stop is
  `refusal`; provider failures that outlast the retries are `transport`; config errors and
  bugs raise so the run stops (EK L-21) (AGT-04).
- Every MCP call and `final_answer` is an `AgentStep`; a turn's tokens go to its first step;
  cost and token totals come back in `AgentRun` (AGT-05).
- Connection: the loop owns the MCP connection's task, so before a turn's MCP calls it reopens
  a connection the toolbox flagged `needs_reopen` (once; a failed reopen is `transport`).
- SQL guard (D-37): an injected `guard(query) -> reason | None` runs on every
  `run_select_query` before the toolbox. A refusal is a tool error that never reaches the
  server, counts as one call on the toolbox's own counter, and is recorded as `write_refused`
  for a write, else `guard`. A guard that raises stops the run: a guard bug is a config error.
- Self-check (F3.1's hook): after the first valid SQL answer, `await self_check(sql)` runs
  under the wall clock. A non-blank message is sent once as a user turn when MCP calls, a model
  turn and at least `self_check_min_s` seconds remain; otherwise, or if the hook times out, the
  check is `skipped` and the answer stands. If the follow-up then overruns a budget, the first
  valid answer is returned rather than `step_budget` (its steps are kept).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from agentic_analytics.backends.prompts import (
    ACCEPTED,
    DUPLICATE,
    FINAL_ANSWER_SPEC,
    GUARD_REFUSAL,
    REJECTED,
    REMINDER,
    render_system_prompt,
)
from agentic_analytics.mcp.client import Toolbox, ToolOutcome
from agentic_analytics.models.base import AgentModel, ModelTurn, ToolCall, ToolResult
from agentic_analytics.models.errors import classify_error
from agentic_analytics.schemas import AgentStep, AnalyticsAnswer, BackendName
from agentic_analytics.schemas._base import deep_freeze
from agentic_analytics.steps import (
    FINAL_ANSWER_TOOL,
    STEP_ERROR_BAD_ARGS,
    STEP_ERROR_BUDGET,
    STEP_ERROR_GUARD,
    STEP_ERROR_SQL,
    STEP_ERROR_WRITE_REFUSED,
    is_write_sql,
)

AnswerError = Literal["transport", "schema", "refusal", "step_budget", "timeout"]
SelfCheck = Callable[[str], Awaitable[str | None]]
Guard = Callable[[str], str | None]  # a refusal reason, or None to let the query run
SelfCheckOutcome = Literal["not_run", "passed", "sent", "skipped"]

BACKEND_OF_PROVIDER: dict[str, BackendName] = {"anthropic": "claude_mcp", "openai": "gpt_mcp"}
FINAL_FIELDS = ("sql", "answer", "clarification", "suggestions")
FEEDBACK_MIN_S = 20.0  # wall clock that must remain to send self-check feedback
DETAIL_CHARS = 300
# server messages that mean the read-only role refused a write or a settings change
_READONLY_MARKERS = ("readonly", "read-only", "read only", "code: 164")


class FinalAnswerError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"field '{field}': {message}")
        self.field = field


@dataclass(frozen=True)
class FinalAnswer:
    sql: str | None
    answer: str | None
    clarification: str | None
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class AgentRun:
    answer: AnalyticsAnswer
    cost_usd: float
    tokens: dict[str, int]  # in, out, reasoning, cached, cache_write over every model turn
    turns: int
    self_check: SelfCheckOutcome = "not_run"


def _text_field(args: Mapping[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FinalAnswerError(name, "must be a string or null")
    return value if value.strip() else None


def parse_final_answer(args: Mapping[str, Any] | None) -> FinalAnswer:
    """Exactly one of {sql and answer} or {clarification}; suggestions only with the latter."""
    if not isinstance(args, Mapping):
        raise FinalAnswerError("arguments", "must be a JSON object")
    for key in args:
        if key not in FINAL_FIELDS:
            raise FinalAnswerError(str(key), "is not a final_answer field")
    sql, answer = _text_field(args, "sql"), _text_field(args, "answer")
    clarification = _text_field(args, "clarification")
    raw = args.get("suggestions")
    raw = [] if raw is None else raw
    if not isinstance(raw, list | tuple) or not all(isinstance(s, str) and s.strip() for s in raw):
        raise FinalAnswerError("suggestions", "must be a list of non-blank strings")
    suggestions = tuple(s.strip() for s in raw)
    if clarification is not None:
        if sql is not None:
            raise FinalAnswerError(
                "clarification", "give sql and answer, or a clarification, not both"
            )
        if answer is not None:
            raise FinalAnswerError("answer", "must be null when you ask for clarification")
        return FinalAnswer(None, None, clarification, suggestions)
    if sql is None:
        message = "is missing: give sql and answer, or a clarification"
        raise FinalAnswerError("sql", message)
    if answer is None:
        raise FinalAnswerError("answer", "is missing: an answer with sql needs the answer text")
    if suggestions:
        raise FinalAnswerError("suggestions", "only go with a clarification, not with sql")
    return FinalAnswer(sql, answer, None, ())


def _recordable(args: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        return {}
    try:
        deep_freeze(dict(args))
    except (ValueError, RecursionError):
        return {"_unrecordable": repr(args)[:DETAIL_CHARS]}
    return dict(args)


def _detail(code: str, text: str) -> str:
    text = " ".join(text.split())
    return f"{code}: {text[:DETAIL_CHARS]}" if text else code


def _step_error(call: ToolCall, outcome: ToolOutcome) -> str | None:
    """`AgentStep.error` in the `scoring/policy.py` convention, `"<code>: <detail>"`."""
    if outcome.refused is not None:
        return _detail(outcome.refused, call.parse_error or outcome.content)
    if not outcome.is_error:
        return None
    query = (call.arguments or {}).get("query")
    text = outcome.content.casefold()
    if (isinstance(query, str) and is_write_sql(query)) or any(
        m in text for m in _READONLY_MARKERS
    ):
        return _detail(STEP_ERROR_WRITE_REFUSED, outcome.content)
    return _detail(STEP_ERROR_SQL, outcome.content)


class _Stop(Exception):
    """Ends the loop with an `AnalyticsAnswer.error`."""

    def __init__(self, error: AnswerError, text: str = "") -> None:
        super().__init__(error)
        self.error: AnswerError = error
        self.text = text


class _Loop:
    def __init__(
        self,
        model: AgentModel,
        toolbox: Toolbox,
        backend: BackendName,
        max_turns: int,
        wall_s: float,
        self_check: SelfCheck | None,
        clock: Callable[[], float],
        guard: Guard | None = None,
        feedback_min_s: float = FEEDBACK_MIN_S,
    ) -> None:
        self.model, self.toolbox, self.backend = model, toolbox, backend
        self.max_turns, self.wall_s, self.self_check, self.clock = (
            max_turns,
            wall_s,
            self_check,
            clock,
        )
        self.guard, self.feedback_min_s = guard, feedback_min_s
        self.first_final: FinalAnswer | None = None  # the answer self-check feedback was about
        self.t0 = clock()
        self.steps: list[AgentStep] = []
        self.turns: list[ModelTurn] = []
        self.violations = 0
        self.reminded = False
        self.budget_refusals = 0  # turns in which the toolbox refused a call for budget
        self.check: SelfCheckOutcome = "not_run"

    # -- limits ---------------------------------------------------------------------------
    def remaining(self) -> float:
        return self.wall_s - (self.clock() - self.t0)

    def check_clock(self) -> None:
        if self.remaining() < 0:
            raise _Stop("step_budget")

    async def timed(self, aw: Awaitable[Any]) -> Any:
        """Await within the wall clock; only our own deadline becomes `step_budget`."""
        self.check_clock()
        cm = asyncio.timeout(max(self.remaining(), 0.0))
        try:
            async with cm:
                return await aw
        except TimeoutError:
            if cm.expired():
                raise _Stop("step_budget") from None
            raise

    # -- steps ----------------------------------------------------------------------------
    def record(
        self,
        call: ToolCall,
        turn: ModelTurn,
        first: bool,
        *,
        latency_ms: int,
        error: str | None = None,
        rows: int | None = None,
        truncated: bool = False,
    ) -> None:
        u = turn.usage
        self.steps.append(
            AgentStep(
                index=len(self.steps),
                tool=call.name if call.name and call.name.strip() else "?",
                arguments=_recordable(call.arguments),
                rows_returned=rows,
                truncated=truncated,
                error=error,
                latency_ms=max(int(latency_ms), 0),
                tokens_in=u.tokens_in if first else 0,
                tokens_out=u.tokens_out + u.tokens_reasoning if first else 0,
            )
        )

    def guard_refusal(self, call: ToolCall) -> ToolOutcome | None:
        """D-37: the guard's verdict on a query, counted on the toolbox's own call counter."""
        query = (call.arguments or {}).get("query")
        if self.guard is None or call.name != "run_select_query" or not isinstance(query, str):
            return None
        if self.toolbox.calls_made >= self.toolbox.max_calls:
            return None  # over budget: the toolbox refuses it as `budget`
        reason = self.guard(query)
        if reason is None:
            return None
        self.toolbox.calls_made += 1  # an attempt like any other (MCP-04)
        return ToolOutcome(content=GUARD_REFUSAL.format(reason=reason), is_error=True)

    async def run_mcp(self, call: ToolCall, turn: ModelTurn, first: bool) -> ToolOutcome:
        refused = self.guard_refusal(call)
        if refused is not None:
            query = str((call.arguments or {}).get("query"))
            code = STEP_ERROR_WRITE_REFUSED if is_write_sql(query) else STEP_ERROR_GUARD
            self.record(call, turn, first, latency_ms=0, error=_detail(code, refused.content))
            return refused
        try:
            outcome: ToolOutcome = await self.timed(self.toolbox.call(call.name, call.arguments))
        except _Stop:
            error = _detail(STEP_ERROR_BUDGET, "wall clock ran out during the call")
            self.record(call, turn, first, latency_ms=0, error=error)
            raise
        self.record(
            call,
            turn,
            first,
            latency_ms=outcome.latency_ms,
            error=_step_error(call, outcome),
            rows=outcome.rows_returned,
            truncated=outcome.truncated,
        )
        return outcome

    def judge_final(
        self, call: ToolCall, turn: ModelTurn, first: bool
    ) -> tuple[ToolResult, FinalAnswer | None]:
        try:
            final = parse_final_answer(call.arguments)
        except FinalAnswerError as exc:
            self.violations += 1
            message = REJECTED.format(error=exc)
            error = _detail(STEP_ERROR_BAD_ARGS, str(exc))
            self.record(call, turn, first, latency_ms=turn.latency_ms, error=error)
            return ToolResult(call.id, message, is_error=True), None
        self.record(call, turn, first, latency_ms=turn.latency_ms)
        return ToolResult(call.id, ACCEPTED), final

    # -- the loop -------------------------------------------------------------------------
    async def step_model(self, conv: Any) -> ModelTurn:
        if len(self.turns) >= self.max_turns:
            raise _Stop("step_budget")
        try:
            turn: ModelTurn = await self.timed(self.model.step(conv))
        except _Stop:
            raise
        except Exception as exc:
            if classify_error(exc) == "config":
                raise  # auth, bad request, quota, unknown model or a bug: stop the run
            raise _Stop("transport") from exc
        self.turns.append(turn)
        self.check_clock()
        return turn

    async def reopen_if_needed(self) -> None:
        """Reopen a connection the toolbox flagged, from the task that owns it (N1.5)."""
        conn = self.toolbox.conn
        if not getattr(conn, "needs_reopen", False):
            return
        try:
            await self.timed(conn.ensure_open())
        except _Stop:
            raise
        except Exception as exc:
            raise _Stop("transport") from exc

    async def run(self, conv: Any) -> FinalAnswer:
        while True:
            turn = await self.step_model(conv)
            if turn.stop == "refusal":
                raise _Stop("refusal", turn.text)
            if turn.stop == "max_tokens":
                raise _Stop("step_budget")
            if not turn.tool_calls:
                if self.reminded:
                    raise _Stop("schema")
                self.reminded = True
                self.model.add_user(conv, REMINDER)
                continue
            if any(c.name != FINAL_ANSWER_TOOL for c in turn.tool_calls):
                await self.reopen_if_needed()
            results: list[ToolResult] = []
            accepted: FinalAnswer | None = None
            budget_hit = False
            mcp_called = False
            for i, call in enumerate(turn.tool_calls):
                first = i == 0
                if call.name == FINAL_ANSWER_TOOL:
                    if accepted is not None:
                        error = _detail(STEP_ERROR_BAD_ARGS, DUPLICATE)
                        self.record(call, turn, first, latency_ms=0, error=error)
                        results.append(ToolResult(call.id, DUPLICATE, is_error=True))
                        continue
                    result, accepted = self.judge_final(call, turn, first)
                    results.append(result)
                    continue
                mcp_called = True
                outcome = await self.run_mcp(call, turn, first)
                budget_hit = budget_hit or outcome.refused == STEP_ERROR_BUDGET
                results.append(ToolResult(call.id, outcome.content, is_error=outcome.is_error))
            if accepted is not None:
                if await self.wants_feedback(accepted, conv, results):
                    continue
                return accepted
            if self.violations >= 2:
                raise _Stop("schema")
            if mcp_called and self.budget_refusals >= 1:
                raise _Stop("step_budget")  # MCP again after the budget refusal
            if budget_hit:
                self.budget_refusals += 1
            self.model.add_tool_results(conv, results)

    async def wants_feedback(
        self, final: FinalAnswer, conv: Any, results: list[ToolResult]
    ) -> bool:
        """Self-check: one feedback turn after the first SQL answer while calls remain."""
        if self.self_check is None or final.sql is None or self.check != "not_run":
            return False
        try:
            message = await self.timed(self.self_check(final.sql))
        except _Stop:  # the hook ran out of wall clock: keep the valid answer
            self.check = "skipped"
            return False
        if message is None or not message.strip():
            self.check = "passed"
            return False
        calls_left = self.toolbox.calls_made < self.toolbox.max_calls
        turn_left = len(self.turns) < self.max_turns
        if not (calls_left and turn_left and self.remaining() >= self.feedback_min_s):
            self.check = "skipped"
            return False
        self.check = "sent"
        self.first_final = final
        self.model.add_tool_results(conv, results)
        self.model.add_user(conv, message)
        return True

    # -- results --------------------------------------------------------------------------
    def answer(self, final: FinalAnswer | None, stop: _Stop | None) -> AnalyticsAnswer:
        last = self.turns[-1] if self.turns else None
        common: dict[str, Any] = {
            "backend": self.backend,
            "model_snapshot": (last.model_snapshot if last else "") or self.model.model,
            "steps": tuple(self.steps),
            "request_id": last.request_id if last else None,
        }
        if final is None:
            assert stop is not None
            return AnalyticsAnswer(sql=None, answer=stop.text, error=stop.error, **common)
        return AnalyticsAnswer(
            sql=final.sql,
            answer=final.answer or "",
            clarification=final.clarification,
            suggestions=final.suggestions,
            **common,
        )

    def totals(self) -> tuple[float, dict[str, int]]:
        cost = math.fsum(t.cost_usd for t in self.turns)
        tokens = {
            "in": sum(t.usage.tokens_in for t in self.turns),
            "out": sum(t.usage.tokens_out for t in self.turns),
            "reasoning": sum(t.usage.tokens_reasoning for t in self.turns),
            "cached": sum(t.usage.tokens_cached for t in self.turns),
            "cache_write": sum(t.usage.tokens_cache_write for t in self.turns),
        }
        return cost, tokens


async def ask_mcp(
    question_text: str,
    *,
    model: AgentModel,
    toolbox: Toolbox,
    as_of: date,
    fix_blocks: Sequence[str] = (),
    max_turns: int = 10,
    wall_s: float = 120.0,
    self_check: SelfCheck | None = None,
    guard: Guard | None = None,
    self_check_min_s: float = FEEDBACK_MIN_S,
    clock: Callable[[], float] = time.monotonic,
) -> AgentRun:
    """Ask one question through the MCP toolbox and return the answer with its cost."""
    backend = BACKEND_OF_PROVIDER.get(getattr(model, "provider", ""))
    if backend is None:
        raise ValueError(f"no MCP backend for provider {getattr(model, 'provider', None)!r}")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")
    if not (isinstance(wall_s, int | float) and math.isfinite(wall_s) and wall_s > 0):
        raise ValueError("wall_s must be a positive number of seconds")
    if not (isinstance(self_check_min_s, int | float) and math.isfinite(self_check_min_s)):
        raise ValueError("self_check_min_s must be a finite number of seconds")
    system = render_system_prompt(as_of, fix_blocks)
    tools = (*toolbox.specs, FINAL_ANSWER_SPEC)
    loop = _Loop(
        model,
        toolbox,
        backend,
        max_turns,
        float(wall_s),
        self_check,
        clock,
        guard,
        float(self_check_min_s),
    )
    conv = model.start(system, question_text, tools)
    final: FinalAnswer | None = None
    stop: _Stop | None = None
    try:
        final = await loop.run(conv)
    except _Stop as exc:
        if exc.error == "step_budget" and loop.first_final is not None:
            final = loop.first_final  # the follow-up to feedback overran: keep the first answer
        else:
            stop = exc
    cost, tokens = loop.totals()
    return AgentRun(
        answer=loop.answer(final, stop),
        cost_usd=cost,
        tokens=tokens,
        turns=len(loop.turns),
        self_check=loop.check,
    )
