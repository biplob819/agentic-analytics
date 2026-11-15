"""Model adapters for the agent loop (N1.6)."""

from __future__ import annotations

from typing import Any

from evalkit.cost import Price

from agentic_analytics.models.anthropic import AnthropicAgent
from agentic_analytics.models.base import (
    AgentModel,
    Conversation,
    Effort,
    ModelTurn,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from agentic_analytics.models.openai import OpenAIAgent


def make_agent(
    model: str, effort: Effort, prices: dict[str, Price], *, client: Any = None
) -> AgentModel:
    """The adapter for a model id: `claude-*` on Anthropic, `gpt-*` on OpenAI."""
    if model.startswith("claude-"):
        return AnthropicAgent(model, effort, prices, client=client)
    if model.startswith("gpt-"):
        return OpenAIAgent(model, effort, prices, client=client)
    raise ValueError(f"no adapter for model {model!r} (expected claude-* or gpt-*)")


__all__ = [
    "AgentModel",
    "AnthropicAgent",
    "Conversation",
    "Effort",
    "ModelTurn",
    "OpenAIAgent",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "make_agent",
]
