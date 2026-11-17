"""Aggregates written to summary.json (SCH-04)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from evalkit.schemas import Interval
from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from agentic_analytics.schemas._base import (
    FIX_NAMES,
    BackendName,
    Category,
    FixName,
    Fraction,
    FrozenMap,
    Model,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    PosInt,
    Sha256,
    StrictFloat,
    Timestamp,
    Versioned,
)


class CategoryAccuracy(Model):
    category: Category
    n: PosInt
    accuracy: Interval  # tolerant of the OWNER D-81 equivalences (the headline)
    accuracy_strict: Interval | None = None  # strict (D-81); None in summaries made before it

    @model_serializer(mode="wrap")
    def _omit_uncomputed(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A summary made before D-81 keeps its exact format."""
        data = handler(self)
        if data.get("accuracy_strict", 0) is None:
            del data["accuracy_strict"]
        return data


class StepEfficiency(Model):
    """M4.1 MON-04: MCP tool calls per answer against each question's minimum (§6.3)."""

    answers: PosInt
    tool_calls_mean: NonNegFloat
    min_tool_calls_mean: NonNegFloat
    excess_mean: NonNegFloat  # calls beyond the minimum per answer (under it counts 0)
    at_minimum: Fraction  # share of answers at or under their minimum

    @model_validator(mode="after")
    def _consistent(self) -> StepEfficiency:
        # mean(max(0, used - min)) >= max(0, mean(used) - mean(min)), with float slack
        if self.excess_mean + 1e-9 < self.tool_calls_mean - self.min_tool_calls_mean:
            raise ValueError("excess_mean cannot be below mean calls minus mean minimum")
        if self.at_minimum == 1.0 and self.excess_mean > 0.0:
            raise ValueError("every answer at its minimum leaves no excess")
        return self


class BackendSummary(Model):
    backend: BackendName
    fixes: tuple[FixName, ...] = ()
    trials: PosInt
    accuracy: Interval  # tolerant of the OWNER D-81 equivalences (the headline)
    # OWNER D-81: the strict, pre-registered accuracy, shown beside the headline; None in
    # summaries made before it
    accuracy_strict: Interval | None = None
    # trials correct only under the tolerant score, by the equivalences they needed
    equivalence_passes: FrozenMap[NonNegInt] = Field(default_factory=dict)
    by_category: tuple[CategoryAccuracy, ...]
    clarification_rate: Interval
    false_clarification_rate: Interval
    pass_all_trials: Interval
    trial_agreement: StrictFloat
    answer_from_rows_violations: NonNegInt
    policy_violations: NonNegInt
    errors: FrozenMap[NonNegInt]
    cost_per_correct_usd: NonNegFloat | None  # None when nothing was correct
    latency_p50_ms: NonNegInt
    latency_p95_ms: NonNegInt
    # M4.1 MON-04; None for Cortex (no agent steps) and for summaries made before it existed
    step_efficiency: StepEfficiency | None = None

    @model_serializer(mode="wrap")
    def _omit_uncomputed(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A summary made before D-81 (no strict accuracy) or before MON-04 (no step
        efficiency) keeps its exact format."""
        data = handler(self)
        if data.get("step_efficiency", 0) is None:
            del data["step_efficiency"]
        if data.get("accuracy_strict", 0) is None:
            del data["accuracy_strict"]
            if not data.get("equivalence_passes"):
                data.pop("equivalence_passes", None)
        return data

    @model_validator(mode="after")
    def _consistent(self) -> BackendSummary:
        cats = [c.category for c in self.by_category]
        if len(cats) != len(set(cats)):
            raise ValueError("one accuracy per category")
        if not 0.0 <= self.trial_agreement <= 1.0:
            raise ValueError("trial_agreement is a fraction")
        if self.latency_p50_ms > self.latency_p95_ms:
            raise ValueError("p50 latency cannot exceed p95")
        if list(self.fixes) != sorted(set(self.fixes), key=FIX_NAMES.index):
            raise ValueError("fixes must be unique and ordered")
        if self.backend == "cortex_analyst" and self.fixes:
            raise ValueError("fixes apply to the MCP agents only")
        if self.backend == "cortex_analyst" and self.step_efficiency is not None:
            raise ValueError("Cortex answers have no agent steps to measure")
        return self


class FixUplift(Model):
    backend: BackendName
    fix: FixName | Literal["all"]
    delta_points: Interval
    p_value: StrictFloat
    separated: bool

    @model_validator(mode="after")
    def _consistent(self) -> FixUplift:
        if not 0.0 <= self.p_value <= 1.0:
            raise ValueError("p_value is a probability")
        if self.backend == "cortex_analyst":
            raise ValueError("fixes apply to the MCP agents only")
        if self.separated and self.delta_points.lo <= 0.0 <= self.delta_points.hi:
            raise ValueError("a separated uplift's interval excludes zero")
        return self


class RunSummary(Versioned):
    run_id: NonBlank
    config_hash: Sha256
    snapshot_sha256: Sha256
    as_of: date
    gold_check_id: Sha256
    pack: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,40}@[0-9]+\.[0-9]+(\.[0-9]+)?$")
    ]
    gold_mode: Literal["cross_engine", "single_engine"]
    backends: tuple[BackendSummary, ...]
    uplifts: tuple[FixUplift, ...] = ()
    probes: FrozenMap[FrozenMap[bool]] = Field(default_factory=dict)  # probe -> backend -> pass
    cost_status: Literal["estimated", "reconciled"]
    total_cost_usd: NonNegFloat
    total_credits: FrozenMap[NonNegFloat]
    started_at: Timestamp
    finished_at: Timestamp | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RunSummary:
        keys = [(b.backend, b.fixes) for b in self.backends]
        if len(keys) != len(set(keys)):
            raise ValueError("one summary per (backend, fixes)")
        ups = [(u.backend, u.fix) for u in self.uplifts]
        if len(ups) != len(set(ups)):
            raise ValueError("one uplift per (backend, fix)")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self
