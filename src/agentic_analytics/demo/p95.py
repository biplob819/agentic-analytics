"""NFR2 / DEM-05: p95 of 20 live asks, with a hard spend cap (live only, run by hand).

    uv run python -m agentic_analytics.demo.p95 --mode page --url https://<host>/<demo-page>
    uv run python -m agentic_analytics.demo.p95 --mode in-process

Two modes, and the output says which one ran:

- **page** (the DEM-05 proof) drives the deployed Streamlit page in a headless browser over
  HTTPS: for each ask a fresh browser session opens the page, chooses live mode, types the
  passcode and a sample question and submits; the time runs from the click to the request's
  cost line (or a refusal). It needs Playwright on the measuring machine (`pip install
  playwright && playwright install chromium`; not a dependency of the package) and the owner
  passcode `DEMO_OWNER_PASSCODE`: the asks run under the owner measurement budget (OWNER D-53:
  its own counter, cap `DEMO_OWNER_CAP_USD` on the host, default $15, at most $25), never the
  visitors' $3/day, with the $1 per-request cap and the SQL guard unchanged. The page says so
  after each ask; the record counts the asks it confirmed (`owner_budget_asks`) and fails the
  run unless every ask was confirmed.
- **in-process** times `ask_panels(..., "live")` on this machine with the owner's `.env`
  (unguarded). It is a diagnostic labelled "in-process", not the deployed page: it leaves out
  Streamlit, the host's network and its cold starts.

Both modes need every measured backend live (OWNER D-58..D-60): `DEMO_LIVE_BACKENDS` must name
them (or be `all`) in the environment the script reads, and on the host too; otherwise the run is
refused before any ask, naming the missing backends. In page mode a page that answers "Live
compute is off" stops the run with the same refusal (exit 3).

Before any ask the worst case (`n` x the request's hold from `run.yaml`) must fit `--max-cost`
(default $15, under the $25 that needs the owner's approval; more than $25 is refused; the same
constants as the owner budget in `demo.ask`), and
during the run an ask starts only while what was spent plus its hold fits. The JSON written to
`docs/measurements/demo-p95-<date>-<mode>.json` records the mode, the budget it ran under, the
URL or host measured, the backends, models and as-of date, the commit, `n` asked and done, the
spend and cap, and the report (p50/p95/max, timed-out and errored panels). It passes only with
20 asks, p95 under 30 s, no timed-out or errored panel and, in page mode, every ask confirmed as
paid by the owner budget.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_analytics.demo.ask import (
    DEMO_BACKENDS,
    LIVE_BACKENDS_VAR,
    OWNER_CAP_MAX_USD,
    OWNER_CAP_USD,
    OWNER_PASSCODE_VAR,
    DemoLimits,
    DemoSettingError,
    live_backends,
    request_bound_usd,
)
from agentic_analytics.demo.page import (
    ERROR_TEXTS,
    OWNER_MARKER,
    P95_ASKS,
    P95Report,
    measure_p95,
)
from agentic_analytics.schemas import BackendName, RunConfig

DEFAULT_MAX_COST_USD = OWNER_CAP_USD  # the owner measurement cap (OWNER D-53): $15
OWNER_APPROVAL_USD = OWNER_CAP_MAX_USD  # above $25 needs the owner's approval: refused here
MODES = ("page", "in-process")
MODE_LABELS = {
    "page": "deployed page over HTTPS (DEM-05)",
    "in-process": "in-process ask_panels on the measuring machine (diagnostic, not DEM-05)",
}
BUDGETS = {
    "page": "owner measurement budget (OWNER D-53), not the visitor $3/day budget",
    "in-process": "none: unguarded on the measuring machine, capped by --max-cost only",
}
PAGE_TIMEOUT_MS = 90_000
COST_RE = re.compile(r"This question cost \$(\d+(?:\.\d+)?)")
TIMEOUT_TEXT = "Timed out after"
COMPUTE_OFF_MARKER = "Live compute is off"  # demo.ask.COMPUTE_OFF: nothing was asked
DONE_MARKERS = (  # page text that ends one ask: the cost line or a refusal shown instead
    "This question cost $",
    COMPUTE_OFF_MARKER,
    "Wrong passcode.",
    "No session.",
    "Rate limit:",
    "per-request limit",
    "live budget is used up",
    "budget store is unavailable",
    "Something went wrong with this question",
)
DONE_MARKERS_JS = json.dumps(list(DONE_MARKERS))


@dataclass(frozen=True)
class PagePanel:
    """What the page shows of one panel, as `measure_p95` reads it."""

    timed_out: bool = False
    ok: bool = True
    cost_usd: float = 0.0


class TimedPanels(list[PagePanel]):
    """One page ask's panels with its own submit-to-answer time (page load excluded), and
    whether the page said the owner measurement budget paid for it."""

    elapsed_s: float | None = None
    owner_budget: bool = False


def live_problem_for(environ: Mapping[str, str], backends: Sequence[BackendName]) -> str | None:
    """Why the p95 run may not start because a measured backend is not live, or None."""
    try:
        on = live_backends(environ)
    except DemoSettingError as exc:
        return exc.reason
    off = [b for b in backends if b not in on]
    if off:
        return (
            f"p95 needs every measured backend live; {', '.join(off)} not in {LIVE_BACKENDS_VAR}. "
            f"Set {LIVE_BACKENDS_VAR} (here and on the host) and try again; nothing asked"
        )
    return None


def worst_case_usd(n: int, hold_usd: float) -> float:
    return n * hold_usd


def check_cap(n: int, hold_usd: float, max_cost_usd: float) -> str | None:
    """Why the run may not start, or None."""
    if not (0 < max_cost_usd <= OWNER_APPROVAL_USD):
        return f"--max-cost must be above $0 and at most ${OWNER_APPROVAL_USD:.0f} (owner approval)"
    worst = worst_case_usd(n, hold_usd)
    if worst > max_cost_usd + 1e-9:
        return (
            f"worst case {n} asks x ${hold_usd:.2f} = ${worst:.2f} is over --max-cost "
            f"${max_cost_usd:.2f}; nothing asked"
        )
    return None


def panels_from_page_text(text: str, n_panels: int) -> list[PagePanel] | None:
    """The panels of one answered request from the page's text, or None when the request was
    refused (no cost line). The cost line's total is spread over the panels."""
    m = COST_RE.search(text)
    if m is None:
        return None
    timed_out = text.count(TIMEOUT_TEXT)
    errored = sum(text.count(t) for t in ERROR_TEXTS.values() if t != ERROR_TEXTS["timeout"])
    errored += text.count("The SQL did not run")
    cost = float(m.group(1)) / max(n_panels, 1)
    out = [PagePanel(timed_out=True, ok=False, cost_usd=cost) for _ in range(timed_out)]
    out += [PagePanel(ok=False, cost_usd=cost) for _ in range(errored)]
    out += [PagePanel(cost_usd=cost) for _ in range(max(n_panels - len(out), 0))]
    return out


def page_asker(url: str, passcode: str, n_panels: int) -> Callable[[str], TimedPanels]:
    """One ask on the deployed page per call, each in a fresh browser session (Playwright,
    imported here: it is the measuring machine's tool, not a package dependency)."""
    import importlib

    sync_api = importlib.import_module("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    browser = pw.chromium.launch()

    def ask_one(question: str) -> TimedPanels:
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
            page.get_by_text("Ask your own (passcode)").click()
            page.get_by_label("Passcode").fill(passcode)
            page.get_by_label("Your question").fill(question)
            t0 = time.perf_counter()  # the visitor's view: from submit to the answer
            page.get_by_role("button", name="Ask both backends").click()
            page.wait_for_function(  # the request's cost line, or a refusal (no panels)
                f"() => {{ const t = document.body.innerText; return {DONE_MARKERS_JS}"
                ".some(m => t.includes(m)); }",
                timeout=PAGE_TIMEOUT_MS,
            )
            text = page.inner_text("body")
            if COMPUTE_OFF_MARKER in text:  # the host has this backend switched off
                raise ValueError(
                    f"the page says live compute is off; set {LIVE_BACKENDS_VAR} on the host"
                )
            out = TimedPanels(panels_from_page_text(text, n_panels) or [])
            out.elapsed_s = time.perf_counter() - t0
            out.owner_budget = OWNER_MARKER in text
            return out
        finally:
            context.close()

    ask_one.close = lambda: (browser.close(), pw.stop())  # type: ignore[attr-defined]
    return ask_one


def git_commit(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def record(
    *,
    mode: str,
    target: str,
    backends: Sequence[BackendName],
    config: RunConfig,
    commit: str,
    n_requested: int,
    hold_usd: float,
    max_cost_usd: float,
    report: P95Report,
    started_at: datetime,
    finished_at: datetime,
    owner_budget_asks: int = 0,
) -> dict[str, Any]:
    """The measurement file's content. Page mode passes only when the page confirmed the owner
    measurement budget on every ask (OWNER D-53)."""
    confirmed = mode != "page" or (report.n > 0 and owner_budget_asks == report.n)
    return {
        "measurement": "demo-p95",
        "mode": mode,
        "measures": MODE_LABELS[mode],
        "budget": BUDGETS[mode],
        "owner_budget": mode == "page",
        "owner_budget_asks": owner_budget_asks,
        "target": target,  # the URL (page) or the host (in-process) measured
        "backends": list(backends),
        "models": {b: config.models[b] for b in backends if b in config.models},
        "effort": config.effort,
        "as_of": config.as_of.isoformat(),
        "commit": commit,
        "n_requested": n_requested,
        "n": report.n,
        "hold_usd": round(hold_usd, 6),
        "max_cost_usd": max_cost_usd,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        **report.to_dict(),
        "passed": report.passed and confirmed,
    }


def run(
    *,
    mode: str,
    config: RunConfig,
    questions: Sequence[str],
    ask_one: Callable[[str], Sequence[Any]],
    target: str,
    commit: str,
    n: int = P95_ASKS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    hold_usd: float | None = None,
    backends: Sequence[BackendName] = DEMO_BACKENDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Check the cap, time `n` asks and return the record. Raises ValueError (nothing asked)
    when the worst case does not fit the cap."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if hold_usd is None:
        from agentic_analytics.cost import credit_rates, model_prices

        hold_usd = request_bound_usd(
            backends, config=config, prices=model_prices(), rates=credit_rates(),
            limits=DemoLimits(),
        )  # fmt: skip
    if problem := check_cap(n, hold_usd, max_cost_usd):
        raise ValueError(problem)
    started = now()
    owner_asks = 0

    def counted(q: str) -> Sequence[Any]:
        nonlocal owner_asks
        panels = ask_one(q)
        owner_asks += bool(panels) and bool(getattr(panels, "owner_budget", False))
        return panels

    report = measure_p95(counted, questions, n=n, max_cost_usd=max_cost_usd, hold_usd=hold_usd)
    return record(
        mode=mode, target=target, backends=backends, config=config, commit=commit,
        n_requested=n, hold_usd=hold_usd, max_cost_usd=max_cost_usd, report=report,
        started_at=started, finished_at=now(), owner_budget_asks=owner_asks,
    )  # fmt: skip


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    import os

    from agentic_analytics.demo.ask import ask_panels, context_from_env, warm_up
    from agentic_analytics.demo.cache import load_cache
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.packs import load_pack
    from agentic_analytics.run.config import with_overrides
    from agentic_analytics.schemas import load_run_config

    ap = argparse.ArgumentParser(prog="python -m agentic_analytics.demo.p95")
    ap.add_argument("--mode", choices=MODES, required=True)
    ap.add_argument("--url", help="the deployed page (page mode)")
    ap.add_argument("--n", type=int, default=P95_ASKS)
    ap.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST_USD)
    ap.add_argument("--config", default="run.yaml")
    ap.add_argument("--cache", default="demo/replay.json", help="sample questions to ask")
    ap.add_argument("--out", help="default docs/measurements/demo-p95-<date>-<mode>.json")
    ns = ap.parse_args(argv)
    load_dotenv()
    env = dict(os.environ if environ is None else environ)
    if ns.mode == "page" and not (ns.url or "").startswith("https://"):
        print("page mode needs --url https://... (the deployed page)", file=sys.stderr)
        return 2
    cache = load_cache(ns.cache)
    if cache.synthetic:
        print(f"{ns.cache} is the synthetic example; record real answers first", file=sys.stderr)
        return 2
    backends: tuple[BackendName, ...] = tuple(b for b in cache.backends if b in DEMO_BACKENDS)
    cfg = with_overrides(load_run_config(ns.config), backends=backends, fixes=(), trials=1)
    questions = cache.samples()
    root = Path.cwd()
    if problem := live_problem_for(env, backends):
        print(problem, file=sys.stderr)
        return 3
    try:
        if ns.mode == "page":
            passcode = (env.get(OWNER_PASSCODE_VAR) or "").strip()  # OWNER D-53
            if not passcode:
                print(f"page mode needs {OWNER_PASSCODE_VAR} (owner budget)", file=sys.stderr)
                return 2
            target = ns.url
            hold = request_bound_usd(backends, config=cfg, prices=_prices(), rates=_rates(),
                                     limits=DemoLimits())  # fmt: skip
            if problem := check_cap(ns.n, hold, ns.max_cost):
                raise ValueError(problem)
            asker: Callable[[str], Sequence[Any]] = page_asker(ns.url, passcode, len(backends))
        else:
            target = platform.node() or "localhost"
            ctx = context_from_env(cfg, load_pack(cache.pack.split("@", 1)[0]), env)
            hold = request_bound_usd(backends, config=cfg, prices=ctx.prices, rates=ctx.rates,
                                     limits=ctx.limits)  # fmt: skip
            if problem := check_cap(ns.n, hold, ns.max_cost):
                raise ValueError(problem)
            warm_up(ctx.services.clickhouse)

            def in_process(q: str) -> Sequence[Any]:  # the owner's run: unguarded, capped here
                return ask_panels(q, backends, "live", ctx=ctx)

            asker = in_process

        try:
            rec = run(
                mode=ns.mode, config=cfg, questions=questions, ask_one=asker, target=target,
                commit=git_commit(root), n=ns.n, max_cost_usd=ns.max_cost, hold_usd=hold,
                backends=backends,
            )  # fmt: skip
        finally:
            close = getattr(asker, "close", None)
            if close is not None:
                close()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    out = Path(ns.out) if ns.out else (
        root / "docs" / "measurements" / f"demo-p95-{datetime.now(UTC):%Y-%m-%d}-{ns.mode}.json"
    )  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{MODE_LABELS[ns.mode]}: p95 {rec['p95_ms']} ms over {rec['n']} asks, "
          f"passed={rec['passed']}; wrote {out}")  # fmt: skip
    return 0 if rec["passed"] else 1


def _prices() -> Any:
    from agentic_analytics.cost import model_prices

    return model_prices()


def _rates() -> Any:
    from agentic_analytics.cost import credit_rates

    return credit_rates()


if __name__ == "__main__":
    sys.exit(main())
