"""Accessible inline-SVG charts for the dashboard (B3.1, EvalKit D-42).

Charts are pure functions of their data: no randomness, no clock, fixed number formatting, so
the same summary always renders the same bytes. Every chart carries `<title>` and `<desc>`,
each mark has its own `<title>` tooltip, series differ by shape as well as colour, and the
page puts a data table next to every chart. Text passed in is plain text and escaped here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

W = 720
PAD_L = 190  # row labels
PAD_R_NOTES = 200  # verdict labels beside each row
PAD_R = 40
PAD_R_VALUES = 84  # room for a value label past a whisker ending at the top of the axis
SHAPES = ("circle", "square", "diamond", "triangle", "down")
SERIES_GAP = 13  # px between the series of one row
LABEL_MAX = 30


@dataclass(frozen=True)
class Mark:
    row: str  # y label
    series: str  # legend label; picks the shape unless `shape` is given
    point: float
    lo: float
    hi: float
    tip: str  # the mark's tooltip, plain text
    style: str = ""  # extra CSS class, e.g. "up", "down", "flat"
    shape: str | None = None  # override the series shape
    hollow: bool = False


def _f(v: float) -> str:
    return f"{v:.2f}"


def short(name: str) -> str:
    return name if len(name) <= LABEL_MAX else name[: LABEL_MAX - 1] + "…"


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values covering [lo, hi] (negative values allowed)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step + 1e-9) * step
    ticks: list[float] = []
    v = start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10) + 0.0)
        v += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def shape(kind: str, cx: float, cy: float, r: float = 5.5) -> str:
    if kind == "circle":
        return f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{r}"/>'
    if kind == "square":
        return f'<rect x="{_f(cx - r)}" y="{_f(cy - r)}" width="{2 * r}" height="{2 * r}"/>'
    if kind == "diamond":
        pts = [(cx, cy - r - 1), (cx + r + 1, cy), (cx, cy + r + 1), (cx - r - 1, cy)]
    elif kind == "down":
        pts = [(cx - r - 1, cy - r), (cx + r + 1, cy - r), (cx, cy + r + 1)]
    else:  # triangle (pointing up)
        pts = [(cx, cy - r - 1), (cx + r + 1, cy + r), (cx - r - 1, cy + r)]
    return '<polygon points="' + " ".join(f"{_f(a)},{_f(b)}" for a, b in pts) + '"/>'


def _open(chart_id: str, height: float, title: str, desc: str) -> list[str]:
    return [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {W} {_f(height)}" role="img" '
        f'aria-labelledby="{chart_id}-t {chart_id}-d">',
        f'<title id="{chart_id}-t">{escape(title)}</title>',
        f'<desc id="{chart_id}-d">{escape(desc)}</desc>',
    ]


def dot_whisker(
    marks: Sequence[Mark],
    *,
    rows: Sequence[str],
    series: Sequence[str],
    chart_id: str,
    title: str,
    desc: str,
    x_label: str,
    domain: tuple[float, float],
    ticks: Sequence[float],
    tick_fmt: Callable[[float], str],
    zero_line: bool = False,
    notes: dict[str, tuple[str, str]] | None = None,
    value_fmt: Callable[[float], str] | None = None,
) -> str:
    """Points with interval whiskers on a horizontal axis, one band per row, series side by side
    within a band. `notes` puts a text label (and class) to the right of a row: the verdict of
    an uplift, so the call is written in words, not carried by mark colour alone. `value_fmt`
    writes each mark's point value just past its upper whisker."""
    if not marks:
        return ""
    legend = len(series) > 1
    pad_t = 40 if legend else 16
    pad_b = 46
    pad_r = PAD_R_NOTES if notes else (PAD_R_VALUES if value_fmt else PAD_R)
    band = max(30.0, 16.0 + SERIES_GAP * len(series))
    height = pad_t + band * len(rows) + pad_b
    lo, hi = domain

    def sx(v: float) -> float:
        v = min(hi, max(lo, v))
        return PAD_L + (v - lo) / ((hi - lo) or 1) * (W - PAD_L - pad_r)

    out = _open(chart_id, height, title, desc)
    bottom = pad_t + band * len(rows)
    for t in ticks:
        x = sx(t)
        out.append(
            f'<line class="grid" x1="{_f(x)}" x2="{_f(x)}" y1="{pad_t}" y2="{_f(bottom)}"/>'
            f'<text class="tick" x="{_f(x)}" y="{_f(bottom + 16)}" text-anchor="middle">'
            f"{escape(tick_fmt(t))}</text>"
        )
    if zero_line and lo <= 0 <= hi:
        out.append(
            f'<line class="zero" x1="{_f(sx(0))}" x2="{_f(sx(0))}" y1="{pad_t - 4}" '
            f'y2="{_f(bottom)}"/>'
        )
    out.append(
        f'<text class="axis" x="{_f((PAD_L + W - pad_r) / 2)}" y="{_f(height - 8)}" '
        f'text-anchor="middle">{escape(x_label)}</text>'
    )
    index = {s: i for i, s in enumerate(series)}
    for r, row in enumerate(rows):
        cy = pad_t + band * r + band / 2
        if r:
            out.append(
                f'<line class="sep" x1="{PAD_L}" x2="{W - pad_r}" y1="{_f(pad_t + band * r)}" '
                f'y2="{_f(pad_t + band * r)}"/>'
            )
        out.append(
            f'<text class="rowlabel" x="{PAD_L - 10}" y="{_f(cy + 4)}" text-anchor="end">'
            f"<title>{escape(row)}</title>{escape(short(row))}</text>"
        )
        if notes and row in notes:
            label, cls = notes[row]
            out.append(
                f'<text class="verdict {escape(cls)}" x="{W - pad_r + 12}" y="{_f(cy + 4)}">'
                f"{escape(label)}</text>"
            )
    for m in marks:
        if m.row not in rows or m.series not in index:
            continue
        i = index[m.series]
        cy = pad_t + band * rows.index(m.row) + band / 2 + (i - (len(series) - 1) / 2) * SERIES_GAP
        x, x0, x1 = sx(m.point), sx(m.lo), sx(m.hi)
        cls = " ".join(c for c in ("pt", m.style, f"s{i % len(SHAPES)}", "hollow" * m.hollow) if c)
        out.append(
            f'<g class="{cls}"><title>{escape(m.tip)}</title>'
            f'<line class="whisker" x1="{_f(x0)}" x2="{_f(x1)}" y1="{_f(cy)}" y2="{_f(cy)}"/>'
            f'<line class="whisker" x1="{_f(x0)}" x2="{_f(x0)}" y1="{_f(cy - 4)}" '
            f'y2="{_f(cy + 4)}"/>'
            f'<line class="whisker" x1="{_f(x1)}" x2="{_f(x1)}" y1="{_f(cy - 4)}" '
            f'y2="{_f(cy + 4)}"/>'
            f"{shape(m.shape or SHAPES[i % len(SHAPES)], x, cy)}</g>"
        )
        if value_fmt is not None:
            out.append(
                f'<text class="value" x="{_f(x1 + 8)}" y="{_f(cy + 4)}">'
                f"{escape(value_fmt(m.point))}</text>"
            )
    if legend:
        lx = float(PAD_L)
        for i, s in enumerate(series):
            out.append(
                f'<g class="legend s{i % len(SHAPES)}">'
                f"{shape(SHAPES[i % len(SHAPES)], lx + 6, 16)}"
                f'<text class="tick" x="{_f(lx + 16)}" y="20">{escape(s)}</text></g>'
            )
            lx += 34 + 7 * len(s)
    out.append("</svg>")
    return "".join(out)


@dataclass(frozen=True)
class Bar:
    label: str
    value: float | None
    text: str  # value label, plain text


def bars(
    items: Sequence[Bar],
    *,
    chart_id: str,
    title: str,
    desc: str,
    x_label: str,
    tick_fmt: Callable[[float], str],
) -> str:
    """Horizontal bars with the value written at the end of each bar; a missing value is
    written out in words where the bar would start."""
    if not items:
        return ""
    row_h, pad_t, pad_b, pad_r = 26, 12, 44, 150
    height = pad_t + row_h * len(items) + pad_b
    top = max((b.value for b in items if b.value is not None), default=0.0)
    ticks = nice_ticks(0.0, top * 1.05 if top > 0 else 1.0, 4)
    hi = ticks[-1]

    def sx(v: float) -> float:
        return PAD_L + v / (hi or 1) * (W - PAD_L - pad_r)

    out = _open(chart_id, height, title, desc)
    bottom = pad_t + row_h * len(items)
    for t in ticks:
        x = sx(t)
        out.append(
            f'<line class="grid" x1="{_f(x)}" x2="{_f(x)}" y1="{pad_t}" y2="{_f(bottom)}"/>'
            f'<text class="tick" x="{_f(x)}" y="{_f(bottom + 16)}" text-anchor="middle">'
            f"{escape(tick_fmt(t))}</text>"
        )
    out.append(
        f'<text class="axis" x="{_f((PAD_L + W - pad_r) / 2)}" y="{_f(height - 8)}" '
        f'text-anchor="middle">{escape(x_label)}</text>'
    )
    for r, b in enumerate(items):
        y = pad_t + row_h * r
        cy = y + row_h / 2
        out.append(
            f'<text class="rowlabel" x="{PAD_L - 10}" y="{_f(cy + 4)}" text-anchor="end">'
            f"<title>{escape(b.label)}</title>{escape(short(b.label))}</text>"
        )
        if b.value is None:
            out.append(
                f'<text class="tick" x="{PAD_L + 6}" y="{_f(cy + 4)}">{escape(b.text)}</text>'
            )
            continue
        w = sx(b.value) - PAD_L
        out.append(
            f'<g class="bar"><title>{escape(b.label)}: {escape(b.text)}</title>'
            f'<rect x="{PAD_L}" y="{_f(y + 5)}" width="{_f(w)}" height="{row_h - 10}"/></g>'
            f'<text class="tick" x="{_f(PAD_L + w + 6)}" y="{_f(cy + 4)}">{escape(b.text)}</text>'
        )
    out.append("</svg>")
    return "".join(out)
