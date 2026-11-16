"""Loaders, parity and the as-of date (N1.3: LOD-01 to LOD-04, LOD-06).

Loaders run fixed SQL from the dataset pack on injected **admin** connections; they never use the
read-only executors and never take SQL from anywhere but the pack. The CLI wiring (`load`) is a
separate step.
"""

from agentic_analytics.load._common import LoaderError, LoadResult
from agentic_analytics.load.asof import (
    as_of_for,
    last_complete_month,
    max_date_from_parity,
    write_as_of,
)
from agentic_analytics.load.clickhouse import load_clickhouse
from agentic_analytics.load.parity import (
    ParityError,
    ParityReport,
    clickhouse_runner,
    executor_runner,
    require_parity,
    run_parity,
    snowflake_runner,
)
from agentic_analytics.load.probes import load_probes_clickhouse, load_probes_snowflake
from agentic_analytics.load.snapshot import SnapshotInfo, download_snapshot, within_load_window
from agentic_analytics.load.snowflake import load_snowflake

__all__ = [
    "LoadResult",
    "LoaderError",
    "ParityError",
    "ParityReport",
    "SnapshotInfo",
    "as_of_for",
    "clickhouse_runner",
    "download_snapshot",
    "executor_runner",
    "last_complete_month",
    "load_clickhouse",
    "load_probes_clickhouse",
    "load_probes_snowflake",
    "load_snowflake",
    "max_date_from_parity",
    "require_parity",
    "run_parity",
    "snowflake_runner",
    "within_load_window",
    "write_as_of",
]
