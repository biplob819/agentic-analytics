"""Live proofs for N1.3 on the real trial accounts. Skipped unless AA_LIVE=1; the full load and
parity run (about 5.5 GB downloaded, about an hour in all) also needs AA_LIVE_LOAD=1. They
write to `docs/measurements/` and use the ADMIN credentials from `.env`."""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

import pytest

from agentic_analytics.load import (
    as_of_for,
    clickhouse_runner,
    load_clickhouse,
    load_probes_clickhouse,
    load_probes_snowflake,
    load_snowflake,
    max_date_from_parity,
    require_parity,
    run_parity,
    snowflake_runner,
)
from agentic_analytics.load.clickhouse import admin_client_from_env
from agentic_analytics.load.snowflake import admin_connection_from_env
from agentic_analytics.packs import load_pack

ROOT = Path(__file__).resolve().parents[2]
LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="live: set AA_LIVE=1")
LIVE_LOAD = pytest.mark.skipif(
    os.environ.get("AA_LIVE_LOAD") != "1", reason="full load: set AA_LIVE_LOAD=1 too"
)


@pytest.fixture(autouse=True)
def _admin_env() -> None:
    """The admin credentials come from the repo's `.env` (never printed), as in the other live
    tests; offline runs never read it."""
    if os.environ.get("AA_LIVE") == "1":
        from agentic_analytics.env import load_dotenv

        load_dotenv(ROOT / ".env")


@pytest.mark.live
@LIVE
@LIVE_LOAD
def test_live_load_both_engines_parity_and_as_of() -> None:  # pragma: no cover - real accounts
    """The N1.3 measurement: ClickHouse url() load, Snowflake download within the hour (D-11),
    snapshot record, parity stamped with the snapshot sha256, and the as-of date (D-50)."""
    pack = load_pack("uk_price_paid")
    ch = load_clickhouse(pack, admin_client_from_env())
    assert ch.rows_after["uk.uk_price_paid"] > 27_000_000
    sf = load_snowflake(
        pack,
        admin_connection_from_env(),
        workdir=ROOT / "data" / "snapshots",
        force=True,  # always a fresh download, so the sha256 belongs to this load
        clickhouse_loaded_at=ch.finished_at,
    )
    sha = sf.snapshot["sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    report = run_parity(
        pack,
        {
            "clickhouse": clickhouse_runner(admin_client_from_env()),
            "snowflake": snowflake_runner(admin_connection_from_env()),
        },
        snapshot_sha256=sha,
        out_dir=ROOT / "docs" / "measurements",
    )
    require_parity(report)
    downloaded = dt.datetime.fromisoformat(sf.snapshot["downloaded_at"]).date()
    as_of = as_of_for(pack, max_date_from_parity(report), downloaded_on=downloaded)
    assert as_of < max_date_from_parity(report)


@pytest.mark.live
@LIVE
def test_live_probes_on_both_engines() -> None:  # pragma: no cover - needs real accounts
    pack = load_pack("uk_price_paid")
    assert load_probes_clickhouse(pack, admin_client_from_env()).rows_after == {
        "uk_probes.listing_notes": 10
    }
    assert load_probes_snowflake(pack, admin_connection_from_env()).rows_after == {
        "ANALYTICS.UK_PROBES.listing_notes": 10
    }
