"""OWNER D-81: the dashboard shows the strict accuracy beside the tolerant headline."""

from __future__ import annotations

import re

from agentic_analytics.schemas import CategoryAccuracy
from tests.report.helpers import html, iv, section, summary, text


def with_strict():
    s = summary()
    first = s.backends[0]
    cats = tuple(
        CategoryAccuracy(
            category=c.category, n=c.n, accuracy=c.accuracy, accuracy_strict=iv(0.5, 0.25, 0.75)
        )
        for c in first.by_category
    )
    first = first.model_copy(
        update={
            "accuracy_strict": iv(0.5, 0.4, 0.6),
            "equivalence_passes": {"percent_scale": 4, "pivot": 2},
            "by_category": cats,
        }
    )
    return s.model_copy(update={"backends": (first, *s.backends[1:])})


def test_overview_has_a_strict_column_beside_accuracy():
    page = html(with_strict())
    table = re.search(r'<table id="configs".*?</table>', section(page, "overview"), re.S)
    assert table
    heads = [text(h) for h in re.findall(r'<th scope="col">(.*?)</th>', table.group(0))]
    assert heads[1:3] == ["Accuracy", "Strict accuracy"]
    first = re.search(r"<tbody><tr>(.*?)</tr>", table.group(0), re.S)
    assert first
    cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", first.group(1), re.S)]
    assert cells[0].startswith("62.5%")
    assert cells[1] == "50.0% 95% CI 40.0–60.0% tolerant-only passes: percent_scale 4, pivot 2"
    assert "OWNER D-81" in text(table.group(0))


def test_strict_is_not_computed_for_summaries_made_before_it():
    table = re.search(r'<table id="configs".*?</table>', section(html(), "overview"), re.S)
    assert table and "— not computed" in text(table.group(0))


def test_heatmap_and_category_data_show_strict_when_present():
    sec = section(html(with_strict()), "categories")
    agg = re.search(r'<tr><th scope="row"[^>]*>aggregation</th>(.*?)</tr>', sec, re.S)
    assert agg and "strict 50.0%" in text(agg.group(1))
    overall = re.search(r'<tr><th scope="row"[^>]*>Overall</th>(.*?)</tr>', sec, re.S)
    assert overall and "strict 50.0%" in text(overall.group(1))
    data = re.search(r'<table id="cat-claude_mcp-data".*?</table>', sec, re.S)
    assert data and "Strict accuracy" in data.group(0) and "50.0%" in text(data.group(0))
