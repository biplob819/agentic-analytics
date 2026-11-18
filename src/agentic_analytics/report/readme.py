"""The README's generated blocks (B3.2, REL-01, REL-04): one chart and three sentences, and a
results table, both generated from a published run's `summary.json` and never typed by hand.

`headline_block(summary)` returns the text that goes between the `<!-- headline:start -->` and
`<!-- headline:end -->` markers; `results_block(summary, ...)` the text between
`<!-- results:start -->` and `<!-- results:end -->`; `update_readme(path, summary, ...)`
replaces whatever is between each pair. Figures print to one decimal. The three sentences say
what was measured (backends, questions, pack, as-of date, trials, judge-free scoring, gold
mode), the result with its intervals and the strict score beside the headline (OWNER D-81), and
how to read both (the intervals are conservative by design, D-42). A summary of the bundled
synthetic example says so in its first sentence, so it cannot pass for a measurement. The run
of all fixes together is recorded but is never a README figure (Tech Spec §7).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from html import escape
from pathlib import Path

from agentic_analytics.report.example import EXAMPLE_RUN_ID
from agentic_analytics.report.figures import (
    baselines,
    has_strict,
    headline_configs,
    ordered_configs,
    question_count,
    trials_text,
    uplift_words,
)
from agentic_analytics.report.format import (
    BACKEND_LABEL,
    config_label,
    cortex_latency_caveat,
    money,
    pct,
    pct_ci,
    pct_range,
    pts,
    pts_ci,
    uplift_fix_label,
)
from agentic_analytics.schemas import BackendSummary, FixUplift, RunSummary

START = "<!-- headline:start -->"
END = "<!-- headline:end -->"
RESULTS_START = "<!-- results:start -->"
RESULTS_END = "<!-- results:end -->"
# Absolute links: the README is also the PyPI project page, where relative paths break.
REPO = "https://github.com/biplob819/agentic-analytics"
REPO_RAW = "https://raw.githubusercontent.com/biplob819/agentic-analytics/main"
CHART_PATH = "docs/assets/headline.svg"
CHART_URL = f"{REPO_RAW}/{CHART_PATH}"
REPORT_URL = "https://biplob819.github.io/agentic-analytics/"
CHART_ALT = (
    "Chart: accuracy of every configuration with its 95% interval, the strict score beside "
    "it, and the run's caveats; the figures are in the text below."
)
# OWNER D-81's four shape equivalences, in the words a reader needs to judge them
EQUIVALENCES = (
    "a percentage given as a fraction, one pivoted row, a zero-count check for no data, "
    "or a period given as a date"
)


class ReadmeError(ValueError):
    """The README's generated-block markers are missing, repeated or out of order."""


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fix_name(u: FixUplift) -> str:
    return "all fixes together" if u.fix == "all" else u.fix.replace("_", "-")


def _accuracy(b: BackendSummary) -> str:
    """'97.0% (95% CI 93.0–100.0%; strict 92.0%)': the strict score always beside it."""
    strict = f"; strict {pct(b.accuracy_strict.point)}" if b.accuracy_strict is not None else ""
    return f"{pct(b.accuracy.point)} ({pct_ci(b.accuracy)}{strict})"


def _fix_sentence_part(summary: RunSummary) -> str:
    single = [u for u in summary.uplifts if u.fix != "all"]  # `all` is never a headline figure
    if not single:
        return "no fix runs are in this summary"
    helps = [u for u in single if u.separated and u.delta_points.point > 0]
    hurts = [u for u in single if u.separated and u.delta_points.point < 0]
    worst = min(hurts, key=lambda u: (u.delta_points.point, u.p_value)) if hurts else None
    if helps:
        u = max(helps, key=lambda u: (u.delta_points.point, -u.p_value))
        text = (
            f"the largest fix effect above noise was {_fix_name(u)} on "
            f"{BACKEND_LABEL[u.backend]}, {pts(u.delta_points.point)} ({pts_ci(u.delta_points)})"
        )
        if worst is not None:
            text += (
                f", and {_fix_name(worst)} lowered {BACKEND_LABEL[worst.backend]}'s accuracy, "
                f"{pts(worst.delta_points.point)} ({pts_ci(worst.delta_points)})"
            )
        return text
    if worst is not None:
        return (
            f"no fix raised accuracy above noise, and {_fix_name(worst)} on "
            f"{BACKEND_LABEL[worst.backend]} lowered it, {pts(worst.delta_points.point)} "
            f"({pts_ci(worst.delta_points)})"
        )
    return "no fix moved accuracy above noise after a Holm correction"


def headline_sentences(summary: RunSummary) -> list[str]:
    """Exactly three sentences: what was measured, the result, how to read it."""
    base = baselines(summary)
    seen = [b.backend for b in (base or ordered_configs(summary))]
    names = _join([BACKEND_LABEL[b] for b in dict.fromkeys(seen)])
    gold = (
        "gold SQL whose results agree on ClickHouse and Snowflake (cross-engine gold)"
        if summary.gold_mode == "cross_engine"
        else "gold SQL confirmed on one engine only (single-engine gold)"
    )
    prefix = (
        "In the bundled SYNTHETIC example (generated data, not a measurement), "
        if summary.run_id == EXAMPLE_RUN_ID
        else ""
    )
    each = "each answered" if len(set(seen)) > 1 else "answered"
    s1 = (
        f"{prefix}{names} {each} {question_count(summary)} questions from the {summary.pack} "
        f"pack {trials_text(summary)} (data as of {summary.as_of.isoformat()}), and every "
        "answer was scored without an LLM judge: its SQL result was compared with "
        f"{gold}, and on ambiguous questions the backend had to ask back."
    )
    accs = _join(
        [
            f"{_accuracy(b)} for "
            f"{config_label(b.backend, b.fixes) if b.fixes else BACKEND_LABEL[b.backend]}"
            for b in base
        ]
    )
    if not base:  # only the all-fixes run, which is never a headline figure (§7)
        s2 = (
            "No baseline or single-fix configuration is in this summary; "
            f"{_fix_sentence_part(summary)}."
        )
    else:
        lead = "Baseline accuracy was" if not base[0].fixes else "Accuracy was"
        s2 = f"{lead} {accs}; {_fix_sentence_part(summary)}."
    cost = (
        "; cost figures are estimates until warehouse credits are reconciled"
        if summary.cost_status == "estimated"
        else ""
    )
    strict = (
        "; the strict score is the pre-registered one, before the four narrow shape "
        f"equivalences the headline accepts for every backend ({EQUIVALENCES})"
        if has_strict(summary)
        else ""
    )
    s3 = (
        "The 95% intervals are two-level bootstrap intervals over questions and trials and are "
        "conservative by design (about 99% coverage in simulation), so overlapping intervals "
        f"do not show a difference{strict}{cost}."
    )
    return [s1, s2, s3]


def headline_block(summary: RunSummary, *, chart_url: str = CHART_URL) -> str:
    """The README block between the markers: one chart reference and three sentences."""
    if not summary.backends:
        raise ValueError("a summary with no configurations has no headline")
    return (
        f"{START}\n"
        f'<p align="center"><img src="{escape(chart_url)}" alt="{escape(CHART_ALT)}" '
        'width="760"></p>\n\n'
        f"{' '.join(headline_sentences(summary))}\n"
        f"{END}"
    )


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def results_block(
    summary: RunSummary,
    *,
    models: Mapping[str, str] | None = None,
    snowflake_region: str | None = None,
) -> str:
    """The README results block: every configuration but the all-fixes run (Tech Spec §7) with
    its accuracy, the strict score beside it (OWNER D-81), asking back, latency (with the
    OWNER D-62 caveat beside Cortex's) and cost per correct answer; then each single fix's
    uplift with its Holm verdict in words. Generated from the summary, never typed."""
    if not summary.backends:
        raise ValueError("a summary with no configurations has no results")
    configs = headline_configs(summary)
    if not configs:
        return (
            f"{RESULTS_START}\nNo baseline or single-fix configuration is in this summary; the "
            f"run of all fixes together is in the dashboard and is not a headline figure.\n"
            f"{RESULTS_END}"
        )
    strict = has_strict(summary)
    cortex = any(b.backend == "cortex_analyst" for b in configs)
    reconciled = summary.cost_status == "reconciled"
    head = ["Configuration", "Accuracy (95% CI)"]
    head += ["Strict"] if strict else []
    head += [
        "Asked back on ambiguous questions",
        "Asked back on answerable questions",
        "p95 latency",
        "Cost per correct answer" + ("" if reconciled else " (estimated)"),
    ]
    lines = [
        "| " + " | ".join(head) + " |",
        "|" + "|".join(["---"] + ["--:"] * (len(head) - 1)) + "|",
    ]
    for b in configs:
        row = [
            config_label(b.backend, b.fixes),
            f"{pct(b.accuracy.point)} ({pct_range(b.accuracy)})",
        ]
        if strict:
            row.append(pct(b.accuracy_strict.point) if b.accuracy_strict is not None else "n/a")
        mark = "¹" if b.backend == "cortex_analyst" else ""
        row += [
            pct(b.clarification_rate.point),
            pct(b.false_clarification_rate.point),
            _seconds(b.latency_p95_ms) + mark,
            money(b.cost_per_correct_usd) if b.cost_per_correct_usd is not None else "n/a",
        ]
        lines.append("| " + " | ".join(row) + " |")
    order = {b: i for i, b in enumerate(BACKEND_LABEL)}
    single = [u for u in summary.uplifts if u.fix != "all"]  # never a README figure (§7)
    if single:
        lines += [
            "",
            "| Fix | Backend | Change in accuracy (95% CI) | Verdict after a Holm correction |",
            "|---|---|--:|---|",
        ]
        for u in sorted(single, key=lambda u: (order[u.backend], u.fix)):
            ci = pts_ci(u.delta_points).removeprefix("95% CI ")
            lines.append(
                f"| {uplift_fix_label(u.fix)} | {BACKEND_LABEL[u.backend]} | "
                f"{pts(u.delta_points.point)} ({ci}) | {uplift_words(u)} |"
            )
    notes: list[str] = []
    agents = [
        f"{BACKEND_LABEL[b]} is `{m}`"
        for b, m in sorted((models or {}).items(), key=lambda kv: order.get(kv[0], len(order)))
        if b in BACKEND_LABEL and b != "cortex_analyst"
    ]
    if agents:
        notes.append(f"{_join(agents)}, each querying ClickHouse through its remote MCP server.")
    notes.append(
        f"Run `{summary.run_id}`: pack {summary.pack}, data as of {summary.as_of.isoformat()}, "
        f"each question asked {trials_text(summary)}; "
        + (
            "costs reconciled with the warehouse's metered credits."
            if reconciled
            else "costs are estimates until warehouse credits are reconciled."
        )
    )
    if strict:
        notes.append(
            "Strict is the pre-registered score; the headline also accepts "
            f"{EQUIVALENCES} (OWNER D-81), for every backend alike."
        )
    if any(u.fix == "all" for u in summary.uplifts):
        notes.append(
            "The run of all fixes together is recorded in the dashboard but is not a headline "
            "figure."
        )
    if cortex:
        notes.append(f"¹ {cortex_latency_caveat(snowflake_region)}")
    return f"{RESULTS_START}\n" + "\n".join(lines) + "\n\n" + " ".join(notes) + f"\n{RESULTS_END}"


def splice(readme: str, block: str, *, start: str = START, end: str = END) -> str:
    """Replace the text from the start marker to the end marker (both included) with `block`."""
    if readme.count(start) != 1 or readme.count(end) != 1:
        raise ReadmeError(f"the README needs exactly one {start} and one {end}")
    head, rest = readme.split(start, 1)
    if end not in rest:
        raise ReadmeError(f"{end} comes before {start}")
    _, tail = rest.split(end, 1)
    return head + block + tail


def render_readme(
    readme: str,
    summary: RunSummary,
    *,
    chart_url: str = CHART_URL,
    models: Mapping[str, str] | None = None,
    snowflake_region: str | None = None,
) -> str:
    """The README text with its generated blocks regenerated; the results block only when the
    README has its markers (a half-present pair is refused)."""
    out = splice(readme, headline_block(summary, chart_url=chart_url))
    if RESULTS_START in out or RESULTS_END in out:
        block = results_block(summary, models=models, snowflake_region=snowflake_region)
        out = splice(out, block, start=RESULTS_START, end=RESULTS_END)
    return out


def update_readme(
    path: str | os.PathLike[str],
    summary: RunSummary,
    *,
    chart_url: str = CHART_URL,
    models: Mapping[str, str] | None = None,
    snowflake_region: str | None = None,
) -> bool:
    """Regenerate the README's blocks in the file at `path`; True when the file changed."""
    p = Path(path)
    old = p.read_text(encoding="utf-8")
    new = render_readme(
        old, summary, chart_url=chart_url, models=models, snowflake_region=snowflake_region
    )
    if new == old:
        return False
    p.write_text(new, encoding="utf-8", newline="\n")
    return True


__all__ = [
    "CHART_PATH",
    "CHART_URL",
    "END",
    "REPORT_URL",
    "RESULTS_END",
    "RESULTS_START",
    "START",
    "ReadmeError",
    "headline_block",
    "headline_sentences",
    "render_readme",
    "results_block",
    "splice",
    "update_readme",
]
