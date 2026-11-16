"""Traces: Langfuse spans, JSON export and redaction (A2.3: ANS-03, ANS-04, ANS-05; D-16, NFR4).

- `build_trace` turns one answer into a `Trace`: one per (run, question, backend, fix set, trial),
  with a deterministic 32-hex trace id, a generation span per model turn (tokens, cost, latency,
  request id), a tool span per agent step, a Cortex span carrying `request_id`, and an execution
  span. Traces are built after the run from what Ask returned, so latency is a number on the span,
  not span timing. Without the model turns, generation spans are derived from the steps that carry
  tokens (a turn's tokens sit on its first step, A2.1) and their cost is unknown (`None`).
- `Tracer` is a protocol: `send(trace) -> url | None` and `flush()`. `NoopTracer` sends nothing.
  `LangfuseTracer` imports the `langfuse` package lazily; it is not a project dependency, so a
  missing package or missing keys raise `TracingUnavailable` with the fix.
- `export_trace` writes the trace as JSON to `<results>/<run_id>/traces/` (sorted keys, UTF-8) and
  returns the relative path for `AnalyticsAnswer.trace_export`; `select_for_export` picks failures
  and a deterministic sample. The dashboard renders these copies because Langfuse Hobby retention
  makes links rot (H9).
- `Redactor` scrubs everything exported or sent to a tracer: secret env values by name, auth
  headers, values under secret-named keys, and the key patterns of `scripts/secret_scan.py`
  (copied here, since `src` never imports `scripts`; a test pins the copy to the script).

Assumed Langfuse Python SDK v3 surface (OpenTelemetry based), checked by the `live` test:

    client = Langfuse(public_key=..., secret_key=..., host=...)
    root = client.start_span(name=..., trace_context={"trace_id": <32 hex>}, input=, output=,
                             metadata=)
    root.update_trace(name=, session_id=, tags=[...], public=bool, input=, output=, metadata=)
    child = root.start_generation(name=, model=, input=, output=, metadata=,
                                  usage_details={"input": n, "output": n},
                                  cost_details={"total": usd}, level=, status_message=)
    child = root.start_span(name=, input=, output=, metadata=, level=, status_message=)
    child.end(end_time=<ns>); root.end(end_time=<ns>)
    client.get_trace_url(trace_id=...); client.flush()

Pin when installed: `langfuse>=3,<4`. v3 takes no start time when a span is created, so each span
is ended `latency_ms` after its creation (`end_time` in epoch nanoseconds) to give Langfuse the
real duration; the span's place in the run is in metadata (`start_offset_ms`, `end_offset_ms`,
and `start_time`/`end_time` when the trace start is known), laid end to end in span order, which
approximates the real interleaving of turns and tool calls.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args
from urllib.parse import quote, quote_plus

from pydantic import BaseModel

from agentic_analytics.schemas import (
    FIX_NAMES,
    MCP_BACKENDS,
    SCHEMA_VERSION,
    AnalyticsAnswer,
    BackendName,
    Execution,
    Record,
)

if TYPE_CHECKING:
    from agentic_analytics.models.base import ModelTurn

EXPORT_ROWS = 50  # rows of the executed result kept in an export
SpanKind = Literal["generation", "tool", "execution"]
ExportReason = Literal["failure", "sample"]

# -- redaction (ANS-05) -----------------------------------------------------------------------

# A faithful copy of scripts/secret_scan.py PATTERNS (pinned by tests/tracing/test_redact.py).
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai key": re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws secret key": re.compile(
        r"(?i)aws_?secret_?(?:access_?)?key\W{0,4}[=:]\W{0,4}[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"
    ),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "github fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    "pypi token": re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}"),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    ),
    "langfuse secret key": re.compile(r"\bsk-lf-[0-9a-f][0-9a-f-]{20,}"),
    "url with password": re.compile(
        r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:(?P<pw>[^\s@/'\"]+)@"
    ),
}

# Every secret in .env.example (HAR-07); `from_environ` adds any other secret-looking name.
SECRET_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLICKHOUSE_MCP_KEY_ID",
    "CLICKHOUSE_MCP_KEY_SECRET",
    "CLICKHOUSE_RO_PASSWORD",
    "CLICKHOUSE_ADMIN_PASSWORD",
    "CLICKHOUSE_CLOUD_API_KEY",
    "CLICKHOUSE_CLOUD_API_SECRET",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "DEMO_PASSCODE",
    "DEMO_OWNER_PASSCODE",
    "EVALKIT_DEMO_DATABASE_URL",
    "SNOWFLAKE_RO_PRIVATE_KEY",  # D-57: the PEM text on the hosted demo
)
# the generic `sk-` OpenAI pattern also matches Langfuse keys, so the specific labels go first
_REDACT_ORDER = sorted(SECRET_PATTERNS.items(), key=lambda kv: kv[0] == "openai key")
SHORT_SECRET_CHARS = 8  # shorter secrets are scrubbed only as whole tokens
MIN_GUESSED_SECRET_CHARS = 8  # for names that only look secret (`*_TOKEN=true` is not a key)
REDACTED = "[REDACTED]"

_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|SECRET|PASSWORD|PASSWD|PASSCODE|TOKEN|DATABASE_URL)")
# mapping keys whose values are replaced whole; `token(?!s)` spares token counts (`tokens_in`)
_SECRET_FIELD = re.compile(
    r"(?i)(token(?!s)|secret|passw(?:or)?d|passcode|credential|private[-_]?key"
    r"|^authorization$|^proxy[-_]authorization$|^x[-_]api[-_]key$|^api[-_]?key$|^key$"
    r"|^cookie$|^set[-_]cookie$)"
)
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
# a header name, its separator and an optional opening quote (plain or JSON-escaped)
_HEADER = re.compile(
    r"(?i)\b(proxy-authorization|authorization|x-api-key|api[-_]?key|set-cookie|cookie)"
    r"(?:\\*[\"'])?\s*[:=]\s*(?P<q>\\*[\"'])?"
)
_UNQUOTED_VALUE = re.compile(r"(?i)(?:(?:bearer|basic|token|digest)\s+)?[^\s\"'\\,;&}\]]*")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|id_token|key|api_key|apikey|sig|signature"
    r"|password|secret|auth)=)[^&\s#\"'\\]+"
)
_BARE_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})")


def _credential_like(token: str) -> bool:
    has_case = any(c.isupper() for c in token) and any(c.islower() for c in token)
    return any(c.isdigit() or c in "+/=" for c in token) or has_case


def _scrub_headers(s: str) -> str:
    """Replace each header value: up to its closing quote, the line end (cookies), or one token."""
    out: list[str] = []
    pos = 0
    for m in _HEADER.finditer(s):
        if m.start() < pos:
            continue
        start = m.end()
        quote = m.group("q")
        if quote:
            ends = [e for e in (s.find(quote, start), s.find("\n", start)) if e != -1]
            end = min(ends) if ends else len(s)
        elif "cookie" in m.group(1).lower():
            line_end = s.find("\n", start)
            end = line_end if line_end != -1 else len(s)
        else:
            value = _UNQUOTED_VALUE.match(s, start)
            end = value.end() if value else start
        if end > start:
            out += [s[pos:start], REDACTED]
            pos = end
    out.append(s[pos:])
    return "".join(out)


def _variants(value: str) -> set[str]:
    """The secret as it can appear in text: raw, JSON-escaped once or twice, URL-encoded."""
    once = {json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    twice = {json.dumps(v)[1:-1] for v in once}
    return {value, *once, *twice, quote(value, safe=""), quote_plus(value, safe="")}


class Redactor:
    """Scrubs secrets from text and from JSON-like structures (which come back as plain JSON).

    Rules, in order, on every string (numbers are never changed, so a short numeric secret never
    corrupts a result cell; it is scrubbed only where it appears inside a string):

    1. each secret value, in every form from `_variants` (raw, JSON-escaped once or twice,
       URL-encoded): one of at least `SHORT_SECRET_CHARS` characters anywhere, a shorter one
       (`DEMO_PASSCODE=482`) only as a whole token, so `4821` and `x482y` are left alone;
    2. PEM private-key blocks;
    3. Authorization, Proxy-Authorization, X-Api-Key, api_key, Cookie and Set-Cookie values,
       case-insensitively, quoted, JSON-escaped or bare;
    4. URL query parameters token, access_token, key, sig, signature, password and similar;
    5. bare `Bearer`/`Basic` credentials;
    6. every `scripts/secret_scan.py` pattern (a URL password keeps its URL).

    In structures, values under keys named like *token* (not *tokens*), *secret*, *password*,
    *passcode*, *credential*, private_key, api_key, key, authorization or cookie are replaced
    whole, and a string holding a JSON object or array is decoded, scrubbed and re-encoded.
    """

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self._secrets = {n: v for n, v in (secrets or {}).items() if isinstance(v, str)}
        long: list[tuple[str, str]] = []
        short: list[tuple[str, str]] = []
        for name, value in (secrets or {}).items():
            if not isinstance(value, str) or not value.strip():
                continue
            bucket = long if len(value) >= SHORT_SECRET_CHARS else short
            bucket += [(v, name) for v in _variants(value)]
        self._long = sorted(set(long), key=lambda vn: (-len(vn[0]), vn))
        self._short = [
            (re.compile(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])"), n)
            for v, n in sorted(set(short), key=lambda vn: (-len(vn[0]), vn))
        ]

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        names: Sequence[str] = SECRET_ENV_NAMES,
    ) -> Redactor:
        env = os.environ if environ is None else environ
        found = {n: env[n] for n in names if env.get(n)}
        for name, value in env.items():
            guessed = name not in found and _SECRET_ENV_NAME.search(name)
            if guessed and len(value) >= MIN_GUESSED_SECRET_CHARS:
                found[name] = value
        return cls(found)

    def with_secrets(self, more: Mapping[str, str]) -> Redactor:
        """A redactor that also scrubs `more` ({label: value}), e.g. a demo visitor's own API
        key for that session only (OWNER D-56); this one is left unchanged."""
        return Redactor({**self._secrets, **more})

    def __repr__(self) -> str:
        return f"Redactor({len(self._secrets)} secret values)"

    def text(self, s: str) -> str:
        for value, name in self._long:
            s = s.replace(value, f"[REDACTED:{name}]")
        for pattern, name in self._short:
            s = pattern.sub(f"[REDACTED:{name}]", s)
        s = _PEM_BLOCK.sub("[REDACTED:private key]", s)
        s = _scrub_headers(s)
        s = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, s)
        s = _BARE_CREDENTIAL.sub(
            lambda m: f"{m.group(1)} {REDACTED}" if _credential_like(m.group(2)) else m.group(0), s
        )
        for label, pat in _REDACT_ORDER:
            if "pw" in pat.groupindex:
                s = pat.sub(_without_password, s)
            else:
                s = pat.sub(f"[REDACTED:{label}]", s)
        return s

    def _string(self, s: str) -> str:
        if s.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(s)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, dict | list):
                try:
                    return json.dumps(self(parsed), ensure_ascii=False)
                except RecursionError:
                    return REDACTED  # too deep to check: fail closed
        return self.text(s)

    def __call__(self, value: Any) -> Any:
        """A plain-JSON copy of `value` with every string scrubbed and secret fields replaced."""
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            return self._string(value)
        if isinstance(value, BaseModel):
            return self(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                secret_field = _SECRET_FIELD.search(str(k)) and v is not None
                out[self.text(str(k))] = REDACTED if secret_field else self(v)
            return out
        if isinstance(value, list | tuple):
            return [self(v) for v in value]
        return self.text(str(value))


def _without_password(m: re.Match[str]) -> str:
    start, end = m.span("pw")
    whole = m.group(0)
    offset = m.start()
    return whole[: start - offset] + REDACTED + whole[end - offset :]


# -- traces (ANS-03) --------------------------------------------------------------------------

_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BACKENDS: tuple[str, ...] = get_args(BackendName)


@dataclass(frozen=True)
class TraceKey:
    """What identifies one trace: one per (run, question, backend, fix set, trial)."""

    run_id: str
    question_id: str
    backend: BackendName
    trial: int
    fixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "question_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SAFE_ID.match(value):
                raise ValueError(f"{name} {value!r} must be a safe file-name part")
        if self.backend not in _BACKENDS:
            raise ValueError(f"unknown backend {self.backend!r}")
        if isinstance(self.trial, bool) or not isinstance(self.trial, int) or self.trial < 1:
            raise ValueError("trial must be a positive integer")
        fixes = tuple(self.fixes)
        if any(x not in FIX_NAMES for x in fixes) or list(fixes) != sorted(
            set(fixes), key=FIX_NAMES.index
        ):
            raise ValueError("fixes must be unique and in the order " + ", ".join(FIX_NAMES))
        object.__setattr__(self, "fixes", fixes)

    @property
    def seed(self) -> str:
        return "|".join(
            (self.run_id, self.question_id, self.backend, "+".join(self.fixes), str(self.trial))
        )

    @property
    def trace_id(self) -> str:
        """32 lowercase hex characters (a W3C trace id), the same for the same key."""
        return hashlib.sha256(self.seed.encode("utf-8")).hexdigest()[:32]

    @property
    def name(self) -> str:
        return f"{self.question_id} {self.backend} trial {self.trial}"

    @property
    def tags(self) -> list[str]:
        return [self.backend, *self.fixes]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "question_id": self.question_id,
            "backend": self.backend,
            "fixes": list(self.fixes),
            "trial": self.trial,
        }


@dataclass(frozen=True)
class Span:
    name: str
    kind: SpanKind
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    model: str | None = None
    request_id: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    start_offset_ms: int = 0  # from the trace start, laying spans end to end (approximate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "model": self.model,
            "request_id": self.request_id,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "metadata": dict(self.metadata),
            "start_offset_ms": self.start_offset_ms,
            "end_offset_ms": self.start_offset_ms + self.latency_ms,
        }


@dataclass(frozen=True)
class Trace:
    key: TraceKey
    question: str
    answer: AnalyticsAnswer
    execution: Execution | None
    spans: tuple[Span, ...]
    cost_usd: float
    tokens: Mapping[str, int]
    cortex_messages: int = 0
    public: bool = False
    scores: Mapping[str, Any] | None = None
    started_at: datetime | None = None


def _turn_spans(turns: Sequence[ModelTurn]) -> list[Span]:
    spans = []
    for i, t in enumerate(turns):
        u = t.usage
        spans.append(
            Span(
                name=f"turn {i}",
                kind="generation",
                latency_ms=t.latency_ms,
                tokens_in=u.tokens_in,
                tokens_out=u.tokens_out + u.tokens_reasoning,
                cost_usd=t.cost_usd,
                model=t.model_snapshot,
                request_id=t.request_id,
                output=t.text,
                error=f"refusal: {t.refusal_category}" if t.stop == "refusal" else None,
                metadata={
                    "stop": t.stop,
                    "tool_calls": [c.name for c in t.tool_calls],
                    "tokens_reasoning": u.tokens_reasoning,
                    "tokens_cached": u.tokens_cached,
                    "tokens_cache_write": u.tokens_cache_write,
                },
            )
        )
    return spans


def _step_generation_spans(answer: AnalyticsAnswer) -> list[Span]:
    spans = []
    for s in answer.steps:
        if s.tokens_in or s.tokens_out:
            spans.append(
                Span(
                    name=f"turn {len(spans)}",
                    kind="generation",
                    tokens_in=s.tokens_in,
                    tokens_out=s.tokens_out,
                    model=answer.model_snapshot,
                    metadata={"derived_from_steps": True, "first_step": s.index},
                )
            )
    return spans


def _tool_spans(answer: AnalyticsAnswer) -> list[Span]:
    return [
        Span(
            name=f"tool {s.tool}",
            kind="tool",
            latency_ms=s.latency_ms,
            input=dict(s.arguments),
            output={"rows_returned": s.rows_returned, "truncated": s.truncated},
            error=s.error,
            metadata={"step_index": s.index},
        )
        for s in answer.steps
    ]


def _execution_span(ex: Execution) -> Span:
    return Span(
        name="execute",
        kind="execution",
        latency_ms=ex.elapsed_ms,
        input=ex.sql,
        output={"rows_total": ex.rows_total, "truncated": ex.truncated, "query_id": ex.query_id},
        error=ex.error,
        metadata={"engine": ex.engine},
    )


def build_trace(
    key: TraceKey,
    question: str,
    answer: AnalyticsAnswer,
    *,
    execution: Execution | None = None,
    turns: Sequence[ModelTurn] = (),
    cost_usd: float | None = None,
    tokens: Mapping[str, int] | None = None,
    cortex_messages: int = 0,
    latency_ms: int = 0,
    public: bool = False,
    scores: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
) -> Trace:
    """One trace for one answer; `latency_ms` is the Cortex call's (agents use their turns)."""
    if answer.backend != key.backend:
        raise ValueError(
            f"trace backend {key.backend} differs from answer backend {answer.backend}"
        )
    if cost_usd is None:
        cost_usd = math.fsum(t.cost_usd for t in turns)
    if not (isinstance(cost_usd, int | float) and math.isfinite(cost_usd) and cost_usd >= 0):
        raise ValueError("cost_usd must be finite and non-negative")  # EK L-12
    if tokens is None:
        tokens = {
            "in": sum(t.usage.tokens_in for t in turns),
            "out": sum(t.usage.tokens_out for t in turns),
            "reasoning": sum(t.usage.tokens_reasoning for t in turns),
            "cached": sum(t.usage.tokens_cached for t in turns),
        }
    spans: list[Span] = []
    if key.backend in MCP_BACKENDS:
        spans += _turn_spans(turns) if turns else _step_generation_spans(answer)
        spans += _tool_spans(answer)
    else:
        spans.append(
            Span(
                name="cortex_analyst",
                kind="generation",
                latency_ms=latency_ms,
                cost_usd=float(cost_usd),
                model=answer.model_snapshot,
                request_id=answer.request_id,
                input=question,
                output={
                    "sql": answer.sql,
                    "interpretation": answer.interpretation,
                    "suggestions": list(answer.suggestions),
                },
                error=answer.error,
                metadata={"cortex_messages": cortex_messages},
            )
        )
    if execution is not None:
        spans.append(_execution_span(execution))
    offset = 0
    for i, span in enumerate(spans):
        spans[i] = replace(span, start_offset_ms=offset)
        offset += span.latency_ms
    return Trace(
        key=key,
        question=question,
        answer=answer,
        execution=execution,
        spans=tuple(spans),
        cost_usd=float(cost_usd),
        tokens=dict(tokens),
        cortex_messages=cortex_messages,
        public=public,
        scores=scores,
        started_at=started_at,
    )


def trace_from_record(
    record: Record,
    *,
    question: str | None = None,
    turns: Sequence[ModelTurn] = (),
    public: bool = False,
) -> Trace:
    """The trace of a stored record (its question text is not stored, so it may be passed)."""
    key = TraceKey(
        run_id=record.run_id,
        question_id=record.question_id,
        backend=record.backend,
        trial=record.trial,
        fixes=tuple(record.fixes),
    )
    return build_trace(
        key,
        question if question is not None else "",
        record.answer,
        execution=record.execution,
        turns=turns,
        cost_usd=record.cost_usd,
        tokens=dict(record.tokens),
        cortex_messages=record.cortex_messages,
        public=public,
        scores=record.scores.model_dump(mode="json"),
        started_at=record.timestamp,  # the record time, taken as the trace start
    )


def _answer_payload(answer: AnalyticsAnswer) -> dict[str, Any]:
    return {
        "backend": answer.backend,
        "model_snapshot": answer.model_snapshot,
        "text": answer.answer,
        "sql": answer.sql,
        "clarification": answer.clarification,
        "suggestions": list(answer.suggestions),
        "interpretation": answer.interpretation,
        "error": answer.error,
        "request_id": answer.request_id,
        "trace_url": answer.trace_url,
        "chart": answer.model_dump(mode="json")["chart"],
    }


def _execution_payload(ex: Execution | None) -> dict[str, Any] | None:
    if ex is None:
        return None
    return {
        "engine": ex.engine,
        "sql": ex.sql,
        "rows_total": ex.rows_total,
        "truncated": ex.truncated,
        "query_id": ex.query_id,
        "elapsed_ms": ex.elapsed_ms,
        "error": ex.error,
        "columns": list(ex.result.columns) if ex.result is not None else None,
        "rows_sample": (
            [list(r) for r in ex.result.rows[:EXPORT_ROWS]] if ex.result is not None else None
        ),
    }


def trace_payload(trace: Trace) -> dict[str, Any]:
    """The whole trace as JSON-ready data, before redaction."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trace",
        "trace_id": trace.key.trace_id,
        "trace_key": trace.key.as_dict(),
        "started_at": trace.started_at.isoformat() if trace.started_at else None,
        "public": trace.public,
        "question": trace.question,
        "answer": _answer_payload(trace.answer),
        "steps": [s.model_dump(mode="json") for s in trace.answer.steps],
        "execution": _execution_payload(trace.execution),
        "costs": {
            "cost_usd": trace.cost_usd,
            "tokens": dict(trace.tokens),
            "cortex_messages": trace.cortex_messages,
        },
        "spans": [s.as_dict() for s in trace.spans],
        "scores": dict(trace.scores) if trace.scores is not None else None,
    }


# -- tracers (ANS-03) -------------------------------------------------------------------------


class TracingUnavailable(RuntimeError):
    """The tracer cannot be built: the package is missing or its keys are not set."""


class Tracer(Protocol):
    def send(self, trace: Trace) -> str | None:
        """Send one trace; returns its URL when the backend has one."""
        ...

    def flush(self) -> None: ...


class NoopTracer:
    """Sends nothing; the default when Langfuse is not configured."""

    def send(self, trace: Trace) -> str | None:
        return None

    def flush(self) -> None:
        return None


def _make_langfuse_client(environ: Mapping[str, str]) -> Any:
    try:
        import langfuse  # type: ignore[import-not-found]  # optional, not a dependency
    except ImportError as exc:
        raise TracingUnavailable(
            "the langfuse package is not installed; add it with `uv add 'langfuse>=3,<4'` "
            "or trace with NoopTracer"
        ) from exc
    missing = [n for n in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not environ.get(n)]
    if missing:
        raise TracingUnavailable("Langfuse tracing needs " + ", ".join(missing) + " set")
    kwargs: dict[str, Any] = {
        "public_key": environ["LANGFUSE_PUBLIC_KEY"],
        "secret_key": environ["LANGFUSE_SECRET_KEY"],
    }
    if environ.get("LANGFUSE_HOST"):
        kwargs["host"] = environ["LANGFUSE_HOST"]
    return langfuse.Langfuse(**kwargs)


class LangfuseTracer:
    """Sends traces to Langfuse through the v3 SDK; every value passes the redactor first."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        redactor: Redactor | None = None,
        environ: Mapping[str, str] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        env = os.environ if environ is None else environ
        self.redactor = redactor if redactor is not None else Redactor.from_environ(env)
        self.client = client if client is not None else _make_langfuse_client(env)
        self.clock_ns = clock_ns

    def send(self, trace: Trace) -> str | None:
        r = self.redactor
        key = trace.key
        payload = trace_payload(trace)
        answer = r(payload["answer"])
        meta = r(
            {
                "trace_key": payload["trace_key"],
                "costs": payload["costs"],
                "execution": {
                    k: v for k, v in (payload["execution"] or {}).items() if k != "rows_sample"
                },
                "scores": payload["scores"],
                "request_id": trace.answer.request_id,
            }
        )
        question = r(trace.question)
        total_ms = sum(sp.latency_ms for sp in trace.spans)
        root_start = self.clock_ns()
        root = self.client.start_span(
            name=r(key.name),
            trace_context={"trace_id": key.trace_id},
            input=question,
            output=answer,
            metadata=meta,
        )
        root.update_trace(
            name=r(key.name),
            session_id=r(key.run_id),
            tags=r(key.tags),
            public=bool(trace.public),
            input=question,
            output=answer,
            metadata=meta,
        )
        for span in trace.spans:
            level = "ERROR" if span.error else None
            status = r(span.error) if span.error else None
            timing: dict[str, Any] = {
                "latency_ms": span.latency_ms,
                "start_offset_ms": span.start_offset_ms,
                "end_offset_ms": span.start_offset_ms + span.latency_ms,
            }
            if trace.started_at is not None:
                begin = trace.started_at + timedelta(milliseconds=span.start_offset_ms)
                timing["start_time"] = begin.isoformat()
                timing["end_time"] = (begin + timedelta(milliseconds=span.latency_ms)).isoformat()
            span_meta = r(
                {
                    **dict(span.metadata),
                    **timing,
                    "request_id": span.request_id,
                    "cost_usd": span.cost_usd,
                }
            )
            child_start = self.clock_ns()
            if span.kind == "generation":
                child = root.start_generation(
                    name=r(span.name),
                    model=r(span.model) if span.model else None,
                    input=r(span.input),
                    output=r(span.output),
                    metadata=span_meta,
                    usage_details={"input": span.tokens_in, "output": span.tokens_out},
                    cost_details=({"total": span.cost_usd} if span.cost_usd is not None else None),
                    level=level,
                    status_message=status,
                )
            else:
                child = root.start_span(
                    name=r(span.name),
                    input=r(span.input),
                    output=r(span.output),
                    metadata=span_meta,
                    level=level,
                    status_message=status,
                )
            # v3 stamps the start when a span is created; ending it `latency` later gives
            # Langfuse the real duration even though the trace is sent after the run
            child.end(end_time=child_start + span.latency_ms * 1_000_000)
        root.end(end_time=root_start + total_ms * 1_000_000)
        return self.client.get_trace_url(trace_id=key.trace_id)

    def flush(self) -> None:
        self.client.flush()


# -- export (ANS-04) --------------------------------------------------------------------------


def export_name(key: TraceKey) -> str:
    fixes = f"-{'+'.join(key.fixes)}" if key.fixes else ""
    return f"{key.question_id}-{key.backend}{fixes}-{key.trial}.json"


def export_trace(
    data: Trace | Record,
    results_dir: str | Path,
    *,
    redactor: Redactor | None = None,
    question: str | None = None,
    public: bool = False,
) -> str:
    """Write the redacted trace to `<results_dir>/<run_id>/traces/` and return its relative path.

    The path is relative to the parent of `results_dir`, so with `results/` it reads
    `results/<run_id>/traces/<qid>-<backend>[-<fixes>]-<trial>.json`. Output is deterministic:
    sorted keys, two-space indent, UTF-8, one trailing newline, written atomically.
    """
    trace = (
        trace_from_record(data, question=question, public=public)
        if isinstance(data, Record)
        else data
    )
    r = redactor if redactor is not None else Redactor.from_environ()
    body = json.dumps(
        r(trace_payload(trace)), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    )
    root = Path(results_dir)
    folder = root / trace.key.run_id / "traces"
    folder.mkdir(parents=True, exist_ok=True)
    name = export_name(trace.key)
    tmp = folder / (name + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, folder / name)
    return Path(root.name, trace.key.run_id, "traces", name).as_posix()


def select_for_export(
    key: TraceKey, *, correct: bool, sample_every: int = 10
) -> ExportReason | None:
    """`failure` for every incorrect answer; `sample` for about 1 in `sample_every` of the rest.

    The sample is a hash of the trace id, so the same key is always picked or always skipped.
    Samples are the traces to mark public.
    """
    if isinstance(sample_every, bool) or not isinstance(sample_every, int) or sample_every < 1:
        raise ValueError("sample_every must be a positive integer")
    if not correct:
        return "failure"
    return "sample" if int(key.trace_id, 16) % sample_every == 0 else None
