"""Live-only proofs for D4.1 and D4.2 (never in CI): set AA_LIVE=1 with the §4 variables.

- one guarded live ask on the owner's accounts (ASK-01, ASK-03, ASK-04 warm-up);
- the keyed Postgres counter under concurrency (ASK-03, D-17), with EVALKIT_DEMO_DATABASE_URL;
- DEM-15 (B3.3, D-93): p95 of 20 live owner-budget asks under 30 s on the deployed page via
  `python -m agentic_analytics.demo.p95 --mode page` (AA_DEMO_URL=https://...; no in-process
  fallback), written to docs/measurements/demo-p95-<date>-page.json.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 for live tests"),
]

ROOT = Path(__file__).resolve().parents[2]


def _ctx():
    from agentic_analytics.demo.ask import context_from_env
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.packs import load_pack
    from agentic_analytics.schemas import load_run_config

    load_dotenv(ROOT / ".env")
    cfg = load_run_config(ROOT / "run.yaml")
    return context_from_env(cfg, load_pack("uk_price_paid"), os.environ)


def _guard(tmp_path, key_store=None):
    from evalkit.demo.guard import SqliteSpend

    from agentic_analytics.demo.ask import make_guard

    store = key_store or SqliteSpend(tmp_path / "live-spend.sqlite")
    return make_guard({"DEMO_PASSCODE": "live-test"}, store=store)


def test_one_guarded_live_ask_on_both_backends(tmp_path):
    from agentic_analytics.demo.ask import DEMO_BACKENDS, ask_panels, warm_up

    ctx = _ctx()
    assert warm_up(ctx.services.clickhouse).ok
    g = _guard(tmp_path)
    panels = ask_panels(
        "What was the median price paid for a property in England and Wales in 2024?",
        DEMO_BACKENDS,
        "live",
        ctx=ctx,
        guard=g,
        passcode="live-test",
        session="live",
    )
    assert [p.backend for p in panels] == list(DEMO_BACKENDS)
    assert all(p.cost_usd <= p.bound_usd for p in panels)
    assert any(p.ok for p in panels)


@pytest.mark.skipif(
    not os.environ.get("EVALKIT_DEMO_DATABASE_URL"), reason="needs EVALKIT_DEMO_DATABASE_URL"
)
def test_the_keyed_postgres_counter_holds_under_concurrency():
    from evalkit.demo.guard import PostgresSpend

    from agentic_analytics.demo.ask import keyed_connect

    url = os.environ["EVALKIT_DEMO_DATABASE_URL"]
    key = "agentic-analytics-test"  # a throwaway key: the real counter is never touched
    day = "2031-01-01"
    store = PostgresSpend(url, keyed_connect(key))
    store.settle(day, store.spent(day), 0.0)  # reset the throwaway day
    ok: list[bool] = []
    threads = [
        threading.Thread(
            target=lambda: ok.append(PostgresSpend(url, keyed_connect(key)).reserve(day, 0.4, 3.0))
        )
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(ok) == 7 and store.spent(day) == pytest.approx(2.8)


@pytest.mark.skipif(
    not os.environ.get("AA_DEMO_URL", "").strip().startswith("https://"),
    reason="set AA_DEMO_URL=https://<deployed page> (DEM-15 is page mode only)",
)
def test_live_p95_over_20_asks_is_under_30_seconds(monkeypatch):
    """DEM-15 (B3.3, D-93): page mode on the deployed page (Playwright), 20 owner-budget asks,
    capped at the script's default $15 (refuses to start if 20 holds do not fit). The record is
    written to docs/measurements/demo-p95-<date>-page.json, to be committed."""
    import json
    from datetime import UTC, datetime

    from agentic_analytics.demo import p95

    monkeypatch.chdir(ROOT)
    url = os.environ["AA_DEMO_URL"].strip()
    out = ROOT / "docs" / "measurements" / f"demo-p95-{datetime.now(UTC):%Y-%m-%d}-page.json"
    code = p95.main(["--mode", "page", "--url", url, "--out", str(out)])
    rec = json.loads(out.read_text(encoding="utf-8"))  # written whether it passed or not
    assert rec["mode"] == "page" and rec["target"] == url and rec["owner_budget"]
    assert rec["backends"] == ["claude_mcp", "cortex_analyst"] and rec["commit"] != "unknown"
    assert rec["n"] == 20 and rec["stopped"] is None and rec["spent_usd"] <= rec["max_cost_usd"]
    assert rec["timed_out_panels"] == 0 and rec["errored_panels"] == 0
    assert rec["p95_ms"] < 30_000 and rec["owner_budget_asks"] == 20
    assert rec["passed"] is True and code == 0
