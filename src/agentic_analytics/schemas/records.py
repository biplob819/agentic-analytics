"""Per-answer scores and the shared results record (SCH-03)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from agentic_analytics.schemas._base import (
    ENGINE_OF,
    FIX_NAMES,
    BackendName,
    FixName,
    FrozenMap,
    GitSha,
    Model,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    PosInt,
    Sha256,
    StrictBool,
    StrictInt,
    Timestamp,
    Versioned,
)
from agentic_analytics.schemas.answers import AnalyticsAnswer, Execution


class Scores(Model):
    result_match: StrictBool | None  # None for clarify-expected questions
    clarified: StrictBool
    false_clarification: StrictBool
    correct: StrictBool
    answer_from_rows: StrictBool | None
    unsupported_numbers: tuple[str, ...] = ()
    tool_calls: NonNegInt
    over_budget: StrictBool
    write_attempted: StrictBool
    injection_followed: StrictBool | None = None
    mismatch_reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Scores:
        answer_expected = self.result_match is not None
        matched = bool(self.result_match) and not self.clarified
        want = matched if answer_expected else self.clarified
        if self.correct != want:
            raise ValueError(f"correct must be {want} given result_match and clarified")
        if self.false_clarification != (answer_expected and self.clarified):
            raise ValueError("false_clarification means clarifying on an answer-expected question")
        if self.answer_from_rows is True and self.unsupported_numbers:
            raise ValueError("answer_from_rows cannot hold with unsupported numbers")
        return self


UNSET_OMITTED = ("ask_wall_ms", "analyst_latency_ms")


class Record(Versioned):
    run_id: NonBlank
    question_id: Annotated[str, Field(pattern=r"^(q-[0-9]{3}|p-(w|iq|id)[0-9]{2})$")]
    question_hash: Sha256
    backend: BackendName
    fixes: tuple[FixName, ...] = ()
    trial: PosInt
    answer: AnalyticsAnswer
    execution: Execution | None
    scores: Scores
    tokens: FrozenMap[NonNegInt]
    cost_usd: NonNegFloat
    cortex_messages: NonNegInt
    warehouse_elapsed_ms: NonNegInt
    warehouse_credits_alloc: NonNegFloat | None = None
    # R3.1: the runner's wall time for the ask, question in to answer out (for Cortex the API
    # round trip plus the execution of its SQL); None on records made before it was measured
    ask_wall_ms: NonNegInt | None = None
    # Cortex's own time from `response_metadata.analyst_latency_ms` (A2.2), Cortex only
    analyst_latency_ms: NonNegFloat | None = None
    timestamp: Timestamp
    seed: StrictInt
    git_sha: GitSha
    prompt_hash: Sha256
    fix_hash: Sha256
    semantic_hash: Sha256
    gold_check_id: Sha256
    snapshot_sha256: Sha256

    @model_validator(mode="after")
    def _consistent(self) -> Record:
        if list(self.fixes) != sorted(set(self.fixes), key=FIX_NAMES.index):
            raise ValueError("fixes must be unique and in the order " + ", ".join(FIX_NAMES))
        if self.backend == "cortex_analyst" and self.fixes:
            raise ValueError("fixes apply to the MCP agents only")
        if self.answer.backend != self.backend:
            raise ValueError("record backend and answer backend differ")
        if (self.execution is None) != (self.answer.sql is None):
            raise ValueError("execution is present exactly when the answer has SQL")
        if self.execution is not None and self.execution.sql != self.answer.sql:
            raise ValueError("the executed SQL must be the answer's SQL")
        if self.execution is not None and self.execution.engine != ENGINE_OF[self.backend]:
            raise ValueError(f"{self.backend} answers execute on {ENGINE_OF[self.backend]}")
        s = self.scores
        if s.clarified != self.answer.asked_for_clarification:
            raise ValueError("scores.clarified must match whether the answer asked back")
        executed = self.execution is not None and self.execution.error is None
        if s.result_match and not executed:
            raise ValueError("result_match needs an execution that succeeded")
        if s.correct and self.answer.error is not None:
            raise ValueError("an answer that errored cannot be correct")
        if s.answer_from_rows is not None and not executed:
            raise ValueError("answer_from_rows is judged only against executed rows")
        if set(self.tokens) - {"in", "out", "cached", "reasoning"}:
            raise ValueError("tokens keys are in, out, cached, reasoning")
        if self.backend == "cortex_analyst" and self.cost_usd and not self.cortex_messages:
            raise ValueError("Cortex spend without a message count")
        if self.analyst_latency_ms is not None and self.backend != "cortex_analyst":
            raise ValueError("analyst_latency_ms is Cortex's own time; only Cortex records have it")
        return self

    @model_serializer(mode="wrap")
    def _omit_unmeasured(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The R3.1 timings are left out while unset, so a record without them keeps the exact
        format it had before they existed (stored records, fixtures and the bundled example)."""
        data = handler(self)
        for name in UNSET_OMITTED:
            if data.get(name, 0) is None:
                del data[name]
        return data
