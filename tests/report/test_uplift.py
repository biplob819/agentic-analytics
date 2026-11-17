"""DSH-03: fix uplift with interval, "above noise" call and sign."""

from __future__ import annotations

import re

from agentic_analytics.schemas import FixUplift
from tests.report.helpers import html, iv, section, summary, text


def test_marks_carry_shape_and_words_for_each_verdict():
    sec = section(html(), "uplift")
    svg = re.search(r'<svg id="uplift-chart".*?</svg>', sec, re.S)
    assert svg
    marks = re.findall(
        r'<g class="pt (up|down|flat)[^"]*"[^>]*><title>([^<]+)</title>', svg.group(0)
    )
    assert marks == [
        ("up", "Claude (MCP) + descriptions: +8.0 pts (95% CI +2.0 to +14.0), above noise, helps"),
        ("flat", "Claude (MCP) + examples: +2.5 pts (95% CI −3.0 to +8.0), within noise"),
        ("down", "GPT (MCP) + self-check: −12.0 pts (95% CI −20.0 to −4.0), above noise, hurts"),
    ]
    labels = re.findall(r'<text class="verdict[^"]*"[^>]*>([^<]+)</text>', svg.group(0))
    assert labels == ["▲ above noise, helps", "○ within noise", "▼ above noise, hurts"]
    assert 'class="zero"' in svg.group(0)


def test_table_has_signed_delta_interval_p_and_verdict():
    sec = section(html(), "uplift")
    table = re.search(r'<table id="uplift-table".*?</table>', sec, re.S)
    assert table
    rows = [
        text(r)
        for r in re.findall(r"<tbody>.*?</tbody>", table.group(0), re.S)[0].split("<tr>")[1:]
    ]
    assert rows[0] == (
        "Claude (MCP) descriptions +8.0 pts 95% CI +2.0 to +14.0 &lt;0.001 ▲ above noise, helps"
    )
    assert rows[1].endswith("0.310 ○ within noise")
    assert rows[2].endswith("0.002 ▼ above noise, hurts")
    assert "Holm" in text(sec)


def test_near_zero_delta_never_prints_minus_zero():
    s = summary(
        uplifts=[
            FixUplift(
                backend="claude_mcp",
                fix="examples",
                delta_points=iv(-1e-12, -7.8, 7.8),
                p_value=0.9,
                separated=False,
            )
        ]
    )
    sec = section(html(s), "uplift")
    assert "+0.0 pts" in sec and "−0.0" not in sec and "-0.0" not in sec


def test_no_uplifts_says_so():
    sec = section(html(summary(uplifts=[])), "uplift")
    assert "No fix runs" in text(sec) and "<svg" not in sec


def test_axis_always_includes_zero_even_when_every_delta_is_positive():
    s = summary(
        uplifts=[
            FixUplift(
                backend="claude_mcp",
                fix="descriptions",
                delta_points=iv(20.0, 12.0, 28.0),
                p_value=0.0004,
                separated=True,
            )
        ]
    )
    svg = re.search(r'<svg id="uplift-chart".*?</svg>', section(html(s), "uplift"), re.S)
    assert svg and '<line class="zero"' in svg.group(0)
    ticks = re.findall(r'<text class="tick"[^>]*>([^<]+)</text>', svg.group(0))
    assert ticks[0] == "0" and "+20.0" in ticks
    neg = summary(
        uplifts=[
            FixUplift(
                backend="gpt_mcp",
                fix="examples",
                delta_points=iv(-20.0, -28.0, -12.0),
                p_value=0.0004,
                separated=True,
            )
        ]
    )
    svg = re.search(r'<svg id="uplift-chart".*?</svg>', section(html(neg), "uplift"), re.S)
    assert svg and '<line class="zero"' in svg.group(0)
    assert re.findall(r'<text class="tick"[^>]*>([^<]+)</text>', svg.group(0))[-1] == "0"


def test_only_within_noise_marks_are_hollow_circles():
    svg = re.search(r'<svg id="uplift-chart".*?</svg>', html(), re.S)
    assert svg
    groups = re.findall(r'<g class="(pt[^"]*)"><title>([^<]+)</title>(.*?)</g>', svg.group(0))
    assert len(groups) == 3
    for cls, tip, body in groups:
        within = tip.endswith("within noise")
        assert ("hollow" in cls.split()) is within, tip
        assert ("<circle" in body) is within, tip
