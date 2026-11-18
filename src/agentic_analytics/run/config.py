"""Run configuration for the CLI: `run.yaml`, command-line overrides, `init` templates and the
measured cost assumptions written back by `smoke` (RUN-01, RUN-05, Tech Spec §4, §9).

`run.yaml` is exactly a `RunConfig` (SCH-05), so `load_run_config` reads it and N1.3's
`write_as_of` can update its `as_of`. Paths and the pack are command-line options.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from agentic_analytics.cost import AgentAssumptions, run_fixsets
from agentic_analytics.schemas import (
    ENGINE_OF,
    FIX_NAMES,
    MCP_BACKENDS,
    BackendName,
    Engine,
    FixName,
    RunConfig,
)

ALL_BACKENDS: tuple[BackendName, ...] = ("claude_mcp", "gpt_mcp", "cortex_analyst")
DEFAULT_MODELS: dict[str, str] = {"claude_mcp": "claude-sonnet-5-5", "gpt_mcp": "gpt-6.1-sol"}
DEFAULT_CONCURRENCY: dict[str, int] = {"claude_mcp": 4, "gpt_mcp": 4, "cortex_analyst": 2}
ASSUMPTIONS_FILE = "cost-assumptions.json"


class RunConfigError(ValueError):
    """A configuration problem the operator must fix (exit code 2)."""


def parse_backends(
    spec: str, configured: Sequence[Engine] | None = None
) -> tuple[BackendName, ...]:
    """`all`, or a comma-separated list of backend names. `all` keeps the backends whose engine
    is configured (single-engine mode, D-29) when `configured` is given."""
    spec = spec.strip()
    if spec == "all":
        out: tuple[BackendName, ...] = ALL_BACKENDS
        if configured is not None:
            out = tuple(b for b in ALL_BACKENDS if ENGINE_OF[b] in configured)
            if not out:
                raise RunConfigError("no engine is configured: set the variables in .env.example")
        return out
    names = [s.strip().replace("-", "_") for s in spec.split(",") if s.strip()]
    bad = [n for n in names if n not in ALL_BACKENDS]
    if bad or not names:
        raise RunConfigError(
            f"unknown backend {bad or spec!r}; use all or {', '.join(ALL_BACKENDS)}"
        )
    if len(set(names)) != len(names):
        raise RunConfigError("a backend is named twice")
    chosen: tuple[BackendName, ...] = tuple(b for b in ALL_BACKENDS if b in names)
    if configured is not None:
        missing = [b for b in chosen if ENGINE_OF[b] not in configured]
        if missing:
            raise RunConfigError(
                f"{', '.join(missing)} needs {ENGINE_OF[missing[0]]}, which is not configured"
            )
    return chosen


def parse_fixes(spec: str) -> tuple[FixName, ...]:
    """`none`, `all`, or a comma-separated list of descriptions, examples, self-check. Each fix
    runs alone against the same baseline (§7), so the fix sets are () plus one per fix."""
    spec = spec.strip()
    if spec == "none":
        return ()
    if spec == "all":
        return FIX_NAMES
    names = [s.strip().replace("-", "_") for s in spec.split(",") if s.strip()]
    bad = [n for n in names if n not in FIX_NAMES]
    if bad or not names:
        raise RunConfigError(
            f"unknown fix {bad or spec!r}; use none, all, descriptions, examples or self-check"
        )
    return tuple(f for f in FIX_NAMES if f in names)


def fixsets(cfg: RunConfig) -> tuple[tuple[FixName, ...], ...]:
    """The fix sets a run asks: see `cost.run_fixsets`, which the estimate shares."""
    return run_fixsets(cfg)


def with_overrides(
    cfg: RunConfig,
    *,
    backends: Sequence[BackendName] | None = None,
    fixes: Sequence[FixName] | None = None,
    trials: int | None = None,
    max_cost_usd: float | None = None,
) -> RunConfig:
    """A validated copy (never `model_copy`, which skips validation, D-32)."""
    data = cfg.model_dump()
    if backends is not None:
        data["backends"] = tuple(backends)
        models = dict(data.get("models") or {})
        for b in backends:
            if b in MCP_BACKENDS and b not in models:
                models[b] = DEFAULT_MODELS[b]
        data["models"] = {k: v for k, v in models.items() if k in backends}
        data["concurrency"] = {
            k: v for k, v in dict(data.get("concurrency") or {}).items() if k in backends
        }
    if fixes is not None:
        data["fixes"] = tuple(fixes)
    if trials is not None:
        data["trials"] = trials
    if max_cost_usd is not None:
        data["max_cost_usd"] = float(max_cost_usd)
    try:
        return RunConfig.model_validate(data)
    except ValueError as exc:
        raise RunConfigError(str(exc)) from exc


def concurrency_for(cfg: RunConfig, backend: BackendName) -> int:
    return int(cfg.concurrency.get(backend, DEFAULT_CONCURRENCY[backend]))


# -- measured cost assumptions (RUN-05) ------------------------------------------------------


def save_assumptions(path: Path, measured: Mapping[str, AgentAssumptions]) -> Path:
    data = {b: asdict(a) for b, a in sorted(measured.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def load_assumptions(path: Path) -> dict[str, AgentAssumptions]:
    """Measured per-question token assumptions from the last smoke run; {} if there are none."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunConfigError(f"{path}: not readable JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RunConfigError(f"{path}: top level must be a mapping")
    names = {f.name for f in fields(AgentAssumptions)}
    out: dict[str, AgentAssumptions] = {}
    for backend, entry in data.items():
        if backend not in MCP_BACKENDS or not isinstance(entry, dict) or set(entry) != names:
            raise RunConfigError(f"{path}: bad entry {backend!r}")
        try:
            out[backend] = AgentAssumptions(**entry)
        except (TypeError, ValueError) as exc:
            raise RunConfigError(f"{path}: {backend}: {exc}") from exc
    return out


def measure_assumptions(
    turns: Mapping[str, Sequence[tuple[int, Mapping[str, int]]]],
) -> dict[str, AgentAssumptions]:
    """Average per-question figures from (model turns, token totals) per MCP backend."""
    out: dict[str, AgentAssumptions] = {}
    for backend, items in sorted(turns.items()):
        items = [(n, t) for n, t in items if n > 0]
        if not items:
            continue
        n_turns = sum(n for n, _ in items)
        total = {k: sum(int(t.get(k, 0)) for _, t in items) for k in ("in", "out", "cached")}
        reasoning = sum(int(t.get("reasoning", 0)) for _, t in items)
        cached_share = total["cached"] / total["in"] if total["in"] else 0.0
        out[backend] = AgentAssumptions(
            turns=round(n_turns / len(items), 3),
            prompt_tokens_per_turn=math.ceil(total["in"] / n_turns),
            cached_share=round(min(max(cached_share, 0.0), 1.0), 4),
            output_tokens_per_turn=math.ceil(total["out"] / n_turns),
            reasoning_tokens_per_turn=math.ceil(reasoning / n_turns),
        )
    return out


# -- init templates ------------------------------------------------------------------------

RUN_YAML = """\
# agentic-analytics run configuration (a RunConfig, Tech Spec §3).
# `agentic-analytics load` sets as_of to the snapshot's last complete month (D-10).
backends: [claude_mcp, gpt_mcp, cortex_analyst]
models:
  claude_mcp: claude-sonnet-5-5
  gpt_mcp: gpt-6.1-sol
fixes: []
trials: 3
max_tool_calls: 6
max_turns: 10
row_cap_model: 50
row_cap_exec: 10000
as_of: 2026-09-30
effort: medium
seed: 0
max_cost_usd: 25.0
concurrency:
  claude_mcp: 4
  gpt_mcp: 4
  cortex_analyst: 2
# The Snowflake account's region, as SELECT CURRENT_REGION() gives it, for the Cortex latency
# caveat (OWNER D-62). Left out, the run reads it through the read-only executor at start.
# snowflake_region: AWS_AP_SOUTHEAST_7
"""

# A copy of the repository's .env.example (pinned by tests/run/test_cli_run.py).
ENV_EXAMPLE = """\
# Copy to .env and fill in. Never commit .env. Tech Spec §4 lists who uses what.
# The demo host gets only the runtime keys, never the *_ADMIN_* ones.

# Models
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# ClickHouse Cloud remote MCP (API key with org role Basic Service API Reader)
CLICKHOUSE_MCP_KEY_ID=
CLICKHOUSE_MCP_KEY_SECRET=
CLICKHOUSE_SERVICE_ID=

# ClickHouse read-only executor (role analytics_ro)
CLICKHOUSE_HOST=
CLICKHOUSE_RO_USER=
CLICKHOUSE_RO_PASSWORD=

# ClickHouse admin: loader and role setup only
CLICKHOUSE_ADMIN_USER=
CLICKHOUSE_ADMIN_PASSWORD=

# ClickHouse Cloud admin API key: optional, owner-only, for `setup clickhouse` (OWNER D-55).
# clickhousectl reads it; short-lived: delete the key once setup is done. Never on the demo host.
CLICKHOUSE_CLOUD_API_KEY=
CLICKHOUSE_CLOUD_API_SECRET=

# Snowflake read-only (role ANALYTICS_RO): executor and Cortex Analyst
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_RO_USER=
SNOWFLAKE_RO_PRIVATE_KEY_PATH=
# The same key as PEM text, used only when the _PATH variable is absent (the hosted demo, D-57)
SNOWFLAKE_RO_PRIVATE_KEY=
SNOWFLAKE_WAREHOUSE=

# Snowflake admin: loader, semantic view and role setup only
SNOWFLAKE_ADMIN_USER=
SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH=

# Tracing
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=

# Demo guard
DEMO_PASSCODE=
# Owner p95 budget (OWNER D-53): a different secret from DEMO_PASSCODE; never give to visitors
DEMO_OWNER_PASSCODE=
DEMO_OWNER_CAP_USD=
EVALKIT_DEMO_DATABASE_URL=
# Demo live compute (OWNER D-59): empty = saved answers only; else a comma list of
# claude_mcp, gpt_mcp, cortex_analyst, or all
DEMO_LIVE_BACKENDS=
# Owner-request link shown while live is off (https only, never an email); default: GitHub Issues
DEMO_CONTACT_URL=
"""


def write_init(folder: Path, *, force: bool = False) -> list[Path]:
    """Write run.yaml and .env.example into `folder`; refuse to overwrite unless `force`."""
    targets = {folder / "run.yaml": RUN_YAML, folder / ".env.example": ENV_EXAMPLE}
    existing = [p for p in targets if p.exists()]
    if existing and not force:
        names = ", ".join(p.name for p in existing)
        raise RunConfigError(f"{names} already exists in {folder}; pass --force to overwrite")
    for p, text in targets.items():
        p.write_text(text, encoding="utf-8", newline="\n")
    return list(targets)


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


__all__ = [
    "ALL_BACKENDS",
    "ASSUMPTIONS_FILE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MODELS",
    "ENV_EXAMPLE",
    "RUN_YAML",
    "RunConfigError",
    "concurrency_for",
    "fixsets",
    "load_assumptions",
    "measure_assumptions",
    "parse_backends",
    "parse_fixes",
    "save_assumptions",
    "with_overrides",
    "write_init",
]
