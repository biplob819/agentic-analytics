"""The live gold check and its gate (GLD-01..06 against the real engines; skipped offline)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from agentic_analytics.gold import configured_executors, run_gold_check
from agentic_analytics.questions import load_questions
from agentic_analytics.run.gold import current_snapshot, load_gold_check, require_gold
from tests.gold_check.fakes import BUNDLED

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")


@pytest.mark.gold
def test_bundled_gold_check_is_clean_when_present():
    """Gate 8: once `agentic-analytics gold` has written the bundled gold check, it is clean and
    its gold_check_id still matches the questions and gold files on disk."""
    check = load_gold_check(BUNDLED)
    assert check is not None, "the bundled pack ships its gold check (Q2.2)"
    require_gold(BUNDLED, load_questions(BUNDLED), check.engines, check.snapshot_sha256)
    measurements = Path(__file__).resolve().parents[2] / "docs" / "measurements"
    if any(measurements.glob("snapshot-*.json")):  # a checkout: the check is for today's snapshot
        assert check.snapshot_sha256 == current_snapshot(measurements)


@pytest.mark.live
@LIVE
def test_live_gold_check_passes_on_the_configured_engines():  # pragma: no cover - live
    from agentic_analytics.env import load_dotenv

    load_dotenv()
    executors = configured_executors()
    assert executors, "no engine variables set"
    # D-50: the snapshot of 2026-11-16 ends in September 2026, so the as-of date is 2026-08-31.
    as_of = date.fromisoformat(os.environ.get("AA_AS_OF", "2026-08-31"))
    out = run_gold_check(
        BUNDLED,
        load_questions(BUNDLED),
        executors,
        as_of=as_of,
        snapshot_sha256=current_snapshot(Path("docs/measurements")),
        write=False,
    )
    assert out.failures == ()
