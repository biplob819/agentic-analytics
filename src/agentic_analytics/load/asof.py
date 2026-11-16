"""The as-of date (LOD-06, D-10, D-50, H2): the last complete month in the snapshot.

Rule (`last_complete_month`, D-50), with `max_date` the latest sale date in the loaded table:
the as-of date is the last day of the month **before** `max_date`'s month, always. The newest
month in the file is partly registered whatever day it ends on, so it is never counted, and a
one-day change in `max_date` cannot move the as-of date by a month. The as-of date must also be
no later than the snapshot's download date (a file dated in the future is refused).

Examples: 2026-10-31 → 2026-09-30; 2026-10-30 → 2026-09-30; 2026-10-14 → 2026-09-30;
2026-03-01 → 2026-02-28; 2024-03-15 → 2024-02-29; 2025-01-15 → 2024-12-31.

"Complete" means complete in the file, not fully registered: the Land Registry publishes
sales weeks to months after completion, so recent months stay under-counted (registration lag,
see DATA_CARD.md). A pack with `as_of_rule: fixed` uses its own `as_of`, under the same
download-date check.

`max_date` comes from the parity report's `date_range` fingerprint, which both engines agreed on;
`write_as_of` sets `as_of` in a run-config YAML file (other keys are kept; comments are not).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from agentic_analytics.load.parity import ParityError, ParityReport
from agentic_analytics.packs import Pack
from agentic_analytics.schemas.config import _UniqueKeyLoader

DATE_RANGE = "date_range"


def last_complete_month(max_date: dt.date) -> dt.date:
    """The last day of the month before `max_date`'s month (D-50)."""
    if isinstance(max_date, dt.datetime) or not isinstance(max_date, dt.date):
        raise TypeError(f"max_date must be a date, got {type(max_date).__name__}")
    return max_date.replace(day=1) - dt.timedelta(days=1)


def as_of_for(pack: Pack, max_date: dt.date, *, downloaded_on: dt.date | None = None) -> dt.date:
    """The pack's as-of date for a snapshot whose latest sale is `max_date`; with
    `downloaded_on`, an as-of date after the download date is refused (D-50)."""
    if pack.manifest.as_of_rule == "fixed":
        assert pack.manifest.as_of is not None  # DatasetPack guarantees it
        as_of = pack.manifest.as_of
    else:
        as_of = last_complete_month(max_date)
    if downloaded_on is not None and as_of > downloaded_on:
        raise ValueError(f"as-of date {as_of} is after the snapshot download date {downloaded_on}")
    return as_of


def max_date_from_parity(report: ParityReport) -> dt.date:
    """`max_date` from the `date_range` fingerprint; every engine must report the same value."""
    if not report.passed:
        raise ParityError("parity failed; no as-of date can be taken from it")
    seen: set[dt.date] = set()
    for engine in report.engines:
        rs = report.result(DATE_RANGE, engine)
        cols = [c.casefold() for c in rs.columns]
        if "max_date" not in cols or len(rs.rows) != 1:
            raise ParityError(f"{engine} {DATE_RANGE} result has no single max_date row")
        value = rs.rows[0][cols.index("max_date")]
        if not isinstance(value, str):
            raise ParityError(f"{engine} max_date is not YYYY-MM-DD text: {value!r}")
        seen.add(dt.date.fromisoformat(value))
    if len(seen) != 1:
        raise ParityError(f"engines disagree on max_date: {sorted(seen)}")
    return seen.pop()


def write_as_of(config_path: Path, as_of: dt.date) -> None:
    """Set `as_of` in a run-config YAML mapping, creating the file if needed."""
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{config_path}: top level must be a mapping")
        data = dict(loaded or {})
    data["as_of"] = as_of
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


__all__ = [
    "as_of_for",
    "last_complete_month",
    "max_date_from_parity",
    "write_as_of",
]
