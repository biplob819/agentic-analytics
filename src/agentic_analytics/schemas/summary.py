"""Aggregates written to summary.json (SCH-04)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from evalkit.schemas import Interval
from pydantic import Field, StringConstraints, model_validator

from agentic_analytics.schemas._base import (
    FIX_NAMES,
    BackendName,
    Category,
    FixName,
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
    accuracy: Interval


class BackendSummary(Model):
    backend: BackendName
    fixes: tuple[FixName, ...] = ()
    trials: PosInt
    accuracy: Interval
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
