"""Base model and strict field types, mirroring EvalKit's schema base (EK L-8, L-9).

Models are deeply immutable: sequences are tuples, mappings become `FrozenDict`, free-form JSON is
frozen recursively, and floats must be finite.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal, NoReturn, TypeVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

SCHEMA_VERSION = "1.0"

Engine = Literal["clickhouse", "snowflake"]
BackendName = Literal["claude_mcp", "gpt_mcp", "cortex_analyst"]
Category = Literal["aggregation", "time", "geography", "top_n", "yoy", "ambiguous"]
FixName = Literal["descriptions", "examples", "self_check"]

ENGINES: tuple[Engine, ...] = ("clickhouse", "snowflake")
MCP_BACKENDS: tuple[BackendName, ...] = ("claude_mcp", "gpt_mcp")
FIX_NAMES: tuple[FixName, ...] = ("descriptions", "examples", "self_check")
ENGINE_OF: dict[str, Engine] = {
    "claude_mcp": "clickhouse",
    "gpt_mcp": "clickhouse",
    "cortex_analyst": "snowflake",
}


class FrozenDict(dict):
    """A dict that refuses mutation and can be hashed."""

    def _blocked(self, *_: Any, **__: Any) -> NoReturn:
        raise TypeError("schema objects are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    # pickle and deepcopy rebuild through the constructor, never through __setitem__
    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (dict(self),))

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        import copy

        return type(self)({k: copy.deepcopy(v, memo) for k, v in self.items()})


def deep_freeze(value: Any) -> Any:
    """Freeze a JSON value: lists become tuples, objects FrozenDicts with string keys.

    Anything that would not survive a JSON round trip unchanged is rejected: sets, bytes,
    dates, non-string keys, arbitrary objects, NaN and infinity.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and infinity are not allowed")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenDict({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(deep_freeze(v) for v in value)
    raise ValueError(f"{type(value).__name__} is not a JSON value")


def json_cells(rs: Any) -> Any:
    """Reject result-set cells that would not survive a JSON round trip (Decimal, date, bytes).

    Executors convert driver values before building a ResultSet (N1.2); this is the backstop.
    """
    for row in rs.rows:
        for cell in row:
            if cell is None or isinstance(cell, bool | str):
                continue
            if isinstance(cell, int):
                continue
            if isinstance(cell, float) and math.isfinite(cell):
                continue
            raise ValueError(f"result cell {cell!r} ({type(cell).__name__}) is not a JSON scalar")
    return rs


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    return FrozenDict(value)


NonNegInt = Annotated[int, Field(ge=0, strict=True)]
PosInt = Annotated[int, Field(ge=1, strict=True)]
NonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False, strict=True)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False, strict=True)]
StrictFloat = Annotated[float, Field(allow_inf_nan=False, strict=True)]
StrictInt = Annotated[int, Field(strict=True)]
NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Name = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(.*\S)?$")]  # no edge spaces
StrictBool = Annotated[bool, Field(strict=True)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
Timestamp = AwareDatetime
JsonValue = Annotated[Any, AfterValidator(deep_freeze)]

V = TypeVar("V")
FrozenMap = Annotated[dict[str, V], AfterValidator(_freeze_mapping)]


class Model(BaseModel):
    """Frozen, strict base: unknown fields are errors, objects immutable, floats finite."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Versioned(Model):
    """A top-level file record; carries the results-format version (SCH-06)."""

    schema_version: Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+$")]

    @field_validator("schema_version")
    @classmethod
    def _same_major(cls, v: str) -> str:
        if v.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(f"results format {v} is not compatible (expects 1.x)")
        return v
