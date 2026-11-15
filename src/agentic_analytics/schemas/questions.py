"""Benchmark inputs: questions, probes, gold results (SCH-01)."""

from __future__ import annotations

from typing import Annotated, Literal

from evalkit.schemas import ResultSet
from pydantic import Field, StringConstraints, field_validator, model_validator

from agentic_analytics.schemas._base import (
    ENGINES,
    BackendName,
    Category,
    Engine,
    FrozenMap,
    Model,
    NonBlank,
    NonNegInt,
    PosInt,
    Sha256,
    Timestamp,
    Versioned,
    json_cells,
)

SqlByEngine = FrozenMap[NonBlank]


def _both_engines(sql: dict[str, str], what: str) -> None:
    if set(sql) != set(ENGINES):
        raise ValueError(f"{what} must name exactly {', '.join(ENGINES)}, got {sorted(sql)}")


class Tolerance(Model):
    rel: Annotated[float, Field(ge=0.0, lt=1.0, allow_inf_nan=False, strict=True)] = 1e-6
    abs: Annotated[float, Field(ge=0.0, allow_inf_nan=False, strict=True)] = 1e-9


class Question(Versioned):
    id: Annotated[str, StringConstraints(pattern=r"^q-[0-9]{3}$")]
    text: NonBlank
    category: Category
    expected: Literal["answer", "clarify"]
    gold_sql: SqlByEngine | None = None
    interpretations: tuple[SqlByEngine, ...] = ()
    tolerance: Tolerance = Tolerance()
    ordered: bool = False
    top_n: PosInt | None = None
    expects_empty: bool = False
    author: Literal["claude", "gpt", "owner"]
    owner_reviewed: bool

    @model_validator(mode="after")
    def _consistent(self) -> Question:
        if (self.expected == "clarify") != (self.category == "ambiguous"):
            raise ValueError("expected must be 'clarify' exactly for ambiguous questions")
        if self.expected == "clarify":
            if self.gold_sql is not None:
                raise ValueError("a clarify-expected question has no gold_sql")
            readings = {
                tuple(sorted((e, " ".join(q.split())) for e, q in i.items()))
                for i in self.interpretations
            }
            if len(readings) < 2:
                raise ValueError(
                    "an ambiguous question needs at least two distinct interpretations"
                )
            for i, interp in enumerate(self.interpretations):
                _both_engines(interp, f"interpretation {i}")
        else:
            if self.gold_sql is None:
                raise ValueError("an answer-expected question needs gold_sql")
            _both_engines(self.gold_sql, "gold_sql")
            if self.interpretations:
                raise ValueError("only ambiguous questions carry interpretations")
        if (self.top_n is not None) != (self.category == "top_n"):
            raise ValueError("top_n is set exactly for top_n questions")
        return self


class Probe(Versioned):
    id: Annotated[str, StringConstraints(pattern=r"^p-(w|iq|id)[0-9]{2}$")]
    kind: Literal["write", "injection_question", "injection_data"]
    text: NonBlank
    backends: Annotated[tuple[BackendName, ...], Field(min_length=1)]
    expected: Literal["refuse", "answer_ignoring_injection"]
    gold_sql: SqlByEngine | None = None
    injection_marker: NonBlank | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Probe:
        prefix = {"write": "p-w", "injection_question": "p-iq", "injection_data": "p-id"}
        if not self.id.startswith(prefix[self.kind]) or self.id[len(prefix[self.kind])].isalpha():
            raise ValueError(f"probe id {self.id} does not match kind {self.kind}")
        if len(set(self.backends)) != len(self.backends):
            raise ValueError("backends must be unique")
        if self.kind == "write":
            if self.expected != "refuse" or self.injection_marker is not None:
                raise ValueError("write probes expect a refusal and carry no marker")
        else:
            if self.expected != "answer_ignoring_injection":
                raise ValueError("injection probes expect an answer that ignores the injection")
            if self.injection_marker is None:
                raise ValueError("injection probes need an injection_marker")
        if self.kind == "injection_data" and "cortex_analyst" in self.backends:
            raise ValueError("the semantic view does not cover the probes table")
        if self.gold_sql is not None:
            _both_engines(self.gold_sql, "gold_sql")
        return self


class GoldResult(Versioned):
    question_id: Annotated[str, StringConstraints(pattern=r"^q-[0-9]{3}$")]
    engine: Engine
    sql_hash: Sha256
    result: ResultSet
    elapsed_ms: NonNegInt
    executed_at: Timestamp

    @field_validator("result")
    @classmethod
    def _json_only(cls, v: ResultSet) -> ResultSet:
        return json_cells(v)
