"""Backends that turn a question into an `AnalyticsAnswer`: the MCP agent loop (A2.1)."""

from agentic_analytics.backends.mcp_agent import (
    AgentRun,
    FinalAnswer,
    FinalAnswerError,
    Guard,
    SelfCheck,
    ask_mcp,
    parse_final_answer,
)
from agentic_analytics.backends.prompts import (
    BASE_PROMPT,
    FINAL_ANSWER_SPEC,
    PROMPT_VERSION,
    as_of_line,
    prompt_hash,
    render_system_prompt,
)

__all__ = [
    "BASE_PROMPT",
    "FINAL_ANSWER_SPEC",
    "PROMPT_VERSION",
    "AgentRun",
    "FinalAnswer",
    "FinalAnswerError",
    "Guard",
    "SelfCheck",
    "as_of_line",
    "ask_mcp",
    "parse_final_answer",
    "prompt_hash",
    "render_system_prompt",
]
