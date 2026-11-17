"""Runner, cache, preconditions and CLI plumbing (R3.1: RUN-01..RUN-06).

Committed with Q2.2 for the gold-check format (D-73); R3.1 adds the runner.
"""

from agentic_analytics.run.cache import AnswerCache, AskResult, KeyParts, cache_key
from agentic_analytics.run.config import RunConfigError, parse_backends, parse_fixes
from agentic_analytics.run.gold import GoldCheck, PreconditionError, compute_gold_check_id

__all__ = [
    "AnswerCache",
    "AskResult",
    "GoldCheck",
    "KeyParts",
    "PreconditionError",
    "RunConfigError",
    "cache_key",
    "compute_gold_check_id",
    "parse_backends",
    "parse_fixes",
]
