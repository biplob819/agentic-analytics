"""DEM-15 (B3.3, OWNER D-93): the demo is deployed on Streamlit Community Cloud and its live
page-mode p95 over 20 owner-budget asks is under 30 s with no timed-out or errored panel. The
live run is `tests/demo/test_live.py::test_live_p95_over_20_asks_is_under_30_seconds`; these
offline tests pin the committed record it wrote."""

from __future__ import annotations

import json
import re
from pathlib import Path

from evalkit.stats import percentile_ms

from agentic_analytics.demo.p95 import DEFAULT_MAX_COST_USD
from secret_scan import scan

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/measurements/demo-p95-2026-11-20-page.json"


def record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_the_deployed_page_answered_20_owner_asks_under_30_seconds() -> None:
    r = record()
    assert r["measurement"] == "demo-p95" and r["mode"] == "page"
    assert r["target"].startswith("https://")
    day = RECORD.stem.removeprefix("demo-p95-").removesuffix("-page")
    assert r["started_at"].startswith(day) and r["finished_at"].startswith(day)
    assert r["started_at"] < r["finished_at"]
    assert r["target"].rstrip("/").endswith(".streamlit.app")  # Community Cloud (D-93)
    assert r["n_requested"] == r["n"] == 20 and r["stopped"] is None
    assert r["owner_budget"] is True and r["owner_budget_asks"] == 20
    assert r["backends"] == ["claude_mcp", "cortex_analyst"]
    assert r["timed_out_panels"] == 0 and r["errored_panels"] == 0
    assert r["p95_ms"] < r["target_ms"] == 30_000 and r["passed"] is True


def test_the_record_figures_follow_from_its_latencies() -> None:
    r = record()
    lat = r["latencies_ms"]
    assert len(lat) == r["n"] and all(isinstance(x, int) and x > 0 for x in lat)
    assert r["p50_ms"] == percentile_ms(lat, 0.5)
    assert r["p95_ms"] == percentile_ms(lat, 0.95)
    assert r["max_ms"] == max(lat)
    assert re.fullmatch(r"[0-9a-f]{40}", r["commit"])


def test_the_run_stayed_inside_its_cap_and_is_ledgered() -> None:
    r = record()
    assert r["max_cost_usd"] == DEFAULT_MAX_COST_USD == 15.0  # OWNER D-53
    assert r["n"] * r["hold_usd"] <= r["max_cost_usd"] and 0 < r["spent_usd"] <= r["max_cost_usd"]
    ledger = ROOT / "docs/measurements/spend-ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    mine = [x for x in rows if x["run_id"] == RECORD.stem]
    assert len(mine) == 1 and mine[0]["kind"] == "demo-p95"
    assert abs(mine[0]["spent_usd"] - r["spent_usd"]) < 1e-9


def test_the_record_carries_no_secret() -> None:
    text = RECORD.read_text(encoding="utf-8")
    assert "passcode" not in text.lower()
    assert scan([RECORD]) == []
    for pattern in (r"sk-[A-Za-z0-9_-]{8,}", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}",
                    r"/Users/|/home/|[A-Z]:\\\\"):  # fmt: skip
        assert not re.search(pattern, text), pattern
