"""Runner, cache, preconditions and CLI plumbing (R3.1: RUN-01..RUN-06)."""

from agentic_analytics.run.cache import AnswerCache, AskResult, KeyParts, cache_key
from agentic_analytics.run.config import RunConfigError, parse_backends, parse_fixes
from agentic_analytics.run.gold import GoldCheck, PreconditionError, compute_gold_check_id
from agentic_analytics.run.runner import (
    RunOptions,
    RunOutcome,
    Services,
    run_benchmark,
    smoke_questions,
)

__all__ = [
    "AnswerCache",
    "AskResult",
    "GoldCheck",
    "KeyParts",
    "PreconditionError",
    "RunConfigError",
    "RunOptions",
    "RunOutcome",
    "Services",
    "cache_key",
    "compute_gold_check_id",
    "parse_backends",
    "parse_fixes",
    "run_benchmark",
    "smoke_questions",
]
