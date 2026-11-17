"""Scorers (S2.1, Tech Spec §6.1): re-execution, result match, clarification, answer from rows
and policy, composed by `score_record` into a schema-valid `Scores`."""

from agentic_analytics.scoring.clarify import Clarification, clarification
from agentic_analytics.scoring.execute import ROW_CAP, Executor, ScoringError, reexecute
from agentic_analytics.scoring.numbers import Number, extract_numbers, unsupported_numbers
from agentic_analytics.scoring.policy import (
    FINAL_ANSWER_TOOL,
    MAX_TOOL_CALLS,
    STEP_ERROR_CODES,
    Policy,
    is_write_sql,
    policy,
)
from agentic_analytics.scoring.result import match_result, result_match
from agentic_analytics.scoring.score import score_record
from agentic_analytics.scoring.tolerant import tolerant_match

__all__ = [
    "FINAL_ANSWER_TOOL",
    "MAX_TOOL_CALLS",
    "ROW_CAP",
    "STEP_ERROR_CODES",
    "Clarification",
    "Executor",
    "Number",
    "Policy",
    "ScoringError",
    "clarification",
    "extract_numbers",
    "is_write_sql",
    "match_result",
    "policy",
    "reexecute",
    "result_match",
    "score_record",
    "tolerant_match",
    "unsupported_numbers",
]
