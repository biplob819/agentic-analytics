"""Fakes for the demo tests (D4.1). No network, no account.

- `fake_context` (from `example_data`) is the runner's fake world as live services.
- `SlowModel` is an `AgentModel` whose turns take `delay` seconds (or hang) and end with a
  clarification, so a panel needs no warehouse: for parallelism, timeout and cap tests.
- `SpyStore` is EvalKit's `SqliteSpend` recording every reserve and settle.
- `FakePg` is a tiny Postgres stand-in for `PostgresSpend` that honours `search_path`, enough to
  show that this project's counter and EvalKit's live apart.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from evalkit.demo.guard import SqliteSpend

from agentic_analytics.models import Conversation, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from tests.demo.example_data import demo_world, fake_context
from tests.run import world as w

__all__ = ["FakePg", "SlowModel", "SpyStore", "demo_world", "fake_context"]


def clarify_turn(cost: float = 0.001, tokens_in: int = 500) -> ModelTurn:
    args = {"sql": None, "answer": None, "clarification": "Which year?", "suggestions": []}
    return ModelTurn(
        text="",
        tool_calls=(ToolCall(id="f1", name="final_answer", arguments=args),),
        stop="tool_use",
        usage=Usage(tokens_in=tokens_in, tokens_out=40),
        cost_usd=cost,
        latency_ms=10,
        model_snapshot="claude-sonnet-5-5-20261101",
    )


@dataclass
class SlowModel:
    """Turns that take `delay` seconds; `turns` overrides the default clarification turn."""

    delay: float = 0.0
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    turns: list[ModelTurn | BaseException] = field(default_factory=list)
    steps: int = 0
    finished: int = 0
    users: list[str] = field(default_factory=list)

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Conversation:
        self.users.append(user)
        return Conversation(system=system, tools=tools)

    async def step(self, conv: Conversation) -> ModelTurn:
        self.steps += 1
        await asyncio.sleep(self.delay)
        self.finished += 1
        if self.turns:
            item = self.turns.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return clarify_turn()

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None:
        return None

    def add_user(self, conv: Conversation, text: str) -> None:
        return None


def gpt_slow(delay: float = 0.0) -> SlowModel:
    return SlowModel(delay=delay, provider="openai", model="gpt-6.1-sol")


def context_with(models: dict[str, Any], backends: tuple[str, ...], counter=None, **limits: Any):
    """`fake_context` for `backends` with some agent models replaced."""
    from dataclasses import replace

    ctx = fake_context(backends, counter)  # type: ignore[arg-type]
    ctx.services.models.update(models)
    if limits:
        ctx.limits = replace(ctx.limits, **limits)
    return ctx


class SpyStore(SqliteSpend):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.reserved: list[float] = []
        self.settled: list[tuple[float, float]] = []

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        self.reserved.append(amount)
        return super().reserve(day, amount, cap)

    def settle(self, day: str, reserved: float, actual: float) -> None:
        self.settled.append((reserved, actual))
        super().settle(day, reserved, actual)


class _Cursor:
    def __init__(self, row: Any) -> None:
        self.row = row

    def fetchone(self) -> Any:
        return self.row


class FakePg:
    """Shared state for `connect(url)`: tables per schema, statements seen."""

    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, Decimal]] = {"public": {}}
        self.statements: list[str] = []

    def connect(self, url: str) -> _PgConn:
        return _PgConn(self)


class _PgConn:
    def __init__(self, db: FakePg) -> None:
        self.db = db
        self.path = "public"
        self.closed = False

    def __enter__(self) -> _PgConn:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.path = "public"  # SET LOCAL ends with the transaction
        self.closed = True

    def commit(self) -> None:
        self.path = "public"

    def rollback(self) -> None:
        self.path = "public"

    def close(self) -> None:
        self.closed = True

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        db = self.db
        db.statements.append(sql)
        if m := re.fullmatch(r'CREATE SCHEMA IF NOT EXISTS "(.+)"', sql):
            db.schemas.setdefault(m.group(1).replace('""', '"'), {})
            return _Cursor(None)
        if m := re.fullmatch(r'SET LOCAL search_path TO "(.+)"', sql):
            name = m.group(1).replace('""', '"')
            if name not in db.schemas:
                raise RuntimeError(f"schema {name} does not exist")
            self.path = name
            return _Cursor(None)
        table = db.schemas[self.path]
        if sql.startswith("CREATE TABLE IF NOT EXISTS demo_spend"):
            return _Cursor(None)
        if sql.startswith("INSERT INTO demo_spend"):
            table.setdefault(params[0], Decimal(0))
            return _Cursor(None)
        if sql.startswith("UPDATE demo_spend SET usd = usd +"):
            amount, day, _, cap = params
            if table[day] + amount <= cap:
                table[day] += amount
                return _Cursor((table[day],))
            return _Cursor(None)
        if sql.startswith("UPDATE demo_spend SET usd = GREATEST"):
            delta, day = params
            table[day] = max(Decimal(0), table.get(day, Decimal(0)) + delta)
            return _Cursor(None)
        if sql.startswith("SELECT usd FROM demo_spend"):
            v = table.get(params[0])
            return _Cursor(None if v is None else (v,))
        raise AssertionError(f"unexpected SQL: {sql}")


def answered_world_ctx(counter: w.Counter | None = None, backends=("claude_mcp", "cortex_analyst")):
    return fake_context(backends, counter)  # type: ignore[arg-type]
