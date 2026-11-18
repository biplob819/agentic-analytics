"""Write-up figures drawn from a run's `summary.json` (B3.2: REL-01, REL-04).

Each figure is a standalone SVG file (its own styles, light and dark schemes) so it can sit in
the README, on PyPI or in a write-up without the dashboard's stylesheet. The figures carry the
run's caveats inside the image (synthetic example, gold mode, conservative intervals, cost
pending, licence credit), so a picture cannot travel without them. Where a summary carries
OWNER D-81's strict score, a hollow mark beside each headline mark shows it. The run of all fixes
together is recorded but is not a figure here (Tech Spec §7). Like the dashboard charts, they are
pure functions of the summary: the same summary gives the same bytes, and no number in them is
typed by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

from agentic_analytics.report.charts import nice_ticks, shape, short
from agentic_analytics.report.example import EXAMPLE_RUN_ID
from agentic_analytics.report.format import (
    ALL_FIXES,
    BACKEND_LABEL,
    MINUS,
    config_label,
    pct,
    pct_range,
    pts,
    pts_ci,
)
from agentic_analytics.schemas import FIX_NAMES, BackendSummary, FixUplift, Interval, RunSummary

BACKENDS = ("claude_mcp", "gpt_mcp", "cortex_analyst")
CATEGORIES = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")
SHAPES = ("circle", "square", "diamond", "triangle", "down")
W = 760
LEFT = 210
ROW = 30
TOP_TITLE = 34
LINE = 18
CSS = """
.bg{fill:#ffffff}.fg{fill:#1d1d1b}.muted{fill:#5f5e58}.grid{stroke:#e3e0d8}
.zero{stroke:#1d1d1b;stroke-dasharray:3 3}.ci{stroke:#1d1d1b;stroke-width:1.5}
.s0{fill:#2f5d50}.s1{fill:#8a4b0f}.s2{fill:#3d5a8a}.s3{fill:#6b3d7a}.s4{fill:#5f5e58}
.warn{fill:#7a4f00}
.h0,.h1,.h2,.h3,.h4{fill:none;stroke-width:1.6}.h0{stroke:#2f5d50}.h1{stroke:#8a4b0f}
.h2{stroke:#3d5a8a}.h3{stroke:#6b3d7a}.h4{stroke:#5f5e58}
@media (prefers-color-scheme:dark){.bg{fill:#0d1117}.fg{fill:#e6edf3}.muted{fill:#9da7b1}
.grid{stroke:#30363d}.zero{stroke:#e6edf3}.ci{stroke:#e6edf3}.s0{fill:#8cc4b0}.s1{fill:#f0a868}
.s2{fill:#9db8e8}.s3{fill:#d3a6e0}.s4{fill:#9da7b1}.warn{fill:#f5dc9a}
.h0{stroke:#8cc4b0}.h1{stroke:#f0a868}.h2{stroke:#9db8e8}.h3{stroke:#d3a6e0}.h4{stroke:#9da7b1}}
text{font:13px -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.title{font-size:15px;font-weight:600}.small{font-size:12px}
"""


@dataclass(frozen=True)
class _Mark:
    row: str
    series: str
    iv: Interval
    tip: str
    strict: float | None = None  # OWNER D-81: drawn as a hollow mark beside the headline one


def _f(v: float) -> str:
    return f"{v:.2f}"


def _fix_order(fixes: Sequence[str]) -> tuple[int, list[int]]:
    return len(fixes), [FIX_NAMES.index(f) for f in fixes]  # type: ignore[arg-type]


def ordered_configs(summary: RunSummary) -> list[BackendSummary]:
    """Configurations in the dashboard's order: backend, then baseline before fix sets."""
    return sorted(summary.backends, key=lambda b: (BACKENDS.index(b.backend), _fix_order(b.fixes)))


def headline_configs(summary: RunSummary) -> list[BackendSummary]:
    """Configurations a README figure may show: every one but the all-fixes run, which is
    recorded but never a headline figure (Tech Spec §7)."""
    return [b for b in ordered_configs(summary) if tuple(b.fixes) != ALL_FIXES]


def has_strict(summary: RunSummary) -> bool:
    """True when the summary carries OWNER D-81's strict score (made after D-81)."""
    return any(b.accuracy_strict is not None for b in summary.backends)


def baselines(summary: RunSummary) -> list[BackendSummary]:
    """Baseline configurations, or every single-fix configuration when a run has no baseline;
    never the all-fixes run (Tech Spec §7), so a run of only that gives an empty list."""
    base = [b for b in ordered_configs(summary) if not b.fixes]
    return base or headline_configs(summary)


def question_count(summary: RunSummary) -> int:
    """Benchmark questions in the run (probes excluded): the largest per-backend total."""
    return max((sum(c.n for c in b.by_category) for b in summary.backends), default=0)


def trials_text(summary: RunSummary) -> str:
    ks = sorted({b.trials for b in summary.backends})
    if not ks:
        return "no trials"
    if len(ks) == 1:
        return "once" if ks[0] == 1 else f"{ks[0]} times"
    return f"{ks[0]} to {ks[-1]} times"


def credit_lines(credit: str | None) -> list[str]:
    """The licence credit split at sentence ends so each line fits the figure."""
    if not credit:
        return []
    parts = [p.strip() for p in " ".join(credit.split()).split(". ")]
    return [p if p.endswith(".") else p + "." for p in parts if p]


def stamp_lines(
    summary: RunSummary, credit: str | None, *, strict_marks: bool = True
) -> list[tuple[str, bool]]:
    """(line, is_warning) caveats printed under every figure; `strict_marks` is False for a
    figure that draws the headline score only (the uplift)."""
    out: list[tuple[str, bool]] = []
    if summary.run_id == EXAMPLE_RUN_ID:
        out.append(("SYNTHETIC EXAMPLE: generated data, not a measurement of any system.", True))
    if summary.gold_mode == "single_engine":
        out.append(("Single-engine gold: gold SQL was confirmed on one engine only.", True))
    else:
        out.append(("Cross-engine gold: gold results agree on ClickHouse and Snowflake.", False))
    out.append(
        (
            f"Pack {summary.pack}, data as of {summary.as_of.isoformat()}, each question asked "
            f"{trials_text(summary)}; scored by result match, no LLM judge.",
            False,
        )
    )
    out.append(
        (
            "95% intervals: two-level bootstrap; conservative (about 99% coverage in simulation).",
            False,
        )
    )
    if has_strict(summary):
        out.append(
            (
                "Filled mark: headline score, with four narrow shape equivalences (OWNER D-81); "
                "ring: strict score.",
                False,
            )
            if strict_marks
            else (
                "Measured on the headline score, tolerant of four narrow shape equivalences "
                "(OWNER D-81).",
                False,
            )
        )
    if summary.cost_status == "estimated":
        out.append(("Cost pending: warehouse credits are not reconciled yet.", True))
    out.extend((line, False) for line in credit_lines(credit))
    return out


def _figure(
    *,
    title: str,
    desc: str,
    marks: Sequence[_Mark],
    rows: Sequence[str],
    series: Sequence[str],
    domain: tuple[float, float],
    ticks: Sequence[float],
    tick_fmt: Callable[[float], str],
    x_label: str,
    stamps: Sequence[tuple[str, bool]],
    notes: dict[str, str] | None = None,
    zero_line: bool = False,
) -> str:
    legend = len(series) > 1
    top = TOP_TITLE + (34 if legend else 18)
    right = 190 if notes else 40
    band = max(float(ROW), 14.0 + 12.0 * len(series))
    axis_y = top + band * len(rows)
    height = axis_y + 44 + LINE * len(stamps) + 8
    lo, hi = domain

    def sx(v: float) -> float:
        v = min(hi, max(lo, v))
        return LEFT + (v - lo) / ((hi - lo) or 1) * (W - LEFT - right)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {_f(height)}" width="{W}" '
        f'height="{_f(height)}" role="img" aria-labelledby="t d">',
        f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>',
        f"<style>{CSS}</style>",
        f'<rect class="bg" width="{W}" height="{_f(height)}" rx="8"/>',
        f'<text class="fg title" x="20" y="{TOP_TITLE - 10}">{escape(title)}</text>',
    ]
    if legend:
        lx = float(LEFT)
        for i, s in enumerate(series):
            k = i % len(SHAPES)
            out.append(
                f'<g class="s{k}">{shape(SHAPES[k], lx + 6, TOP_TITLE + 10)}</g>'
                f'<text class="fg small" x="{_f(lx + 16)}" y="{TOP_TITLE + 14}">'
                f"{escape(s)}</text>"
            )
            lx += 34 + 7 * len(s)
    for t in ticks:
        x = sx(t)
        out.append(
            f'<line class="grid" x1="{_f(x)}" x2="{_f(x)}" y1="{top - 6}" y2="{_f(axis_y)}"/>'
            f'<text class="muted small" x="{_f(x)}" y="{_f(axis_y + 16)}" '
            f'text-anchor="middle">{escape(tick_fmt(t))}</text>'
        )
    if zero_line and lo <= 0 <= hi:
        out.append(
            f'<line class="zero" x1="{_f(sx(0))}" x2="{_f(sx(0))}" y1="{top - 6}" '
            f'y2="{_f(axis_y)}"/>'
        )
    out.append(
        f'<text class="muted small" x="{_f((LEFT + W - right) / 2)}" y="{_f(axis_y + 34)}" '
        f'text-anchor="middle">{escape(x_label)}</text>'
    )
    for r, row in enumerate(rows):
        cy = top + band * r + band / 2
        out.append(
            f'<text class="fg" x="{LEFT - 12}" y="{_f(cy + 4)}" text-anchor="end">'
            f"{escape(short(row))}</text>"
        )
        if notes and row in notes:
            out.append(
                f'<text class="fg small" x="{W - right + 12}" y="{_f(cy + 4)}">'
                f"{escape(notes[row])}</text>"
            )
    index = {s: i for i, s in enumerate(series)}
    for m in marks:
        i = index[m.series]
        k = i % len(SHAPES)
        cy = top + band * rows.index(m.row) + band / 2 + (i - (len(series) - 1) / 2) * 12
        # drawn after the filled mark and larger, so it shows as a ring where the two are equal
        hollow = (
            f'<g class="h{k}">{shape(SHAPES[k], sx(m.strict), cy, 8.0)}</g>'
            if m.strict is not None
            else ""
        )
        out.append(
            f'<g><title>{escape(m.tip)}</title><line class="ci" x1="{_f(sx(m.iv.lo))}" '
            f'x2="{_f(sx(m.iv.hi))}" y1="{_f(cy)}" y2="{_f(cy)}"/>'
            f'<g class="s{k}">{shape(SHAPES[k], sx(m.iv.point), cy)}</g>{hollow}</g>'
        )
    for n, (line, warn) in enumerate(stamps):
        cls = "warn" if warn else "muted"
        mark = "⚠ " if warn else ""
        out.append(
            f'<text class="{cls} small" x="20" y="{_f(axis_y + 58 + LINE * n)}">'
            f"{escape(mark + line)}</text>"
        )
    out.append("</svg>")
    return "".join(out) + "\n"


def _acc_tick(v: float) -> str:
    return f"{v * 100:.0f}%"


ACC_TICKS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def _strict_tip(iv: Interval | None) -> str:
    return f"; strict {pct(iv.point)} (95% CI {pct_range(iv)})" if iv is not None else ""


def headline_svg(summary: RunSummary, credit: str | None = None) -> str:
    """REL-01: accuracy of every configuration with its 95% interval, and the run's caveats."""
    configs = headline_configs(summary)
    rows = [config_label(b.backend, b.fixes) for b in configs]
    marks = [
        _Mark(
            config_label(b.backend, b.fixes),
            "accuracy",
            b.accuracy,
            f"{config_label(b.backend, b.fixes)}: {pct(b.accuracy.point)} "
            f"(95% CI {pct_range(b.accuracy)}){_strict_tip(b.accuracy_strict)}",
            b.accuracy_strict.point if b.accuracy_strict is not None else None,
        )
        for b in configs
    ]
    title = f"Accuracy by configuration on {question_count(summary)} questions (95% intervals)"
    desc = "; ".join(m.tip for m in marks) or "no configurations"
    return _figure(
        title=title,
        desc=desc,
        marks=marks,
        rows=rows,
        series=["accuracy"],
        domain=(0.0, 1.0),
        ticks=ACC_TICKS,
        tick_fmt=_acc_tick,
        x_label="accuracy (share of questions correct, averaged over trials)",
        stamps=stamp_lines(summary, credit),
    )


def _uplift_label(u: FixUplift) -> str:
    fix = "all fixes" if u.fix == "all" else u.fix.replace("_", "-")
    return f"{BACKEND_LABEL[u.backend]} + {fix}"


def uplift_words(u: FixUplift) -> str:
    """The verdict in words (as the dashboard's DSH-03 marker)."""
    if not u.separated:
        return "○ within noise"
    return "▲ above noise, helps" if u.delta_points.point > 0 else "▼ above noise, hurts"


def uplift_svg(summary: RunSummary, credit: str | None = None) -> str | None:
    """REL-04: fix uplift in points against the same backend's baseline; None with no fixes."""
    ups = sorted(
        (u for u in summary.uplifts if u.fix != "all"),  # never a headline figure (§7)
        key=lambda u: (BACKENDS.index(u.backend), FIX_NAMES.index(u.fix)),  # type: ignore[arg-type]
    )
    if not ups:
        return None
    rows = [_uplift_label(u) for u in ups]
    marks = [
        _Mark(
            _uplift_label(u),
            "uplift",
            u.delta_points,
            f"{_uplift_label(u)}: {pts(u.delta_points.point)} ({pts_ci(u.delta_points)}), "
            f"{uplift_words(u)}",
        )
        for u in ups
    ]
    lo = min(0.0, *(u.delta_points.lo for u in ups))
    hi = max(0.0, *(u.delta_points.hi for u in ups))
    ticks = nice_ticks(lo, hi, 5)
    return _figure(
        title="Fix uplift against each backend's baseline (points, 95% intervals)",
        desc="; ".join(m.tip for m in marks),
        marks=marks,
        rows=rows,
        series=["uplift"],
        domain=(ticks[0], ticks[-1]),
        ticks=ticks,
        tick_fmt=lambda v: f"{v:+.0f}".replace("-", MINUS) if v else "0",
        x_label="change in accuracy, percentage points (Holm-corrected verdict at right)",
        stamps=[
            *stamp_lines(summary, credit, strict_marks=False),
            *(
                [
                    (
                        "The run of all fixes together is in the dashboard, not a headline figure.",
                        False,
                    )
                ]
                if any(u.fix == "all" for u in summary.uplifts)
                else []
            ),
        ],
        notes={_uplift_label(u): uplift_words(u) for u in ups},
        zero_line=True,
    )


def categories_svg(summary: RunSummary, credit: str | None = None) -> str:
    """REL-04: baseline accuracy by category, one shape per backend (a map, not a ranking)."""
    base = baselines(summary)
    series = [config_label(b.backend, b.fixes) for b in base]
    present = {c.category for b in base for c in b.by_category}
    rows = [c for c in CATEGORIES if c in present]
    marks = [
        _Mark(
            c.category,
            config_label(b.backend, b.fixes),
            c.accuracy,
            f"{config_label(b.backend, b.fixes)}, {c.category} (n = {c.n}): "
            f"{pct(c.accuracy.point)} (95% CI {pct_range(c.accuracy)})"
            f"{_strict_tip(c.accuracy_strict)}",
            c.accuracy_strict.point if c.accuracy_strict is not None else None,
        )
        for b in base
        for c in b.by_category
    ]
    return _figure(
        title="Accuracy by question category (95% intervals; a map, not a ranking)",
        desc="; ".join(m.tip for m in marks) or "no categories",
        marks=marks,
        rows=rows,
        series=series,
        domain=(0.0, 1.0),
        ticks=ACC_TICKS,
        tick_fmt=_acc_tick,
        x_label="accuracy within the category (few questions each, so the intervals are wide)",
        stamps=stamp_lines(summary, credit),
    )


def writeup_figures(summary: RunSummary, credit: str | None = None) -> dict[str, str]:
    """Every write-up figure by file name: headline, categories and, when fixes ran, uplift."""
    figs = {
        "headline.svg": headline_svg(summary, credit),
        "categories.svg": categories_svg(summary, credit),
    }
    up = uplift_svg(summary, credit)
    if up is not None:
        figs["uplift.svg"] = up
    return figs


__all__ = [
    "categories_svg",
    "credit_lines",
    "has_strict",
    "headline_configs",
    "headline_svg",
    "question_count",
    "stamp_lines",
    "trials_text",
    "uplift_svg",
    "uplift_words",
    "writeup_figures",
]
