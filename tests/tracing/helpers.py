"""Fixtures for the tracing tests: planted secrets and a fake Langfuse v3 client.

Planted keys are built by concatenation so the repo's secret scan never sees a key-shaped literal.
`FakeLangfuse` mirrors the v3 SDK surface `LangfuseTracer` assumes (see the module docstring of
`agentic_analytics.tracing`); its methods take keyword arguments only and reject unknown ones, so a
call outside the assumed API fails the test.
"""

from __future__ import annotations

import json
from typing import Any

from agentic_analytics.models import ModelTurn, ToolCall, Usage
from agentic_analytics.tracing import TraceKey, build_trace
from tests.schemas import factories as f

ANTHROPIC_KEY = "sk-ant-" + "a1B2c3D4" * 4
OPENAI_KEY = "sk-proj-" + "Z9y8X7w6" * 5
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB_TOKEN = "ghp_" + "x1Y2" * 10
LANGFUSE_SECRET = "sk-lf-" + "0123abcd-" * 4
ENV_SECRET = "Plain" + "Passw0rdValue"  # matches no pattern: only the env mapping can catch it
BEARER = "eyJhbGciOi" + "JIUzI1NiJ9.payload.sig123"
URL_PW = "hunter2" + "Secret"
DB_URL = "postgres://app:" + URL_PW + "@db.example.com/x"
NESTED_PW = "nested" + "Hunter77"  # under a password key inside a JSON string
PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END " + "PRIVATE KEY-----"

ALL_PLANTED = (
    ANTHROPIC_KEY,
    OPENAI_KEY,
    AWS_KEY,
    GITHUB_TOKEN,
    LANGFUSE_SECRET,
    ENV_SECRET,
    BEARER,
    URL_PW,
    "MIIEvQIBADANBg",
    NESTED_PW,
)
SECRETS = {"CLICKHOUSE_RO_PASSWORD": ENV_SECRET, "ANTHROPIC_API_KEY": ANTHROPIC_KEY}

KEY = TraceKey(run_id="r-0001", question_id="q-001", backend="claude_mcp", trial=1)


def turn(
    i: int, *, tin: int = 1000, tout: int = 50, cost: float = 0.01, text: str = ""
) -> ModelTurn:
    return ModelTurn(
        text=text,
        tool_calls=(
            ToolCall(id=f"c{i}", name="run_select_query", arguments={"query": "SELECT 1"}),
        ),
        stop="tool_use",
        usage=Usage(tokens_in=tin, tokens_out=tout, tokens_reasoning=5, tokens_cached=100),
        cost_usd=cost,
        latency_ms=700 + i,
        model_snapshot="claude-sonnet-5-5-20261101",
        request_id=f"req_{i}",
    )


def leaky_trace(public: bool = False):
    """A trace with a planted secret in every free-text place a model or a driver can fill."""
    steps = [
        f.step(
            0,
            arguments={
                "query": f"SELECT '{ANTHROPIC_KEY}'",
                "headers": {"Authorization": f"Bearer {BEARER}"},
            },
        ),
        f.step(
            1,
            arguments={"query": "SELECT 2", "note": f"{AWS_KEY} {DB_URL}"},
            error=f"failed: {GITHUB_TOKEN}",
        ),
    ]
    steps.append(
        f.step(
            2,
            arguments={
                "payload": json.dumps({"db": {"password": NESTED_PW}, "env": ENV_SECRET}),
                "escaped": json.dumps(json.dumps({"k": ENV_SECRET})),
            },
        )
    )
    sql = f"SELECT '{OPENAI_KEY}' AS k, 1 AS n"
    answer = f.answer(
        sql=sql,
        answer=f"key is {ENV_SECRET} and {LANGFUSE_SECRET}\n{PEM}",
        steps=steps,
    )
    from agentic_analytics.schemas import ResultSet

    execution = f.execution(
        sql=sql,
        result=ResultSet(columns=("k", "n"), rows=((OPENAI_KEY, 1),)),
        rows_total=1,
    )
    return build_trace(
        KEY,
        f"What is {ENV_SECRET}? Authorization: Basic dXNlcjpwYXNzd29yZA==",
        answer,
        execution=execution,
        turns=[turn(0, text=f"thinking {ANTHROPIC_KEY}"), turn(1)],
        public=public,
    )


class FakeObservation:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], kind: str) -> None:
        self.log = log
        self.kind = kind
        self.ended = False

    def start_span(
        self,
        *,
        name: str,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> FakeObservation:
        self.log.append(
            (
                "span",
                dict(
                    name=name,
                    input=input,
                    output=output,
                    metadata=metadata,
                    level=level,
                    status_message=status_message,
                ),
            )
        )
        return FakeObservation(self.log, "span")

    def start_generation(
        self,
        *,
        name: str,
        model: str | None = None,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
        usage_details: dict[str, int] | None = None,
        cost_details: dict[str, float] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> FakeObservation:
        self.log.append(
            (
                "generation",
                dict(
                    name=name,
                    model=model,
                    input=input,
                    output=output,
                    metadata=metadata,
                    usage_details=usage_details,
                    cost_details=cost_details,
                    level=level,
                    status_message=status_message,
                ),
            )
        )
        return FakeObservation(self.log, "generation")

    def update_trace(
        self,
        *,
        name: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        public: bool | None = None,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
    ) -> None:
        self.log.append(
            (
                "update_trace",
                dict(
                    name=name,
                    session_id=session_id,
                    tags=tags,
                    public=public,
                    input=input,
                    output=output,
                    metadata=metadata,
                ),
            )
        )

    def end(self, *, end_time: int | None = None) -> None:
        assert not self.ended, "an observation is ended once"
        self.ended = True
        self.log.append(("end", {"kind": self.kind, "end_time": end_time}))


class FakeLangfuse:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.flushed = 0

    def start_span(
        self,
        *,
        name: str,
        trace_context: dict[str, str] | None = None,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
    ) -> FakeObservation:
        self.log.append(
            (
                "root",
                dict(
                    name=name,
                    trace_context=trace_context,
                    input=input,
                    output=output,
                    metadata=metadata,
                ),
            )
        )
        return FakeObservation(self.log, "root")

    def get_trace_url(self, *, trace_id: str) -> str:
        return f"https://cloud.langfuse.com/project/p1/traces/{trace_id}"

    def flush(self) -> None:
        self.flushed += 1
