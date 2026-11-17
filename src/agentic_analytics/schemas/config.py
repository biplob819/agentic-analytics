"""Run configuration and its YAML loader (SCH-05)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

from agentic_analytics.schemas._base import (
    MCP_BACKENDS,
    BackendName,
    FixName,
    FrozenMap,
    Model,
    NonBlank,
    PosInt,
    StrictInt,
)

# A Snowflake region as `SELECT CURRENT_REGION()` returns it (`AWS_AP_SOUTHEAST_7`), optionally
# with its region group (`PUBLIC.AWS_US_WEST_2`).
SNOWFLAKE_REGION_PATTERN = r"^(?:[A-Za-z][A-Za-z0-9_]{0,31}\.)?[A-Za-z][A-Za-z0-9_]{1,63}$"


class RunConfig(Model):
    backends: Annotated[tuple[BackendName, ...], Field(min_length=1)]
    models: FrozenMap[NonBlank] = Field(default_factory=dict)
    fixes: tuple[FixName, ...] = ()
    trials: PosInt = 3
    max_tool_calls: PosInt = 6
    max_turns: PosInt = 10
    row_cap_model: PosInt = 50
    row_cap_exec: PosInt = 10_000
    as_of: date
    effort: Literal["low", "medium", "high"] = "medium"  # same for both agents (EvalKit D-24)
    seed: StrictInt = 0
    max_cost_usd: Annotated[float, Field(gt=0.0, allow_inf_nan=False, strict=True)] | None = None
    concurrency: FrozenMap[PosInt] = Field(default_factory=dict)
    # OWNER D-62: the Snowflake account's region, for the Cortex latency caveat. Left out, the
    # runner reads `SELECT CURRENT_REGION()` through the read-only executor at run start.
    snowflake_region: Annotated[str, Field(pattern=SNOWFLAKE_REGION_PATTERN)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RunConfig:
        if len(set(self.backends)) != len(self.backends):
            raise ValueError("backends must be unique")
        missing = [b for b in self.backends if b in MCP_BACKENDS and b not in self.models]
        if missing:
            raise ValueError(f"models must name a model for {', '.join(missing)}")
        stray = set(self.models) - set(MCP_BACKENDS)
        if stray:
            raise ValueError(f"models are only for MCP backends, not {', '.join(sorted(stray))}")
        unknown = set(self.concurrency) - set(self.backends)
        if unknown:
            raise ValueError(f"concurrency names unknown backends {sorted(unknown)}")
        if len(set(self.fixes)) != len(self.fixes):
            raise ValueError("fixes must be unique")
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_region(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """`snowflake_region` is left out while unset, so a config without it dumps (and hashes,
        `config_hash`) exactly as it did before the field existed."""
        data = handler(self)
        if data.get("snowflake_region", "") is None:
            del data["snowflake_region"]
        return data


class ConfigError(ValueError):
    """A config file could not be read or validated; the message names the file and field."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


MERGE_TAG = "tag:yaml.org,2002:merge"


def _check_unique(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool) -> None:
    """Duplicate keys are an error within any one mapping, merged mappings included; at most
    one `<<` per mapping. Across a merge, explicit keys may override merged ones (YAML rules)."""
    seen: set[Any] = set()
    merges = 0
    for key_node, value_node in node.value:
        if key_node.tag == MERGE_TAG:
            merges += 1
            if merges > 1:
                raise yaml.constructor.ConstructorError(
                    None, None, "more than one '<<' merge key", key_node.start_mark
                )
            subs = value_node.value if isinstance(value_node, yaml.SequenceNode) else [value_node]
            for sub in subs:
                if isinstance(sub, yaml.MappingNode):
                    _check_unique(loader, sub, deep)
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    _check_unique(loader, node, deep)
    loader.flatten_mapping(node)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,  # type: ignore[arg-type]
)


def load_run_config(path: str | Path) -> RunConfig:
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8")
    except IsADirectoryError as exc:
        raise ConfigError(f"{p}: is a directory, not a config file") from exc
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read ({exc.strerror})") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{p}: not UTF-8 text") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    try:
        return RunConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(x) for x in first["loc"]) or "(config)"
        raise ConfigError(f"{p}: {field}: {first['msg']}") from exc
