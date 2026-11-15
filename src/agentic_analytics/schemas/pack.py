"""Dataset-pack manifest, `pack.yaml` (SCH-07, OWNER D-29)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, model_validator

from agentic_analytics.schemas._base import (
    Category,
    Engine,
    FrozenMap,
    Name,
    NonBlank,
    PosInt,
    Versioned,
)

PackName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,40}$")]
_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
CATEGORIES: tuple[str, ...] = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")


class DatasetPack(Versioned):
    name: PackName
    version: Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")]
    title: NonBlank
    licence: NonBlank
    attribution: NonBlank
    source_url: Annotated[str, StringConstraints(pattern=r"^https?://[^\s\"'<>`]+$")]
    engines: Annotated[tuple[Engine, ...], Field(min_length=1)]
    databases: FrozenMap[NonBlank]
    tables: Annotated[tuple[Name, ...], Field(min_length=1)]
    as_of_rule: Literal["last_complete_month", "fixed"]
    as_of: date | None = None
    quotas: FrozenMap[PosInt]

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @model_validator(mode="after")
    def _consistent(self) -> DatasetPack:
        if len(set(self.engines)) != len(self.engines):
            raise ValueError("engines must be unique")
        if set(self.databases) != set(self.engines):
            raise ValueError("databases must name exactly the pack's engines")
        if (self.as_of is not None) != (self.as_of_rule == "fixed"):
            raise ValueError("as_of is given exactly when as_of_rule is 'fixed'")
        unknown = set(self.quotas) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"quotas name unknown categories {sorted(unknown)}")
        host = urlsplit(self.source_url).hostname or ""
        labels = host.split(".")
        if len(labels) < 2 or not all(_LABEL.fullmatch(lb) for lb in labels):
            raise ValueError(f"source_url has no valid host name: {self.source_url!r}")
        if len({t.casefold() for t in self.tables}) != len(self.tables):
            raise ValueError("tables must be unique, ignoring case")
        return self


__all__ = ["CATEGORIES", "Category", "DatasetPack"]
