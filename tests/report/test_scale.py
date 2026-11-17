"""B3.1: every chart is drawn to scale against its own axis, and every number on a mark is the
summary's number (traceable to the aggregate, not re-derived)."""

from __future__ import annotations

import re

import pytest

from agentic_analytics.report.format import money
from tests.report.helpers import html, summary

TOL = 0.011  # coordinates are written to two decimals


def _svg(page: str, chart_id: str) -> str:
    m = re.search(rf'<svg id="{chart_id}".*?</svg>', page, re.S)
    assert m, chart_id
    return m.group(0)


def _axis(svg: str) -> list[tuple[float, str]]:
    """(x, label) of every tick: its grid line and the label under it."""
    return [
        (float(x), label)
        for x, label in re.findall(
            r'<line class="grid" x1="([\d.]+)"[^>]*/><text class="tick"[^>]*>([^<]+)</text>', svg
        )
    ]


def _scale(ticks: list[tuple[float, float]]):
    """The linear map the axis implies, checked to be linear across every tick."""
    (x0, v0), (x1, v1) = ticks[0], ticks[-1]
    k = (x1 - x0) / (v1 - v0)
    for x, v in ticks:
        assert abs(x - (x0 + (v - v0) * k)) < TOL, "ticks are not evenly spaced"
    return lambda v: x0 + (v - v0) * k


def _marks(svg: str) -> dict[str, tuple[float, float, float]]:
    """tooltip -> (point x, whisker lo x, whisker hi x)."""
    out = {}
    for tip, body in re.findall(r'<g class="pt[^"]*"><title>([^<]+)</title>(.*?)</g>', svg, re.S):
        whisker = re.search(r'<line class="whisker" x1="([\d.]+)" x2="([\d.]+)"', body)
        assert whisker, tip
        lo, hi = float(whisker.group(1)), float(whisker.group(2))
        c = re.search(r'<circle cx="([\d.]+)"', body)
        sq = re.search(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', body)
        if c:
            x = float(c.group(1))
        elif sq:
            x = float(sq.group(1)) + float(sq.group(2)) / 2
        else:
            xs = [float(a) for a in re.findall(r"([\d.]+),[\d.]+", body)]
            x = (min(xs) + max(xs)) / 2
        out[tip] = (x, lo, hi)
    return out


def _pct(label: str) -> float:
    return float(label.rstrip("%")) / 100


def test_category_marks_sit_at_their_accuracy_on_the_axis():
    s = summary()
    svg = _svg(html(s), "cat-claude_mcp")
    ticks = _axis(svg)
    assert [lab for _, lab in ticks] == ["0%", "20%", "40%", "60%", "80%", "100%"]
    sx = _scale([(x, _pct(lab)) for x, lab in ticks])
    marks = _marks(svg)
    expected = [(b, c) for b in s.backends if b.backend == "claude_mcp" for c in b.by_category]
    assert len(marks) == len(expected) == 5
    for b, c in expected:
        tip = next(t for t in marks if t.startswith(("Claude (MCP) baseline · " if not b.fixes
                   else "Claude (MCP) + descriptions · ") + c.category + ":"))  # fmt: skip
        x, lo, hi = marks[tip]
        assert abs(x - sx(c.accuracy.point)) < TOL, tip
        assert abs(lo - sx(c.accuracy.lo)) < TOL and abs(hi - sx(c.accuracy.hi)) < TOL, tip
        assert f"n = {c.n}" in tip and f"{c.accuracy.point * 100:.1f}%" in tip


def _pts(label: str) -> float:
    return 0.0 if label == "0" else float(label.replace("−", "-"))


@pytest.mark.parametrize("sign", [1, -1])
def test_uplift_marks_and_zero_line_are_to_scale(sign):
    base = summary()
    ups = [
        u.model_copy(
            update={
                "delta_points": u.delta_points.model_copy(
                    update={
                        "point": sign * abs(u.delta_points.point),
                        "lo": min(sign * abs(u.delta_points.lo), sign * abs(u.delta_points.hi)),
                        "hi": max(sign * abs(u.delta_points.lo), sign * abs(u.delta_points.hi)),
                    }
                ),
                "separated": False,
            }
        )
        for u in base.uplifts
    ]
    s = summary(uplifts=ups)
    svg = _svg(html(s), "uplift-chart")
    ticks = _axis(svg)
    sx = _scale([(x, _pts(lab)) for x, lab in ticks])
    zero = re.search(r'<line class="zero" x1="([\d.]+)"', svg)
    assert zero and abs(float(zero.group(1)) - sx(0.0)) < TOL
    marks = _marks(svg)
    assert len(marks) == len(ups)
    for u, (x, lo, hi) in zip(ups, marks.values(), strict=True):
        assert abs(x - sx(u.delta_points.point)) < TOL
        assert abs(lo - sx(u.delta_points.lo)) < TOL and abs(hi - sx(u.delta_points.hi)) < TOL


def test_cost_bars_are_proportional_and_labelled_with_the_summarys_values():
    s = summary(cost_status="reconciled")
    svg = _svg(html(s), "cost-chart")
    ticks = _axis(svg)
    sx = _scale([(x, float(lab.lstrip("$").replace(",", ""))) for x, lab in ticks])
    bars = re.findall(
        r'<g class="bar"><title>([^<]+)</title><rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg
    )
    priced = [b for b in s.backends if b.cost_per_correct_usd is not None]
    assert len(bars) == len(priced) == 3
    for b, (tip, x, width) in zip(priced, bars, strict=True):
        assert abs(float(x) - sx(0.0)) < TOL
        assert abs(float(x) + float(width) - sx(b.cost_per_correct_usd)) < TOL, tip  # type: ignore[arg-type]
    # the label is the summary's value in the page's money format, not a recomputed one
    labels = [money(b.cost_per_correct_usd or 0.0) for b in priced]
    assert [t.split(": ")[-1] for t, _, _ in bars] == labels
    assert bars[0][0] == "Claude (MCP) baseline: $0.071"
    assert "— nothing correct" in svg  # GPT has no cost per correct


def test_the_cost_axis_ends_at_its_top_tick_just_past_the_largest_bar():
    s = summary(cost_status="reconciled")
    svg = _svg(html(s), "cost-chart")
    ticks = _axis(svg)
    top = max(b.cost_per_correct_usd or 0.0 for b in s.backends)
    last = float(ticks[-1][1].lstrip("$").replace(",", ""))
    assert last >= top and float(ticks[-2][1].lstrip("$").replace(",", "")) < top * 1.05
    # the top tick is the right edge of the plot area: no axis runs on past its last label
    assert abs(ticks[-1][0] - (720 - 150)) < TOL


@pytest.mark.parametrize(
    "top", [0.0005, 0.0123, 0.19, 0.21, 0.27, 0.43, 0.99, 1.0, 7.3, 21.0, 160.0]
)
def test_no_bar_runs_past_the_axis_whatever_the_largest_value(top):
    """Verifier: an axis built short of the largest bar went unnoticed with the one fixture."""
    from agentic_analytics.report import charts

    svg = charts.bars(
        [
            charts.Bar(label="a", value=top, text="t"),
            charts.Bar(label="b", value=top / 3, text="t"),
        ],
        chart_id="c",
        title="t",
        desc="d",
        x_label="x",
        tick_fmt=money,
    )
    ticks = _axis(svg)
    widths = [float(w) for w in re.findall(r'<rect x="[\d.]+" y="[\d.]+" width="([\d.]+)"', svg)]
    right = ticks[-1][0]
    assert max(190 + w for w in widths) <= right + TOL
    assert abs(right - (720 - 150)) < TOL
