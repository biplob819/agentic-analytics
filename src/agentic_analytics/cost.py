"""Cost model, pre-run estimate, spend cap and ledger (N1.6, Tech Spec §9).

Token prices use EvalKit's `Price` and `call_cost` (pinned, D-2). Every money value is checked
finite and non-negative before it can affect the cap (EK L-12).
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml
from evalkit.cost import Price, call_cost, load_prices, price_for

from agentic_analytics.schemas import FIX_NAMES, MCP_BACKENDS, FixName, RunConfig


def _money(x: float, what: str) -> float:
    if not isinstance(x, int | float) or isinstance(x, bool) or not math.isfinite(x) or x < 0:
        raise ValueError(f"{what} must be a finite, non-negative number, got {x!r}")
    return float(x)


def model_prices(path: Path | None = None) -> dict[str, Price]:
    if path is None:
        with resources.as_file(resources.files("agentic_analytics") / "prices.yaml") as p:
            return load_prices(p)
    return load_prices(path)


@dataclass(frozen=True)
class Rate:
    value: float
    verified: bool
    source: str


def credit_rates(path: Path | None = None) -> dict[str, Rate]:
    if path is None:
        text = (resources.files("agentic_analytics") / "credits.yaml").read_text("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("credits.yaml must be a mapping of rate name to {value, verified, source}")
    unknown = set(data) - RATE_NAMES
    missing = RATE_NAMES - set(data)
    if unknown or missing:
        raise ValueError(f"credits.yaml: unknown {sorted(unknown)}, missing {sorted(missing)}")
    out: dict[str, Rate] = {}
    for key, entry in data.items():
        if not isinstance(entry, dict) or set(entry) != {"value", "verified", "source"}:
            raise ValueError(f"credits.yaml: {key} needs exactly value, verified, source")
        if not isinstance(entry["verified"], bool):
            raise ValueError(f"credits.yaml: {key}.verified must be true or false, not quoted")
        if not isinstance(entry["source"], str) or not entry["source"].strip():
            raise ValueError(f"credits.yaml: {key}.source must be a non-empty string")
        out[key] = Rate(_money(entry["value"], key), entry["verified"], entry["source"])
    return out


RATE_NAMES = frozenset(
    {
        "cortex_analyst_credits_per_message",
        "snowflake_xs_credits_per_hour",
        "snowflake_usd_per_credit",
        "clickhouse_usd_per_hour",
    }
)


@dataclass(frozen=True)
class AgentAssumptions:
    """Planning numbers per question; replaced by measured averages after the smoke run."""

    turns: float = 6.0
    prompt_tokens_per_turn: int = 3000
    cached_share: float = 0.6
    output_tokens_per_turn: int = 250
    reasoning_tokens_per_turn: int = 150

    def __post_init__(self) -> None:
        if not 0 <= self.cached_share <= 1:
            raise ValueError("cached_share is a fraction")
        for name in (
            "turns",
            "prompt_tokens_per_turn",
            "output_tokens_per_turn",
            "reasoning_tokens_per_turn",
        ):
            _money(getattr(self, name), name)


@dataclass(frozen=True)
class EstimateLine:
    backend: str
    asks: int
    usd: float
    credits: float
    note: str = ""


@dataclass(frozen=True)
class RunEstimate:
    lines: tuple[EstimateLine, ...]
    usd_per_credit: float
    unverified: tuple[str, ...]

    @property
    def model_usd(self) -> float:
        return math.fsum(line.usd for line in self.lines if line.backend != "clickhouse")

    @property
    def compute_usd(self) -> float:
        return math.fsum(line.usd for line in self.lines if line.backend == "clickhouse")

    @property
    def credits(self) -> float:
        return math.fsum(line.credits for line in self.lines)

    @property
    def total_usd(self) -> float:
        return self.model_usd + self.compute_usd + self.credits * self.usd_per_credit


def agent_question_cost(price: Price, a: AgentAssumptions) -> float:
    per_turn = call_cost(
        price,
        tokens_in=a.prompt_tokens_per_turn,
        tokens_cached=int(a.prompt_tokens_per_turn * a.cached_share),
        tokens_out=a.output_tokens_per_turn,
        tokens_reasoning=a.reasoning_tokens_per_turn,
    )
    return per_turn * a.turns


def run_fixsets(cfg: RunConfig) -> tuple[tuple[FixName, ...], ...]:
    """The baseline, then each configured fix alone, then, when every fix is configured
    (`--fix all`), all fixes together: §7 records an `all` run, never a headline figure, and
    D-34 puts it in the Holm family. Two-fix combinations are never run (D-34)."""
    fixes = cast("tuple[FixName, ...]", tuple(cfg.fixes))
    sets: list[tuple[FixName, ...]] = [(), *((f,) for f in fixes)]
    if fixes == FIX_NAMES:
        sets.append(fixes)
    return tuple(sets)


def estimate_run(
    cfg: RunConfig,
    n_questions: int,
    *,
    prices: dict[str, Price] | None = None,
    rates: dict[str, Rate] | None = None,
    assumptions: dict[str, AgentAssumptions] | None = None,
    warehouse_seconds_per_query: float = 2.0,
    queries_per_agent_question: float = 4.0,
) -> RunEstimate:
    """Spend for one run: every backend × question × fix set × trial (Tech Spec §9)."""
    if not isinstance(n_questions, int) or isinstance(n_questions, bool) or n_questions < 0:
        raise ValueError("n_questions must be a non-negative integer")
    _money(warehouse_seconds_per_query, "warehouse_seconds_per_query")
    _money(queries_per_agent_question, "queries_per_agent_question")
    prices = prices if prices is not None else model_prices()
    rates = rates if rates is not None else credit_rates()
    assumptions = assumptions or {}
    fixsets = len(run_fixsets(cfg))  # the baseline, each fix alone and `all` (run/config.py)
    lines: list[EstimateLine] = []
    unverified = sorted(k for k, r in rates.items() if not r.verified)
    agent_asks = 0
    for backend in cfg.backends:
        if backend in MCP_BACKENDS:
            model = cfg.models[backend]
            a = assumptions.get(backend, AgentAssumptions())
            asks = n_questions * cfg.trials * fixsets
            usd = asks * agent_question_cost(price_for(prices, model), a)
            lines.append(EstimateLine(backend, asks, usd, 0.0, model))
            agent_asks += asks
        else:
            asks = n_questions * cfg.trials  # fixes apply to the MCP agents only
            msg = asks * rates["cortex_analyst_credits_per_message"].value
            wh = (
                asks
                * warehouse_seconds_per_query
                / 3600
                * rates["snowflake_xs_credits_per_hour"].value
            )
            lines.append(
                EstimateLine(backend, asks, 0.0, msg + wh, "Cortex messages + XS warehouse")
            )
    if agent_asks:
        hours = agent_asks * queries_per_agent_question * warehouse_seconds_per_query / 3600
        ch_usd = hours * rates["clickhouse_usd_per_hour"].value
        lines.append(EstimateLine("clickhouse", agent_asks, ch_usd, 0.0, "ClickHouse compute"))
    return RunEstimate(tuple(lines), rates["snowflake_usd_per_credit"].value, tuple(unverified))


class CostCapReached(Exception):
    """The next call would take actual plus in-flight spend past the cap (exit code 3)."""


@dataclass(frozen=True)
class Reservation:
    """Issued by one SpendCap; settled exactly once by that cap."""

    id: int
    amount: float


@dataclass
class SpendCap:
    """Reserve an upper-bound estimate before a call, settle to the actual cost after.

    Check and update happen with no await in between, so asyncio tasks cannot interleave
    inside them; the lock keeps that true if an await is ever added (it is not testable now).
    """

    cap_usd: float | None
    spent: float = 0.0
    in_flight: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _open: dict[int, float] = field(default_factory=dict, repr=False)
    _next: int = 0

    def __post_init__(self) -> None:
        if self.cap_usd is not None:
            _money(self.cap_usd, "cap_usd")
            if self.cap_usd == 0:
                raise ValueError("cap_usd must be positive; use None for no cap")

    async def reserve(self, estimate_usd: float) -> Reservation:
        est = _money(estimate_usd, "estimate")
        async with self._lock:
            if self.cap_usd is not None and self.spent + self.in_flight + est > self.cap_usd:
                raise CostCapReached(
                    f"spent ${self.spent:.4f} + in flight ${self.in_flight:.4f} + next "
                    f"${est:.4f} would pass the ${self.cap_usd:.2f} cap"
                )
            self.in_flight += est
            self._next += 1
            self._open[self._next] = est
            return Reservation(self._next, est)

    async def settle(self, reservation: Reservation, actual_usd: float) -> None:
        actual = _money(actual_usd, "actual cost")
        async with self._lock:
            amount = self._open.get(reservation.id)
            if amount is None or amount != reservation.amount:
                raise ValueError("unknown, altered or already settled reservation")
            del self._open[reservation.id]
            self.in_flight = max(self.in_flight - amount, 0.0)
            self.spent += actual


def append_ledger(
    path: Path,
    *,
    kind: str,
    run_id: str,
    spent_usd: float,
    credits: dict[str, float] | None = None,
    note: str = "",
    at: datetime | None = None,
) -> dict[str, Any]:
    """Append one line to the spend ledger after a real run (MOD-06)."""
    row: dict[str, Any] = {
        "at": (at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "kind": kind,
        "run_id": run_id,
        "spent_usd": round(_money(spent_usd, "spent_usd"), 6),
        "credits": {k: round(_money(v, k), 6) for k, v in sorted((credits or {}).items())},
        "note": note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row
