"""DSH-04: cost per correct, the clarification pair, and the cost-pending state."""

from __future__ import annotations

import re

from tests.report.helpers import html, section, summary, text


def _row(sec: str, label: str) -> str:
    m = re.search(rf'<tr><th scope="row"[^>]*>{re.escape(label)}</th>(.*?)</tr>', sec, re.S)
    assert m, label
    return text(m.group(1))


def test_cost_table_pairs_clarification_with_false_clarification():
    sec = section(html(), "cost")
    claude = _row(sec, "Claude (MCP) baseline")
    assert claude.startswith("$0.071 estimated")
    assert "60.0% 95% CI 50.0–70.0%" in claude  # clarification on ambiguous questions
    assert "5.0% 95% CI 0.0–10.0%" in claude  # false clarification on answer-expected
    assert "9,000 ms / 21,000 ms" in claude
    heads = text(sec)
    assert "Clarification rate (ambiguous questions)" in heads
    assert "False-clarification rate (answer-expected questions)" in heads


def test_nothing_correct_shows_a_dash_with_words():
    assert _row(section(html(), "cost"), "GPT (MCP) baseline").startswith("— nothing correct")


def test_estimated_cost_shows_the_pending_banner():
    page = html()
    banner = re.search(r'<p class="pending"[^>]*>(.*?)</p>', page, re.S)
    assert banner and "Cost pending" in text(banner.group(1))
    assert "warehouse credits" in text(banner.group(1))
    assert "Total cost $4.20 estimated" in text(section(page, "cost"))


def test_reconciled_cost_has_no_banner_and_no_estimated_marks():
    page = html(summary(cost_status="reconciled"))
    assert 'class="pending"' not in page and "Cost pending" not in page
    assert "estimated" not in text(section(page, "cost"))


def test_charts_carry_value_labels():
    sec = section(html(), "cost")
    bars = re.search(r'<svg id="cost-chart".*?</svg>', sec, re.S)
    assert bars and "$0.071" in bars.group(0) and "nothing correct" in bars.group(0)
    clar = re.search(r'<svg id="clar-chart".*?</svg>', sec, re.S)
    assert clar and "<title" in clar.group(0)
    assert "credits" in text(sec) and "snowflake 0.400" in text(sec)


def test_estimated_bar_labels_say_est_and_reconciled_ones_do_not():
    est = re.search(r'<svg id="cost-chart".*?</svg>', section(html(), "cost"), re.S)
    assert est and "$0.071 (est.)" in est.group(0) and "$0.27 (est.)" in est.group(0)
    assert "(estimated, cost pending)" in est.group(0)
    rec_ = re.search(
        r'<svg id="cost-chart".*?</svg>',
        section(html(summary(cost_status="reconciled")), "cost"),
        re.S,
    )
    assert rec_ and "$0.071<" in rec_.group(0) and "(est.)" not in rec_.group(0)
    assert "cost pending" not in rec_.group(0)


def test_clarification_chart_writes_each_marks_value_beside_it():
    """DSH-04: the clarification chart carries value labels, not only tooltips (verifier)."""
    s = summary()
    clar = re.search(r'<svg id="clar-chart".*?</svg>', section(html(s), "cost"), re.S)
    assert clar
    marks = re.findall(
        r'<g class="pt[^"]*"><title>[^<]*</title>.*?</g><text class="value"[^>]*>([^<]+)</text>',
        clar.group(0),
        re.S,
    )
    want = [
        f"{iv.point * 100:.1f}%"
        for b in s.backends
        for iv in (b.clarification_rate, b.false_clarification_rate)
    ]
    assert marks == want
    # the label sits just past its upper whisker, inside the chart
    for hi_x, label_x in re.findall(
        r'<line class="whisker" x1="[\d.]+" x2="([\d.]+)".*?<text class="value" x="([\d.]+)"',
        clar.group(0),
        re.S,
    ):
        assert abs(float(label_x) - float(hi_x) - 8) < 0.011 and float(label_x) < 720 - 40
