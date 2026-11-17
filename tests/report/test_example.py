"""The bundled SYNTHETIC example: up to date with its generator, and renders with no keys."""

from __future__ import annotations

import json
import re

from agentic_analytics.report import load_example, render_example
from agentic_analytics.schemas import RunSummary
from tests.report.example_data import EXAMPLE_DIR, RUN_ID, build_example, tree
from tests.report.helpers import section, text


def test_bundled_example_matches_its_generator(tmp_path):
    build_example(tmp_path)
    assert tree(tmp_path) == tree(EXAMPLE_DIR)


def test_example_loads_as_a_clearly_synthetic_run():
    summary, records, questions = load_example()
    assert isinstance(summary, RunSummary) and summary.run_id == RUN_ID == "example-synthetic"
    assert summary.cost_status == "estimated"
    assert records and all(not r.scores.correct for r in records)
    assert {r.question_id for r in records} == set(questions)


def test_render_example_writes_a_page_whose_trace_links_resolve(tmp_path):
    out = tmp_path / "site"
    path = render_example(out)
    page = path.read_text("utf-8")
    assert "SYNTHETIC EXAMPLE" in page and "Cost pending" in page
    assert "Contains HM Land Registry data" in page
    links = re.findall(r'href="(traces/[^"]+)"', page)
    assert links
    for link in links:
        assert (out / link).is_file(), link
    t = text(section(page, "failures"))
    assert "No exported trace" in t  # the generator leaves some failures without one
    assert "above noise, hurts" in text(section(page, "uplift"))
    assert render_example(tmp_path / "again").read_bytes() == path.read_bytes()


def test_example_is_labelled_synthetic_everywhere(tmp_path):
    out = tmp_path / "site"
    page = render_example(out).read_text("utf-8")
    titles = re.findall(r'<title id="[^"]+-t">([^<]+)</title>', page)
    assert len(titles) == 6  # three category charts, uplift, cost, clarification
    assert all(t.startswith("SYNTHETIC EXAMPLE · ") for t in titles)
    specs = json.loads((out / "summary.vl.json").read_text("utf-8"))
    assert specs["synthetic"] is True and "SYNTHETIC EXAMPLE" in specs["description"]
    for name in ("accuracy_by_category", "uplift", "cost_per_correct"):
        assert specs[name]["description"].startswith("SYNTHETIC EXAMPLE · ")
    # made-up accuracy never sits next to a real model's name
    bundle = b"".join(tree(EXAMPLE_DIR).values()).decode("utf-8") + page
    assert re.search(r"(?i)claude-|gpt-\d|sonnet|opus|haiku", bundle) is None
    _, records, _ = load_example()
    assert {r.answer.model_snapshot for r in records} <= {
        "example-agent-a",
        "example-agent-b",
        "example-cortex",
    }
