"""DSH-02: accuracy by category × backend, with point, interval and n in text."""

from __future__ import annotations

import re

import pytest

from agentic_analytics.report.render import heat_band
from tests.report.helpers import html, section, summary, text


def _heatmap(page: str) -> str:
    m = re.search(r'<table id="heatmap".*?</table>', section(page, "categories"), re.S)
    assert m
    return m.group(0)


def test_heatmap_has_a_row_per_category_and_a_column_per_baseline_backend():
    table = _heatmap(html())
    heads = re.findall(r'<th scope="col"[^>]*>(.*?)</th>', table)
    assert [text(h) for h in heads] == ["Category", "Claude (MCP)", "GPT (MCP)", "Cortex Analyst"]
    rows = re.findall(r'<th scope="row"[^>]*>(.*?)</th>', table)
    assert [text(r) for r in rows] == [
        "aggregation",
        "time",
        "geography",
        "top_n",
        "yoy",
        "ambiguous",
        "Overall",
    ]


def test_each_cell_prints_point_interval_and_n():
    table = _heatmap(html())
    agg = re.search(r'<tr><th scope="row"[^>]*>aggregation</th>(.*?)</tr>', table, re.S)
    assert agg
    cells = re.findall(r"<td[^>]*>(.*?)</td>", agg.group(1), re.S)
    assert [text(c) for c in cells] == [
        "82.4% 95% CI 60.0–100.0% n = 17",
        "70.0% 95% CI 50.0–90.0% n = 17",
        "95.0% 95% CI 85.0–100.0% n = 17",
    ]
    overall = re.search(r'<tr><th scope="row"[^>]*>Overall</th>(.*?)</tr>', table, re.S)
    assert overall and "62.5% 95% CI 51.2–73.0%" in text(overall.group(1))


def test_missing_category_is_a_dash_with_words_and_bands_are_secondary():
    table = _heatmap(html())
    yoy = re.search(r'<tr><th scope="row"[^>]*>yoy</th>(.*?)</tr>', table, re.S)
    assert yoy
    cells = re.findall(r"<td[^>]*>(.*?)</td>", yoy.group(1), re.S)
    assert text(cells[2]) == "— not run"
    # a heat band class is present but the number is always printed in the cell
    for cell in re.findall(r'<td class="heat (h[0-4])"[^>]*>(.*?)</td>', table, re.S):
        assert re.search(r"\d+\.\d%", cell[1])


def test_caption_calls_categories_a_map_not_a_ranking():
    cap = text(section(html(), "categories"))
    assert "a map, not a ranking" in cap and "n" in cap


def test_dot_and_whisker_chart_per_backend_with_shapes_and_legend():
    sec = section(html(), "categories")
    charts = re.findall(r'<svg id="cat-(\w+)"', sec)
    assert charts == ["claude_mcp", "gpt_mcp", "cortex_analyst"]
    claude = re.search(r'<svg id="cat-claude_mcp".*?</svg>', sec, re.S)
    assert claude
    svg = claude.group(0)
    assert "<title" in svg and "<desc" in svg and 'role="img"' in svg
    # one shape per fix set, each named in the legend
    assert 'class="legend s0"' in svg and 'class="legend s1"' in svg
    assert ">baseline<" in svg and ">descriptions<" in svg
    tips = re.findall(r'<g class="pt[^"]*"[^>]*><title>([^<]+)</title>', svg)
    assert "Claude (MCP) baseline · aggregation: 82.4% (95% CI 60.0–100.0%), n = 17" in tips
    # and a data table for the chart
    assert 'id="cat-claude_mcp-data"' in sec


@pytest.mark.parametrize(
    ("point", "band"),
    [
        (0.0, "h0"),
        (0.199, "h0"),
        (0.2, "h1"),
        (0.399, "h1"),
        (0.4, "h2"),
        (0.6, "h3"),
        (0.799, "h3"),
        (0.8, "h4"),
        (1.0, "h4"),
    ],
)
def test_heat_bands_are_five_equal_bins(point, band):
    assert heat_band(point) == band


def test_heatmap_cells_use_the_band_of_their_printed_point():
    table = _heatmap(html())
    claude = [
        (band, text(cell).split()[0])
        for band, cell in re.findall(r'<td class="heat (h[0-4])"[^>]*>(.*?)</td>', table, re.S)
    ]
    # row-major: aggregation (Claude, GPT, Cortex), yoy (Claude, GPT), ambiguous (Claude), overall
    assert claude == [
        ("h4", "82.4%"),
        ("h3", "70.0%"),
        ("h4", "95.0%"),
        ("h2", "43.8%"),
        ("h2", "40.0%"),
        ("h1", "31.2%"),
        ("h3", "62.5%"),
        ("h2", "55.0%"),
        ("h3", "70.0%"),
    ]


def test_heatmap_columns_follow_the_fixed_backend_order_whatever_the_summary_order():
    s = summary()
    shuffled = s.model_copy(update={"backends": tuple(reversed(s.backends))})
    head = re.findall(r'<th scope="col">([^<]+)</th>', section(html(shuffled), "categories"))
    assert head[:4] == ["Category", "Claude (MCP)", "GPT (MCP)", "Cortex Analyst"]
    assert section(html(shuffled), "categories") == section(html(s), "categories")


def test_overall_row_prints_the_backends_accuracy_and_its_question_count():
    s = summary()
    rows = re.findall(r"<tr>(.*?)</tr>", section(html(s), "categories"), re.S)
    overall = next(r for r in rows if '<th scope="row">Overall</th>' in r)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", overall, re.S)
    base = [b for b in s.backends if not b.fixes]
    assert len(cells) == len(base) == 3
    for b, cell in zip(base, cells, strict=True):
        assert f"{b.accuracy.point * 100:.1f}%" in text(cell)
        assert f"n = {sum(c.n for c in b.by_category)}" in text(cell)
    assert "n = 49" in text(cells[0])  # Claude: 17 + 16 + 16 questions


def test_each_legend_entry_has_the_shape_of_its_series_marks():
    """Verifier: a legend whose shapes were shifted by one still passed."""
    page = html()
    svg = re.search(r'<svg id="cat-claude_mcp".*?</svg>', page, re.S).group(0)  # type: ignore[union-attr]

    def kind(body: str) -> str:
        m = re.search(r"<(circle|rect|polygon)\b", body)
        assert m, body
        return m.group(1)

    legend = {
        name: kind(body)
        for body, name in re.findall(
            r'<g class="legend s\d">(.*?)<text[^>]*>([^<]+)</text></g>', svg, re.S
        )
    }
    assert set(legend) == {"baseline", "descriptions"}
    for name, shape_ in legend.items():
        marks = re.findall(
            rf'<g class="pt[^"]*"><title>Claude \(MCP\)[^<]*?{name}[^<]*</title>(.*?)</g>',
            svg,
            re.S,
        )
        assert marks and all(kind(re.sub(r"<line[^>]*/>", "", m)) == shape_ for m in marks), name
