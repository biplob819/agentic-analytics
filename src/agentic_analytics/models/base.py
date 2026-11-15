"""Provider-neutral types for the agent loop (N1.6).

The agent loop (A2.1) sees only these: a conversation it can step, tool calls to run, and
results to hand back. Each adapter keeps its provider's history format private.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Effort = Literal["low", "medium", "high"]
Stop = Literal["tool_use", "end", "refusal", "max_tokens"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    strict: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model sent arguments that are not JSON
    parse_error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    tokens_in: int  # the whole prompt, cached and cache-write parts included
    tokens_out: int  # visible output, reasoning excluded
    tokens_reasoning: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0

    def __post_init__(self) -> None:
        parts = (self.tokens_in, self.tokens_out, self.tokens_reasoning, self.tokens_cached)
        if min(*parts, self.tokens_cache_write) < 0:
            raise ValueError("token counts are non-negative")
        if self.tokens_cached + self.tokens_cache_write > self.tokens_in:
            raise ValueError("cached and cache-write tokens are part of tokens_in")


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: Stop
    usage: Usage
    cost_usd: float
    latency_ms: int
    model_snapshot: str
    request_id: str | None = None
    refusal_category: str | None = None


@dataclass
class Conversation:
    """Opaque per-provider history plus what every provider needs."""

    system: str
    tools: tuple[ToolSpec, ...]
    history: list[Any] = field(default_factory=list)


class AgentModel(Protocol):
    provider: str
    model: str
    effort: Effort

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Conversation: ...

    async def step(self, conv: Conversation) -> ModelTurn: ...

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None: ...

    def add_user(self, conv: Conversation, text: str) -> None: ...
