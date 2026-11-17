"""The results dashboard (B3.1: DSH-01 to DSH-06, EvalKit D-42).

`render_report` writes one self-contained `index.html` and `summary.vl.json` for a run. The
page loads nothing from the network, escapes every value it shows, links only http(s) Langfuse
URLs and safe relative trace paths, and is a pure function of its inputs: the same inputs give
the same bytes. Sections: stamps, overview, accuracy by category, fix uplift, cost and
clarification, safety probes, failure explorer, licence credit. Every Cortex latency figure
carries the OWNER D-62 caveat, and key-shaped strings are scrubbed before escaping.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

from agentic_analytics.report import charts, templates
from agentic_analytics.report.format import (
    BACKEND_LABEL,
    CORTEX_LATENCY_CAVEAT,
    config_label,
    fixes_key,
    fixes_label,
    money,
    p_value,
    pct,
    pct_ci,
    pct_range,
    pts,
    pts_ci,
    safe_http_url,
    safe_trace_href,
    short_hash,
    uplift_fix_label,
)
from agentic_analytics.schemas import (
    ENGINE_OF,
    FIX_NAMES,
    BackendSummary,
    DatasetPack,
    FixUplift,
    Interval,
    Question,
    Record,
    RunSummary,
)
from agentic_analytics.tracing import Redactor

BACKENDS = ("claude_mcp", "gpt_mcp", "cortex_analyst")
CATEGORIES = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")
VL_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
ACC_TICKS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
GoldMap = Mapping[str, Mapping[str, str]]


# Pattern rules only, no environment (so output stays a pure function of the inputs): a
# key-shaped string in a record (an answer, SQL, a step's arguments, an error, a URL) is shown
# as [REDACTED:...] rather than published.
# The redactor keeps a URL's user name (`user:[REDACTED]@host`), which the repo's secret scan
# still reads as a URL with a password; the page drops the whole user part.
_SCRUB = Redactor()
_REDACTED_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@'\"]*\[REDACTED\]@")


def _scrub(v: object) -> str:
    """Secret patterns scrubbed (plain text); a redacted URL loses its user name too."""
    return _REDACTED_USERINFO.sub(r"\1[REDACTED]@", _SCRUB.text(str(v)))


def _e(v: object) -> str:
    """Every value shown on the page: secret patterns scrubbed, then HTML-escaped."""
    return escape(_scrub(v), quote=True)


def _pct_tick(v: float) -> str:
    return f"{v * 100:.0f}%"


def _config_order(fixes: Sequence[str]) -> tuple[int, list[int]]:
    return len(fixes), [FIX_NAMES.index(f) for f in fixes]  # type: ignore[arg-type]


def _record_order(r: Record) -> tuple[Any, ...]:
    return (BACKENDS.index(r.backend), _config_order(r.fixes), r.question_id, r.trial)


# Stamps (DSH-06)


def resolve_credit(
    summary: RunSummary, pack: DatasetPack | None, attribution: str | None
) -> tuple[str | None, DatasetPack | None]:
    """(licence credit, pack it came from). An explicit string wins, then an explicit pack, then
    the bundled pack named by `summary.pack` when its version matches the run's."""
    if attribution:
        return attribution, pack
    if pack is not None:
        return pack.attribution, pack
    from agentic_analytics.packs import PackError, load_pack

    name, _, version = summary.pack.partition("@")
    try:
        found = load_pack(name).manifest
    except (PackError, OSError, ValueError):
        return None, None
    if found.version != version:
        return None, None
    return found.attribution, found


def _header(summary: RunSummary, credit: str | None, example: bool) -> str:
    parts = [f"<h1>Agentic analytics benchmark · run {_e(summary.run_id)}</h1>"]
    if example:
        parts.append(
            '<p class="synthetic" role="note">SYNTHETIC EXAMPLE: every figure on this page comes '
            "from generated data bundled with the package to show the dashboard. It is not a "
            "measurement of any system. The failure explorer holds a sample of its failures.</p>"
        )
    if summary.gold_mode == "single_engine":
        parts.append(
            '<p class="stamp warn" role="note">⚠ single-engine gold: the cross-engine gold check '
            "could not run, so gold SQL was confirmed by dual authorship on one engine only "
            "(D-29).</p>"
        )
    else:
        parts.append(
            '<p class="stamp">✓ cross-engine gold: gold results agree on both engines.</p>'
        )
    if summary.cost_status == "estimated":
        parts.append(
            '<p class="pending" role="status"><strong>⏳ Cost pending.</strong> Cost figures are '
            "estimates: metered warehouse credits are not yet reconciled (usage views lag by "
            "hours), so unreconciled warehouse credits are not included. "
            "<code>agentic-analytics reconcile</code> fills them in.</p>"
        )
    finished = summary.finished_at.isoformat() if summary.finished_at else "not finished"
    meta = [
        f"pack {_e(summary.pack)}",
        f'snapshot <abbr title="{_e(summary.snapshot_sha256)}">'
        f"{_e(short_hash(summary.snapshot_sha256))}</abbr>",
        f"as of {_e(summary.as_of.isoformat())}",
        f'gold check <abbr title="{_e(summary.gold_check_id)}">'
        f"{_e(short_hash(summary.gold_check_id))}</abbr>",
        "single-engine gold" if summary.gold_mode == "single_engine" else "cross-engine gold",
        f"run {_e(summary.run_id)}",
        f'config <abbr title="{_e(summary.config_hash)}">{_e(short_hash(summary.config_hash))}'
        "</abbr>",
        f"started {_e(summary.started_at.isoformat())}",
        f"finished {_e(finished)}",
        f"cost {_e(summary.cost_status)}",
        f"results format {_e(summary.schema_version)}",
    ]
    parts.append('<ul class="meta">' + "".join(f"<li>{m}</li>" for m in meta) + "</ul>")
    if credit:
        parts.append(f'<p class="stamp">Licence: {_e(credit)}</p>')
    else:
        parts.append(
            '<p class="stamp warn" role="note">⚠ Licence attribution unavailable: pack '
            f"{_e(summary.pack)} was not found (or its version differs), so the data's credit "
            "line could not be shown. Add the source's attribution before sharing this page.</p>"
        )
    return '<header id="stamps">' + "".join(parts) + "</header>"


def _footer(credit: str | None, pack: DatasetPack | None) -> str:
    lines = []
    if credit:
        lines.append(f"<p>{_e(credit)}</p>")
    if pack is not None:
        src = safe_http_url(pack.source_url)
        link = f'<a href="{_e(src)}">{_e(src)}</a>' if src else _e(pack.source_url)
        lines.append(f"<p>Dataset: {_e(pack.title)} · licence {_e(pack.licence)} · {link}</p>")
    if not lines:
        lines.append("<p>Licence attribution unavailable for this pack.</p>")
    lines.append(
        "<p>Built by agentic-analytics. Self-contained page: no scripts or fonts load from the "
        "network. Interactive versions of the charts are in <code>summary.vl.json</code>.</p>"
    )
    return '<footer id="licence">' + "".join(lines) + "</footer>"


# Overview


def _iv_cell(iv: Interval) -> str:
    return f'<strong>{pct(iv.point)}</strong> <span class="ci">{pct_ci(iv)}</span>'


def _overview(summary: RunSummary) -> str:
    rows = []
    for b in summary.backends:
        errors = ", ".join(f"{_e(k)} {v}" for k, v in sorted(b.errors.items())) or "none"
        rows.append(
            f'<tr><th scope="row">{_e(config_label(b.backend, b.fixes))}</th>'
            f"<td>{_iv_cell(b.accuracy)}</td><td>{_iv_cell(b.pass_all_trials)}</td>"
            f"<td>{pct(b.trial_agreement)}</td><td>{b.trials}</td><td>{errors}</td>"
            f"<td>{b.answer_from_rows_violations}</td><td>{b.policy_violations}</td></tr>"
        )
    return (
        '<div class="card"><table id="configs"><caption>Every configuration in the run. Accuracy '
        "weights each question equally; intervals are 95% two-level bootstrap intervals over "
        "questions and trials. pass^k is the share of questions correct on every trial.</caption>"
        '<thead><tr><th scope="col">Configuration</th><th scope="col">Accuracy</th>'
        '<th scope="col">pass^k</th><th scope="col">Trial agreement</th>'
        '<th scope="col">Trials</th><th scope="col">Errors</th>'
        '<th scope="col">Answer-from-rows violations</th><th scope="col">Policy violations</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


# DSH-02


SYNTHETIC = "SYNTHETIC EXAMPLE"

_EXAMPLE_SNAPSHOT_PREFIX = "example-"  # the example's agents: example-agent-a, example-cortex
_FINGERPRINT = ("config_hash", "snapshot_sha256", "gold_check_id")
_RECORD_HASHES = (
    "question_hash",
    "prompt_hash",
    "fix_hash",
    "semantic_hash",
    "gold_check_id",
    "snapshot_sha256",
)


def _placeholder_hash(value: str) -> bool:
    """A hash made of one repeated character (the example's 'aaaa…') is not a real digest."""
    return len(value) > 1 and len(set(value)) == 1


def _example_fingerprint() -> tuple[str, ...]:
    from importlib import resources

    src = resources.files("agentic_analytics.report").joinpath("example", "summary.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    return tuple(str(data[k]) for k in _FINGERPRINT)


def looks_synthetic(
    summary: RunSummary,
    records: Sequence[Record] | None = None,
    *,
    placeholder_hashes: bool = True,
) -> bool:
    """True when the run is the bundled SYNTHETIC example, whatever its run id now says.

    A renamed copy of the example keeps its fingerprint (config, snapshot and gold-check
    hashes), its `example-` model snapshots and its placeholder hashes; any one of them marks
    the run synthetic. Publishing refuses on all three. The dashboard stamps on the first two
    only (`placeholder_hashes=False`): test fixtures use placeholder hashes too, and a real run
    never reaches the page without passing the publish check."""
    from agentic_analytics.report.example import EXAMPLE_RUN_ID

    if summary.run_id == EXAMPLE_RUN_ID:
        return True
    if tuple(str(getattr(summary, k)) for k in _FINGERPRINT) == _example_fingerprint():
        return True
    if placeholder_hashes and any(
        _placeholder_hash(str(getattr(summary, k))) for k in _FINGERPRINT
    ):
        return True
    for r in records or ():
        if (r.answer.model_snapshot or "").startswith(_EXAMPLE_SNAPSHOT_PREFIX):
            return True
        if placeholder_hashes and any(
            _placeholder_hash(str(getattr(r, k))) for k in _RECORD_HASHES
        ):
            return True
    return False


def _tag(title: str, example: bool) -> str:
    """Chart titles of the example say so, wherever a chart is copied to."""
    return f"{SYNTHETIC} · {title}" if example else title


def heat_band(point: float) -> str:
    """Five equal bands by point estimate: h0 below 20%, ..., h4 from 80%."""
    return f"h{min(4, max(0, int(point * 5)))}"


def _heat_cell(iv: Interval, n: int | None) -> str:
    n_txt = f' <span class="n">n = {n}</span>' if n is not None else ""
    return f'<td class="heat {heat_band(iv.point)}">{_iv_cell(iv)}{n_txt}</td>'


def _baselines(summary: RunSummary) -> list[BackendSummary]:
    """Baseline configurations in the fixed backend order, whatever the summary's order."""
    return sorted(
        (b for b in summary.backends if not b.fixes), key=lambda b: BACKENDS.index(b.backend)
    )


def _categories(summary: RunSummary, example: bool = False) -> str:
    base = _baselines(summary)
    single = (
        " <strong>single-engine gold</strong>: gold SQL was checked on one engine only."
        if summary.gold_mode == "single_engine"
        else ""
    )
    head = "".join(f'<th scope="col">{_e(BACKEND_LABEL[b.backend])}</th>' for b in base)
    rows = []
    for cat in CATEGORIES:
        cells = []
        for b in base:
            ca = next((c for c in b.by_category if c.category == cat), None)
            cells.append(
                _heat_cell(ca.accuracy, ca.n) if ca else '<td class="missing">— not run</td>'
            )
        rows.append(f'<tr><th scope="row">{cat}</th>{"".join(cells)}</tr>')
    overall = "".join(_heat_cell(b.accuracy, sum(c.n for c in b.by_category) or None) for b in base)
    rows.append(f'<tr><th scope="row">Overall</th>{overall}</tr>')
    ns = sorted({c.n for b in base for c in b.by_category})
    span = "few" if not ns else (f"{ns[0]}" if ns[0] == ns[-1] else f"{ns[0]}–{ns[-1]}")
    table = (
        '<div class="card"><table id="heatmap"><caption>Accuracy of each baseline backend by '
        f"question category: point estimate, 95% interval and n (questions). With {span} "
        "questions per category the intervals are wide (often ±20 points), so read this as a "
        "map, not a ranking. Shading repeats the printed number in five bands (darker green is "
        f"higher).{single}</caption><thead><tr>"
        f'<th scope="col">Category</th>{head}</tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        if base
        else '<div class="card"><p>No baseline configuration in this run.</p></div>'
    )
    charts_html = []
    for backend in BACKENDS:
        configs = sorted(
            (b for b in summary.backends if b.backend == backend),
            key=lambda b: _config_order(b.fixes),
        )
        if not configs:
            continue
        series = [fixes_label(b.fixes) for b in configs]
        cats = [
            c for c in CATEGORIES if any(x.category == c for b in configs for x in b.by_category)
        ]
        marks = [
            charts.Mark(
                row=c.category,
                series=fixes_label(b.fixes),
                point=c.accuracy.point,
                lo=c.accuracy.lo,
                hi=c.accuracy.hi,
                tip=(
                    f"{config_label(b.backend, b.fixes)} · {c.category}: {pct(c.accuracy.point)} "
                    f"(95% CI {pct_range(c.accuracy)}), n = {c.n}"
                ),
            )
            for b in configs
            for c in b.by_category
        ]
        name = BACKEND_LABEL[backend]
        svg = charts.dot_whisker(
            marks,
            rows=cats,
            series=series,
            chart_id=f"cat-{backend}",
            title=_tag(f"{name}: accuracy by category", example),
            desc=(
                f"Accuracy of {name} per question category with 95% intervals, one shape per "
                "fix set: " + ", ".join(series) + "."
            ),
            x_label="Accuracy (share of questions correct)",
            domain=(0.0, 1.0),
            ticks=ACC_TICKS,
            tick_fmt=_pct_tick,
        )
        data_rows = "".join(
            f'<tr><th scope="row">{_e(fixes_label(b.fixes))}</th><td>{c.category}</td>'
            f"<td>{pct(c.accuracy.point)}</td><td>{pct_range(c.accuracy)}</td><td>{c.n}</td></tr>"
            for b in configs
            for c in b.by_category
        )
        charts_html.append(
            f'<div class="card"><h3>{_e(name)}</h3>{svg}<details><summary>Data for this chart'
            f'</summary><div><table id="cat-{backend}-data"><thead><tr>'
            '<th scope="col">Fix set</th><th scope="col">Category</th>'
            '<th scope="col">Accuracy</th><th scope="col">95% CI</th><th scope="col">n</th>'
            f"</tr></thead><tbody>{data_rows}</tbody></table></div></details></div>"
        )
    return table + "".join(charts_html)


# DSH-03


def verdict(u: FixUplift) -> tuple[str, str, str]:
    """(symbol, words, css class). The sign decides helps or hurts for a separated fix."""
    if not u.separated:
        return "○", "within noise", "flat"
    if u.delta_points.point > 0:
        return "▲", "above noise, helps", "up"
    return "▼", "above noise, hurts", "down"


def _uplift_row_label(u: FixUplift) -> str:
    return f"{BACKEND_LABEL[u.backend]} + {uplift_fix_label(u.fix)}"


def _uplift(summary: RunSummary, example: bool = False) -> str:
    ups = list(summary.uplifts)
    if not ups:
        return (
            '<div class="card"><p>No fix runs in this run, so there is no uplift to show.</p></div>'
        )
    lo = min(0.0, *(u.delta_points.lo for u in ups))
    hi = max(0.0, *(u.delta_points.hi for u in ups))
    ticks = charts.nice_ticks(lo, hi, 6)
    rows = [_uplift_row_label(u) for u in ups]
    marks, notes = [], {}
    for u, row in zip(ups, rows, strict=True):
        sym, words, cls = verdict(u)
        marks.append(
            charts.Mark(
                row=row,
                series="uplift",
                point=u.delta_points.point,
                lo=u.delta_points.lo,
                hi=u.delta_points.hi,
                tip=f"{row}: {pts(u.delta_points.point)} ({pts_ci(u.delta_points)}), {words}",
                style=cls,
                shape={"up": "triangle", "down": "down", "flat": "circle"}[cls],
                hollow=cls == "flat",
            )
        )
        notes[row] = (f"{sym} {words}", cls)
    svg = charts.dot_whisker(
        marks,
        rows=rows,
        series=["uplift"],
        chart_id="uplift-chart",
        title=_tag("Fix uplift over each backend's baseline", example),
        desc=(
            "Change in accuracy, in percentage points, from adding each fix, with 95% paired "
            "bootstrap intervals. Each row is labelled with its verdict in words."
        ),
        x_label="Change in accuracy (percentage points)",
        domain=(ticks[0], ticks[-1]),
        ticks=ticks,
        tick_fmt=lambda v: pts(v).replace(" pts", "") if v else "0",
        zero_line=True,
        notes=notes,
    )
    body = []
    for u in ups:
        sym, words, cls = verdict(u)
        body.append(
            f'<tr><th scope="row">{_e(BACKEND_LABEL[u.backend])}</th>'
            f"<td>{_e(uplift_fix_label(u.fix))}</td><td>{pts(u.delta_points.point)}</td>"
            f"<td>{pts_ci(u.delta_points)}</td><td>{p_value(u.p_value)}</td>"
            f'<td class="{ {"up": "pass", "down": "fail", "flat": "muted"}[cls] }">'
            f"{sym} {words}</td></tr>"
        )
    return (
        f'<div class="card">{svg}<p class="legend-key">▲ filled triangle up: above noise, '
        "helps · ▼ filled triangle down: above noise, hurts · ○ hollow circle: within noise. "
        "The dashed line is no change.</p></div>"
        '<div class="card"><table id="uplift-table"><caption>Paired bootstrap over shared '
        "questions, fix run minus the same backend's baseline. Intervals are unadjusted 95%; "
        "“above noise” means the fix survives a Holm correction across that backend's fixes, "
        "its interval excludes zero and enough questions are shared. A fix can be above noise "
        "in either direction: a harmful fix is reported, not hidden.</caption><thead><tr>"
        '<th scope="col">Backend</th><th scope="col">Fix</th><th scope="col">Change</th>'
        '<th scope="col">Interval</th><th scope="col">p (two-sided)</th>'
        '<th scope="col">Verdict</th></tr></thead><tbody>'
        + "".join(body)
        + "</tbody></table></div>"
    )


# DSH-04


def _latency_note(backend: str) -> str:
    """OWNER D-62: the caveat that sits next to every Cortex latency figure."""
    if backend != "cortex_analyst":
        return ""
    return f'<span class="caveat">† {_e(CORTEX_LATENCY_CAVEAT)}</span>'


def _cost_text(b: BackendSummary, estimated: bool) -> str:
    if b.cost_per_correct_usd is None:
        return "— nothing correct"
    return money(b.cost_per_correct_usd) + (" estimated" if estimated else "")


def _cost(summary: RunSummary, example: bool = False) -> str:
    est = summary.cost_status == "estimated"
    rows = []
    for b in summary.backends:
        rows.append(
            f'<tr><th scope="row">{_e(config_label(b.backend, b.fixes))}</th>'
            f"<td>{_e(_cost_text(b, est))}</td>"
            f"<td>{_iv_cell(b.clarification_rate)}</td>"
            f"<td>{_iv_cell(b.false_clarification_rate)}</td>"
            f"<td>{b.latency_p50_ms:,} ms / {b.latency_p95_ms:,} ms{_latency_note(b.backend)}</td>"
            "</tr>"
        )
    credits = ", ".join(f"{_e(k)} {v:.3f}" for k, v in sorted(summary.total_credits.items()))
    total = (
        f"<p>Total cost {_e(money(summary.total_cost_usd))}{' estimated' if est else ''} · "
        f"credits by engine: {credits or 'none'}</p>"
    )
    table = (
        '<div class="card"><table id="cost-table"><caption>Cost per correct answer is model '
        "spend plus Cortex message credits plus allocated warehouse credits, divided by correct "
        "answers. The clarification rate (on ambiguous questions) is always shown next to the "
        "false-clarification rate (on answer-expected questions), so asking back on everything "
        'cannot look good.</caption><thead><tr><th scope="col">Configuration</th>'
        '<th scope="col">Cost per correct</th>'
        '<th scope="col">Clarification rate (ambiguous questions)</th>'
        '<th scope="col">False-clarification rate (answer-expected questions)</th>'
        '<th scope="col">Latency p50 / p95</th></tr></thead><tbody>'
        + "".join(rows)
        + f"</tbody></table>{total}</div>"
    )
    suffix = " (est.)" if est else ""
    bar_svg = charts.bars(
        [
            charts.Bar(
                label=config_label(b.backend, b.fixes),
                value=b.cost_per_correct_usd,
                text=(money(b.cost_per_correct_usd) + suffix)
                if b.cost_per_correct_usd is not None
                else "— nothing correct",
            )
            for b in summary.backends
        ],
        chart_id="cost-chart",
        title=_tag(
            "Cost per correct answer" + (" (estimated, cost pending)" if est else ""), example
        ),
        desc="Dollars spent per correct answer for each configuration; lower is cheaper.",
        x_label="US dollars per correct answer",
        tick_fmt=money,
    )
    series = ["clarification (ambiguous)", "false clarification (answer-expected)"]
    marks = []
    for b in summary.backends:
        label = config_label(b.backend, b.fixes)
        for s, iv in zip(series, (b.clarification_rate, b.false_clarification_rate), strict=True):
            marks.append(
                charts.Mark(
                    row=label,
                    series=s,
                    point=iv.point,
                    lo=iv.lo,
                    hi=iv.hi,
                    tip=f"{label}, {s}: {pct(iv.point)} ({pct_ci(iv)})",
                )
            )
    clar_svg = charts.dot_whisker(
        marks,
        rows=[config_label(b.backend, b.fixes) for b in summary.backends],
        series=series,
        chart_id="clar-chart",
        title=_tag("Clarification and false clarification", example),
        desc=(
            "Share of ambiguous questions where the backend asked back (higher is better) and "
            "share of answer-expected questions where it asked back needlessly (lower is "
            "better), with 95% intervals."
        ),
        x_label="Share of questions",
        domain=(0.0, 1.0),
        ticks=ACC_TICKS,
        tick_fmt=_pct_tick,
        value_fmt=pct,
    )
    return table + f'<div class="card">{bar_svg}</div><div class="card">{clar_svg}</div>'


# Probes


def _probes(summary: RunSummary) -> str:
    probes = summary.probes
    if not probes:
        return '<div class="card"><p>No probes in this run.</p></div>'
    seen = {b for per in probes.values() for b in per}
    cols = [b for b in BACKENDS if b in seen]
    failed = sum(1 for per in probes.values() for ok in per.values() if not ok)
    head = "".join(f'<th scope="col">{_e(BACKEND_LABEL[b])}</th>' for b in cols)
    rows = []
    for pid in sorted(probes):
        cells = []
        for b in cols:
            if b not in probes[pid]:
                cells.append('<td class="muted">— not run</td>')
            elif probes[pid][b]:
                cells.append('<td class="pass">✓ PASS</td>')
            else:
                cells.append('<td class="fail">✗ FAIL</td>')
        rows.append(f'<tr><th scope="row">{_e(pid)}</th>{"".join(cells)}</tr>')
    verdict_line = (
        "All probes passed."
        if not failed
        else f"{failed} probe failure{'s' if failed != 1 else ''}: the run exits 1."
    )
    return (
        f'<div class="card"><p>{verdict_line}</p><table id="probe-table"><caption>Write and '
        "injection probes (outside the benchmark). A backend passes a probe only if every trial "
        "passed.</caption><thead><tr>"
        f'<th scope="col">Probe</th>{head}</tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


# DSH-05

_BREAK = re.compile(
    r"\s+(?=(?:FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|UNION|(?:LEFT |INNER )?JOIN|AND|OR)\b)",
    re.I,
)


def _sql_lines(sql: str) -> list[str]:
    """One clause per line, for the diff only (the SQL itself is shown verbatim)."""
    return [ln.strip() for ln in _BREAK.sub("\n", " ".join(sql.split())).split("\n")]


def failure_reason(r: Record, question: Question | None) -> str:
    """Why the answer was scored incorrect, in words."""
    if r.answer.error is not None:
        return f"answer error: {r.answer.error}"
    if r.execution is not None and r.execution.error is not None:
        return f"execution error: {r.execution.error}"
    if r.scores.false_clarification:
        return "asked for clarification on an answer-expected question"
    if r.scores.result_match is None and not r.scores.clarified:
        return "answered an ambiguous question without asking back"
    if r.scores.mismatch_reason:
        return r.scores.mismatch_reason
    return "result did not match gold"


def _gold_for(
    r: Record, questions: Mapping[str, Question], gold_sql: GoldMap | None
) -> tuple[str, str | None]:
    """(heading, gold SQL or None) for the record's engine."""
    engine = ENGINE_OF[r.backend]
    if gold_sql is not None and r.question_id in gold_sql:
        sql = gold_sql[r.question_id].get(engine)
        if sql is not None:
            return f"Gold SQL ({engine})", sql
    q = questions.get(r.question_id)
    if q is not None and q.gold_sql is not None:
        return f"Gold SQL ({engine})", q.gold_sql[engine]
    return f"Gold SQL ({engine})", None


def _pre(sql: str) -> str:
    return f"<pre><code>{_e(sql)}</code></pre>"


def _explorer_item(r: Record, questions: Mapping[str, Question], gold_sql: GoldMap | None) -> str:
    q = questions.get(r.question_id)
    reason = failure_reason(r, q)
    cfg = config_label(r.backend, r.fixes)
    parts = []
    if q is not None:
        parts.append(f"<p><strong>Question.</strong> {_e(q.text)}</p>")
        meta_q = f" · category {_e(q.category)} · expects {_e(q.expected)}"
    else:
        parts.append(
            '<p class="muted">Question text not provided (pass the questions to show it).</p>'
        )
        meta_q = ""
    snap = f" · model {_e(r.answer.model_snapshot)}" if r.answer.model_snapshot else ""
    parts.append(
        f'<p class="muted">Backend {_e(BACKEND_LABEL[r.backend])} · configuration {_e(cfg)} · '
        f"trial {r.trial}{snap}{meta_q}</p>"
    )
    parts.append(f"<p><strong>Why it failed.</strong> {_e(reason)}</p>")
    if r.answer.answer:
        parts.append(f'<p class="answer"><strong>Answer.</strong> {_e(r.answer.answer)}</p>')
    if r.answer.interpretation:
        parts.append(
            f'<p class="answer"><strong>Cortex&#x27;s reading.</strong> '
            f"{_e(r.answer.interpretation)}</p>"
        )
    parts.append(f"<p><strong>Chart.</strong> {_e(chart_text(r))}</p>")
    if r.answer.clarification:
        parts.append(f"<p><strong>Asked back.</strong> {_e(r.answer.clarification)}</p>")
    if r.answer.suggestions:
        parts.append(
            "<p><strong>Suggestions.</strong> "
            + " · ".join(_e(s) for s in r.answer.suggestions)
            + "</p>"
        )
    agent = r.answer.sql
    parts.append("<h4>Agent SQL</h4>" + (_pre(agent) if agent else "<p>No SQL in the answer.</p>"))
    heading, gold = _gold_for(r, questions, gold_sql)
    if gold is not None:
        parts.append(f"<h4>{_e(heading)}</h4>{_pre(gold)}")
        if agent:
            diff = list(
                difflib.unified_diff(
                    _sql_lines(gold), _sql_lines(agent), "gold", "agent", lineterm="", n=2
                )
            )
            if diff:
                parts.append(
                    '<h4>Gold vs agent</h4><p class="muted">Lines starting − are only in the '
                    "gold SQL, + only in the agent's SQL.</p>"
                    f'<pre class="diff"><code>{_e(chr(10).join(diff))}</code></pre>'
                )
            else:
                parts.append("<p>The agent's SQL is the gold SQL, clause for clause.</p>")
    elif q is not None and q.expected == "clarify":
        readings = "".join(_pre(i[ENGINE_OF[r.backend]]) for i in q.interpretations)
        parts.append(f"<h4>Readings of the ambiguous question</h4>{readings}")
    else:
        parts.append('<p class="muted">No gold mapping passed, so there is no gold SQL.</p>')
    ex = r.execution
    if ex is not None:
        rows = "—" if ex.rows_total is None else f"{ex.rows_total:,}"
        parts.append(
            f'<p class="muted">Executed on {_e(ex.engine)} · rows {rows} · '
            f"{ex.elapsed_ms:,} ms · error {_e(ex.error or 'none')}"
            f"{_latency_note(r.backend)}</p>"
        )
    if r.answer.steps:
        steps = "".join(
            f"<tr><td>{s.index}</td><td>{_e(s.tool)}</td>"
            f"<td><code>{_e(_args(s.arguments))}</code></td>"
            f"<td>{'—' if s.rows_returned is None else s.rows_returned}</td>"
            f"<td>{_e(s.error or '—')}</td><td>{s.latency_ms:,} ms</td></tr>"
            for s in r.answer.steps
        )
        parts.append(
            f"<table><caption>Agent steps{_latency_note(r.backend)}</caption><thead><tr>"
            '<th scope="col">#</th>'
            '<th scope="col">Tool</th><th scope="col">Arguments</th><th scope="col">Rows</th>'
            '<th scope="col">Error</th><th scope="col">Latency</th></tr></thead>'
            f"<tbody>{steps}</tbody></table>"
        )
    parts.append(_trace_links(r))
    head = f"{r.question_id} · {cfg} · trial {r.trial} · {reason}"
    return (
        f'<details data-backend="{_e(r.backend)}" '
        f'data-config="{_e(r.backend)}:{_e(fixes_key(r.fixes))}"><summary>{_e(head)}</summary>'
        f"<div>{''.join(parts)}</div></details>"
    )


def chart_text(r: Record) -> str:
    """What A2.3's builder made of the answer's executed rows, in words (plain text)."""
    chart = r.answer.chart
    if chart is None:
        if r.execution is None or r.execution.result is None:
            return "none: no executed rows."
        return "none: the builder shows this result as a table."
    meta = chart.get("usermeta")
    kind = meta.get("chart") if isinstance(meta, Mapping) else None
    if kind not in ("kpi", "line", "bar"):
        return "a chart of an unrecognised kind."
    shown, total = meta.get("rows_shown"), meta.get("rows_total")  # type: ignore[union-attr]
    if not (isinstance(shown, int) and isinstance(total, int)):
        rows = ""
    elif shown == total:
        rows = f" of {shown} row{'s' if shown != 1 else ''}"
    else:
        rows = f" of the top {shown} of {total} rows"
    return f"{kind}{rows}, built from the executed rows."


def _args(arguments: Mapping[str, Any]) -> str:
    # scrubbed before the cut, so a cut cannot leave a key too short for the patterns to match
    text = _scrub(json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str))
    return text if len(text) <= 400 else text[:399] + "…"


def _trace_links(r: Record) -> str:
    out = []
    path = r.answer.trace_export
    if path:
        href = safe_trace_href(path, r.run_id)
        if href:
            out.append(f'Exported trace: <a href="{_e(href)}">{_e(href)}</a>')
        else:
            out.append(
                f"Exported trace: <code>{_e(path)}</code> (not linked: not a safe relative path)"
            )
    else:
        out.append("No exported trace for this answer.")
    url = r.answer.trace_url
    if url:
        safe = safe_http_url(url)
        if safe:
            out.append(f'<a href="{_e(safe)}" rel="noopener noreferrer">Open in Langfuse</a>')
        else:
            out.append(f"Langfuse: <code>{_e(url)}</code> (not linked: not an http(s) URL)")
    return "<p><strong>Trace.</strong> " + " · ".join(out) + "</p>"


def _failures(
    records: Sequence[Record] | None,
    questions: Mapping[str, Question],
    gold_sql: GoldMap | None,
) -> str:
    if records is None:
        return (
            '<div class="card"><p>No records were passed to the report, so there are no '
            "failures to explore. Render with the run's records to fill this in.</p></div>"
        )
    items = sorted(
        (r for r in records if not r.scores.correct and not r.question_id.startswith("p-")),
        key=_record_order,
    )
    if not items:
        return '<div class="card"><p>No incorrect answers among the records passed.</p></div>'
    backends = [b for b in BACKENDS if any(r.backend == b for r in items)]
    configs = sorted(
        {(r.backend, r.fixes) for r in items},
        key=lambda k: (BACKENDS.index(k[0]), _config_order(k[1])),
    )
    opt_b = "".join(f'<option value="{b}">{_e(BACKEND_LABEL[b])}</option>' for b in backends)
    opt_c = "".join(
        f'<option value="{b}:{_e(fixes_key(f))}">{_e(config_label(b, f))}</option>'
        for b, f in configs
    )
    n = len(items)
    return (
        f'<div class="card"><p>{n} incorrect answer{"s" if n != 1 else ""} '
        "(probes are in the table above). Open an item for the question, both SQLs, the reason "
        'and the trace. <span id="f-count" aria-live="polite"></span></p>'
        f'<label for="f-backend">Backend </label><select id="f-backend">'
        f'<option value="">all</option>{opt_b}</select> '
        f'<label for="f-config">Configuration </label><select id="f-config">'
        f'<option value="">all</option>{opt_c}</select>'
        '<div id="failure-list">'
        + "".join(_explorer_item(r, questions, gold_sql) for r in items)
        + "</div></div>"
    )


# Page and files (DSH-01)


def render_html(
    summary: RunSummary,
    records: Sequence[Record] | None = None,
    *,
    questions: Mapping[str, Question] | None = None,
    gold_sql: GoldMap | None = None,
    pack: DatasetPack | None = None,
    attribution: str | None = None,
    example: bool = False,
) -> str:
    """The dashboard page as a string; a pure function of its arguments.

    A copy of the bundled example is stamped SYNTHETIC whatever `example` says."""
    example = example or looks_synthetic(summary, records, placeholder_hashes=False)
    credit, src_pack = resolve_credit(summary, pack, attribution)
    qs = dict(questions or {})
    sections = "".join(
        (
            templates.section("overview", "Overview", _overview(summary)),
            templates.section("categories", "Accuracy by category", _categories(summary, example)),
            templates.section("uplift", "Fix uplift", _uplift(summary, example)),
            templates.section("cost", "Cost and clarification", _cost(summary, example)),
            templates.section("probes", "Safety probes", _probes(summary)),
            templates.section("failures", "Failure explorer", _failures(records, qs, gold_sql)),
        )
    )
    title = f"Agentic Analytics · run {summary.run_id}"
    if example:
        title = f"SYNTHETIC EXAMPLE · {title}"
    return templates.page(
        title=_e(title),
        header=_header(summary, credit, example),
        sections=sections,
        footer=_footer(credit, src_pack),
    )


def vega_lite_specs(summary: RunSummary, *, example: bool = False) -> dict[str, Any]:
    """The dashboard's charts as Vega-Lite v5 specs, for interactive exploration.

    Top-level `synthetic` and `description` say whether the numbers are the bundled SYNTHETIC
    example; for the example each spec's description says so too. A copy of the example is
    marked synthetic whatever `example` says."""
    example = example or looks_synthetic(summary, placeholder_hashes=False)
    acc = [
        {
            "backend": b.backend,
            "fixes": fixes_key(b.fixes),
            "category": c.category,
            "n": c.n,
            "accuracy": c.accuracy.point,
            "lo": c.accuracy.lo,
            "hi": c.accuracy.hi,
        }
        for b in summary.backends
        for c in b.by_category
    ]
    up = [
        {
            "backend": u.backend,
            "fix": u.fix,
            "delta_points": u.delta_points.point,
            "lo": u.delta_points.lo,
            "hi": u.delta_points.hi,
            "p_value": u.p_value,
            "separated": u.separated,
            "verdict": verdict(u)[1],
        }
        for u in summary.uplifts
    ]
    cost = [
        {
            "backend": b.backend,
            "fixes": fixes_key(b.fixes),
            "cost_per_correct_usd": b.cost_per_correct_usd,
            "cost_status": summary.cost_status,
            "clarification_rate": b.clarification_rate.point,
            "false_clarification_rate": b.false_clarification_rate.point,
        }
        for b in summary.backends
    ]
    shape = {"field": "fixes", "type": "nominal", "title": "Fix set"}
    specs: dict[str, Any] = {
        "accuracy_by_category": {
            "$schema": VL_SCHEMA,
            "description": "Accuracy by category per backend and fix set, 95% intervals",
            "data": {"values": acc},
            "facet": {"row": {"field": "backend", "type": "nominal", "title": "Backend"}},
            "spec": {
                "layer": [
                    {
                        "mark": "rule",
                        "encoding": {
                            "y": {"field": "category", "type": "nominal", "sort": list(CATEGORIES)},
                            "yOffset": shape,
                            "x": {"field": "lo", "type": "quantitative"},
                            "x2": {"field": "hi"},
                        },
                    },
                    {
                        "mark": {"type": "point", "filled": True, "size": 70},
                        "encoding": {
                            "y": {"field": "category", "type": "nominal", "sort": list(CATEGORIES)},
                            "yOffset": shape,
                            "x": {
                                "field": "accuracy",
                                "type": "quantitative",
                                "scale": {"domain": [0, 1]},
                                "axis": {"format": ".0%"},
                                "title": "Accuracy",
                            },
                            "shape": shape,
                            "color": shape,
                            "tooltip": [{"field": f} for f in ("backend", "fixes", "category", "n")]
                            + [{"field": f, "format": ".1%"} for f in ("accuracy", "lo", "hi")],
                        },
                    },
                ]
            },
        },
        "uplift": {
            "$schema": VL_SCHEMA,
            "description": "Fix uplift in points with 95% paired bootstrap intervals",
            "data": {"values": up},
            "transform": [{"calculate": "datum.backend + ' + ' + datum.fix", "as": "row"}],
            "layer": [
                {"mark": {"type": "rule", "strokeDash": [4, 3]}, "encoding": {"x": {"datum": 0}}},
                {
                    "mark": "rule",
                    "encoding": {
                        "y": {"field": "row", "type": "nominal", "title": None},
                        "x": {"field": "lo", "type": "quantitative"},
                        "x2": {"field": "hi"},
                    },
                },
                {
                    "mark": {"type": "point", "filled": True, "size": 90},
                    "encoding": {
                        "y": {"field": "row", "type": "nominal"},
                        "x": {
                            "field": "delta_points",
                            "type": "quantitative",
                            "title": "Change in accuracy (points)",
                        },
                        "shape": {"field": "verdict", "type": "nominal"},
                        "color": {"field": "verdict", "type": "nominal"},
                        "tooltip": [
                            {"field": f}
                            for f in ("backend", "fix", "delta_points", "lo", "hi", "p_value")
                        ]
                        + [{"field": "verdict"}],
                    },
                },
            ],
        },
        "cost_per_correct": {
            "$schema": VL_SCHEMA,
            "description": "Cost per correct answer (USD) per configuration",
            "data": {"values": cost},
            "transform": [{"calculate": "datum.backend + ' ' + datum.fixes", "as": "config"}],
            "mark": "bar",
            "encoding": {
                "y": {"field": "config", "type": "nominal", "title": None},
                "x": {
                    "field": "cost_per_correct_usd",
                    "type": "quantitative",
                    "title": "USD per correct answer",
                },
                "tooltip": [
                    {"field": f}
                    for f in (
                        "backend",
                        "fixes",
                        "cost_per_correct_usd",
                        "cost_status",
                        "clarification_rate",
                        "false_clarification_rate",
                    )
                ],
            },
        },
    }
    for spec in specs.values():
        spec["description"] = _tag(spec["description"], example)
    note = (
        f"{SYNTHETIC}: generated data bundled with agentic-analytics to show the dashboard; "
        "not a measurement of any system."
        if example
        else f"Charts of run {_scrub(summary.run_id)} ({summary.pack}, as of {summary.as_of})."
    )
    return {"synthetic": example, "description": note, **specs}


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; the dashboard is meant to be shared
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_report(
    summary: RunSummary,
    records: Sequence[Record] | None,
    out_dir: str | os.PathLike[str],
    *,
    questions: Mapping[str, Question] | None = None,
    gold_sql: GoldMap | None = None,
    pack: DatasetPack | None = None,
    attribution: str | None = None,
    example: bool = False,
) -> Path:
    """DSH-01: write `index.html` and `summary.vl.json` into `out_dir`; return the HTML path.

    `questions` supplies question text and gold SQL for the failure explorer; `gold_sql`
    (question id -> engine -> SQL) overrides or adds gold SQL. `pack` or `attribution` give the
    licence credit; by default it comes from the bundled pack named in `summary.pack`.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # a renamed copy of the example is still stamped SYNTHETIC
    example = example or looks_synthetic(summary, records, placeholder_hashes=False)
    html = render_html(
        summary,
        records,
        questions=questions,
        gold_sql=gold_sql,
        pack=pack,
        attribution=attribution,
        example=example,
    )
    specs = json.dumps(
        vega_lite_specs(summary, example=example),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    _write_atomic(out / "index.html", html)
    _write_atomic(out / "summary.vl.json", specs + "\n")
    return out / "index.html"
