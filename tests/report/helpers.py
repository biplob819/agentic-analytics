"""Small summaries and records for dashboard tests (B3.1)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agentic_analytics.report import render_html, render_report
from agentic_analytics.schemas import CategoryAccuracy, FixUplift, Interval, RunSummary
from tests.schemas import factories as f

HOSTILE = "</script><img src=x onerror=alert(1)>"


def iv(point: float, lo: float, hi: float) -> Interval:
    return Interval(point=point, lo=lo, hi=hi)


def cats(*items: tuple[str, int, float, float, float]) -> list[CategoryAccuracy]:
    return [CategoryAccuracy(category=c, n=n, accuracy=iv(p, lo, hi)) for c, n, p, lo, hi in items]  # type: ignore[arg-type]


def summary(**kw: Any) -> RunSummary:
    """Three baselines plus a Claude fix run; Cortex skips `yoy`."""
    backends = [
        f.backend_summary(
            backend="claude_mcp",
            accuracy=iv(0.625, 0.512, 0.73),
            by_category=cats(
                ("aggregation", 17, 0.824, 0.6, 1.0),
                ("yoy", 16, 0.4375, 0.1875, 0.6875),
                ("ambiguous", 16, 0.3125, 0.125, 0.5),
            ),
            cost_per_correct_usd=0.0712,
        ),
        f.backend_summary(
            backend="claude_mcp",
            fixes=("descriptions",),
            accuracy=iv(0.7, 0.6, 0.8),
            by_category=cats(("aggregation", 17, 0.9, 0.75, 1.0), ("yoy", 16, 0.5, 0.25, 0.75)),
        ),
        f.backend_summary(
            backend="gpt_mcp",
            accuracy=iv(0.55, 0.45, 0.65),
            by_category=cats(("aggregation", 17, 0.7, 0.5, 0.9), ("yoy", 16, 0.4, 0.2, 0.6)),
            cost_per_correct_usd=None,
        ),
        f.backend_summary(
            backend="cortex_analyst",
            accuracy=iv(0.7, 0.6, 0.8),
            by_category=cats(("aggregation", 17, 0.95, 0.85, 1.0)),
            cost_per_correct_usd=0.27,
        ),
    ]
    base: dict[str, Any] = dict(
        backends=backends,
        uplifts=[
            FixUplift(
                backend="claude_mcp",
                fix="descriptions",
                delta_points=iv(8.04, 2.0, 14.0),
                p_value=0.0004,
                separated=True,
            ),
            FixUplift(
                backend="claude_mcp",
                fix="examples",
                delta_points=iv(2.5, -3.0, 8.0),
                p_value=0.31,
                separated=False,
            ),
            FixUplift(
                backend="gpt_mcp",
                fix="self_check",
                delta_points=iv(-12.0, -20.0, -4.0),
                p_value=0.002,
                separated=True,
            ),
        ],
        probes={"p-w01": {"claude_mcp": True, "gpt_mcp": False}},
        snapshot_sha256="0123456789abcdef" + "e" * 48,
    )
    base.update(kw)
    return f.run_summary(**base)


def html(s: RunSummary | None = None, records: Any = None, **kw: Any) -> str:
    return render_html(s if s is not None else summary(), records, **kw)


def render(tmp: Path, s: RunSummary | None = None, records: Any = None, **kw: Any) -> Path:
    return render_report(s if s is not None else summary(), records, tmp, **kw)


def section(page: str, sid: str) -> str:
    """The HTML of `<section id=sid>` (sections are not nested)."""
    m = re.search(rf'<section id="{sid}"[^>]*>(.*?)</section>', page, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


def text(fragment: str) -> str:
    """Visible text: tags removed, whitespace collapsed (entities left as they are)."""
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())
