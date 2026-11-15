"""Error taxonomy for provider calls (MOD-05, Tech Spec §2.6, EK L-21).

Only transient provider failures that outlast the retries are `transport`. Authentication,
permission, bad-request and unknown-model errors are `config`: they stop the run instead of
being scored as if the model had failed a question.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Literal

from evalkit.retry import is_retryable

ErrorKind = Literal["transport", "config"]
CONFIG_STATUS = frozenset({400, 401, 402, 403, 404, 413, 422})


class QuotaExhausted(Exception):
    """The account is out of credit (OpenAI 429 `insufficient_quota`): never retried."""

    status_code = 402


def _is_quota(exc: BaseException) -> bool:
    """True only for a recognisable out-of-quota error; any odd error body is just 'no'."""
    try:
        code = getattr(exc, "code", None)
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            code = code or body.get("code")
            err = body.get("error")
            if isinstance(err, dict):
                code = code or err.get("code")
        return code == "insufficient_quota"
    except Exception:
        return False


async def quota_guard(call: Awaitable[Any]) -> Any:
    try:
        return await call
    except Exception as exc:
        if _is_quota(exc):
            raise QuotaExhausted(str(exc)) from exc
        raise


def classify_error(exc: BaseException) -> ErrorKind:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in CONFIG_STATUS:
        return "config"
    if is_retryable(exc):
        return "transport"
    return "config"
