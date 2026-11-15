"""What a backend returns and what execution produced (SCH-02)."""

from __future__ import annotations

from typing import Literal

from evalkit.schemas import ResultSet
from pydantic import field_validator, model_validator

from agentic_analytics.schemas._base import (
    MCP_BACKENDS,
    BackendName,
    Engine,
    FrozenMap,
    JsonValue,
    Model,
    NonBlank,
    NonNegInt,
    json_cells,
)


class AgentStep(Model):
    index: NonNegInt
    tool: NonBlank
    arguments: FrozenMap[JsonValue]
    rows_returned: NonNegInt | None = None
    truncated: bool = False
    error: str | None = None
    latency_ms: NonNegInt
    tokens_in: NonNegInt
    tokens_out: NonNegInt


class Execution(Model):
    engine: Engine
    sql: NonBlank
    result: ResultSet | None
    rows_total: NonNegInt | None
    truncated: bool
    query_id: str | None = None
    elapsed_ms: NonNegInt
    error: Literal["sql_error", "guard", "timeout", "transport"] | None = None

    @field_validator("result")
    @classmethod
    def _json_only(cls, v: ResultSet | None) -> ResultSet | None:
        return json_cells(v) if v is not None else v

    @model_validator(mode="after")
    def _result_xor_error(self) -> Execution:
        if (self.result is None) == (self.error is None):
            raise ValueError("an execution has a result exactly when it has no error")
        if self.error is not None and (self.rows_total is not None or self.truncated):
            raise ValueError("a failed execution reports no rows_total and is not truncated")
        if self.result is not None and self.rows_total is None:
            raise ValueError("a successful execution reports rows_total")
        if self.result is not None and self.rows_total is not None:
            if len(self.result.rows) > self.rows_total:
                raise ValueError("result holds more rows than rows_total")
            if self.truncated != (len(self.result.rows) < self.rows_total):
                raise ValueError("truncated must say whether rows were cut")
        return self


class AnalyticsAnswer(Model):
    backend: BackendName
    model_snapshot: NonBlank | None
    sql: NonBlank | None
    answer: str
    clarification: NonBlank | None = None
    suggestions: tuple[NonBlank, ...] = ()
    interpretation: str | None = None
    chart: FrozenMap[JsonValue] | None = None
    steps: tuple[AgentStep, ...] = ()
    request_id: str | None = None
    trace_url: str | None = None
    trace_export: str | None = None
    error: Literal["transport", "schema", "refusal", "step_budget", "timeout"] | None = None

    @property
    def asked_for_clarification(self) -> bool:
        return self.clarification is not None or (bool(self.suggestions) and self.sql is None)

    @model_validator(mode="after")
    def _consistent(self) -> AnalyticsAnswer:
        if self.backend in MCP_BACKENDS and not self.model_snapshot:
            raise ValueError("MCP agent answers record the model snapshot")
        if self.backend == "cortex_analyst" and self.steps:
            raise ValueError("Cortex answers have no agent steps")
        if [s.index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("step indexes must be 0, 1, 2, ...")
        if self.sql is not None and self.suggestions:
            raise ValueError("suggestions only accompany a request for clarification (B6)")
        if self.sql is not None and self.asked_for_clarification:
            raise ValueError("an answer is either SQL or a request for clarification, not both")
        if self.error is None and self.sql is None and not self.asked_for_clarification:
            raise ValueError("an answer without an error needs SQL or a clarification")
        if self.error is not None and (self.clarification is not None or self.suggestions):
            raise ValueError("an answer that errored carries no clarification or suggestions")
        if self.sql is None and self.chart is not None:
            raise ValueError("a chart needs executed SQL")
        return self
