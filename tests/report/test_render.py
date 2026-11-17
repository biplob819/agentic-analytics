"""DSH-01: one self-contained, deterministic index.html plus summary.vl.json."""

from __future__ import annotations

import json
import os
import re
import stat
from html.parser import HTMLParser
from unittest.mock import ANY

import pytest

from agentic_analytics.report import render_html, vega_lite_specs
from tests.aggregate.helpers import rec
from tests.report.helpers import html, render, summary

SECTIONS = ("overview", "categories", "uplift", "cost", "probes", "failures")


def test_writes_index_and_vega_lite_and_returns_the_html_path(tmp_path):
    out = tmp_path / "run"
    path = render(out)
    assert path == out / "index.html"
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "summary.vl.json"]
    for p in out.iterdir():
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
    page = path.read_text("utf-8")
    assert page.startswith("<!doctype html>") and page.endswith("</html>\n")
    for sid in SECTIONS:
        assert f'<section id="{sid}"' in page
    assert page == html()


def test_vega_lite_specs_are_valid_and_built_from_the_same_numbers(tmp_path):
    render(tmp_path)
    specs = json.loads((tmp_path / "summary.vl.json").read_text("utf-8"))
    assert specs.pop("synthetic") is False and "r-0001" in specs.pop("description")
    assert set(specs) == {"accuracy_by_category", "uplift", "cost_per_correct"}
    for spec in specs.values():
        assert "SYNTHETIC" not in spec["description"]
        assert spec["$schema"] == "https://vega.github.io/schema/vega-lite/v5.json"
        assert spec["data"]["values"]
    rows = specs["accuracy_by_category"]["data"]["values"]
    claude_agg = next(
        r
        for r in rows
        if (r["backend"], r["fixes"], r["category"]) == ("claude_mcp", "baseline", "aggregation")
    )
    assert claude_agg == {
        "backend": "claude_mcp",
        "fixes": "baseline",
        "category": "aggregation",
        "n": 17,
        "accuracy": 0.824,
        "lo": 0.6,
        "hi": 1.0,
    }
    up = specs["uplift"]["data"]["values"]
    assert [(u["backend"], u["fix"], u["separated"], u["verdict"]) for u in up] == [
        ("claude_mcp", "descriptions", True, "above noise, helps"),
        ("claude_mcp", "examples", False, "within noise"),
        ("gpt_mcp", "self_check", True, "above noise, hurts"),
    ]
    assert [(u["delta_points"], u["lo"], u["hi"], u["p_value"]) for u in up] == [
        (8.04, 2.0, 14.0, 0.0004),
        (2.5, -3.0, 8.0, 0.31),
        (-12.0, -20.0, -4.0, 0.002),
    ]
    # `separated` is copied from the summary, not re-derived from the interval
    flipped = summary(
        uplifts=[u.model_copy(update={"separated": False}) for u in summary().uplifts]
    )
    vl = vega_lite_specs(flipped)["uplift"]["data"]["values"]
    assert [u["separated"] for u in vl] == [False, False, False]
    assert {u["verdict"] for u in vl} == {"within noise"}
    assert {"synthetic": False, "description": ANY, **specs} == vega_lite_specs(summary())


def test_rendering_is_byte_identical_and_input_sensitive(tmp_path):
    records = [rec("q-001", "wrong"), rec("q-002", "error")]
    a = render(tmp_path / "a", records=records)
    b = render(tmp_path / "b", records=list(reversed(records)))
    for name in ("index.html", "summary.vl.json"):
        assert (a.parent / name).read_bytes() == (b.parent / name).read_bytes()
    other = summary(as_of=summary().as_of.replace(day=1))
    assert render_html(other, records) != render_html(summary(), records)


class _Resources(HTMLParser):
    """Markup that could load something: tags, attributes and CSS, not escaped text."""

    LOADING_TAGS = frozenset(
        {"link", "img", "iframe", "frame", "object", "embed", "video", "audio", "source",
         "track", "image", "use", "base", "form", "input", "meta", "picture", "portal"}
    )  # fmt: skip
    LOADING_ATTRS = frozenset(
        {"src", "srcset", "action", "formaction", "poster", "background", "data", "xlink:href",
         "ping", "manifest", "style"}
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.css: list[str] = []
        self.scripts = 0
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if tag == "meta" and set(names) <= {"charset", "name", "content"}:
            pass  # charset, viewport and color-scheme metas load nothing
        elif tag in self.LOADING_TAGS:
            self.problems.append(f"<{tag}>")
        for name, value in attrs:
            if name in self.LOADING_ATTRS:
                self.problems.append(f"<{tag} {name}=>")
            if value and re.match(r"\s*(?:https?:)?//", value) and (tag, name) != ("a", "href"):
                self.problems.append(f"<{tag} {name}={value}>")
            if name == "href" and value and value.strip().lower().startswith("javascript:"):
                self.problems.append(f"<{tag} href=javascript:>")
        if tag == "script":
            self.scripts += 1
            if "src" in names:
                self.problems.append("<script src>")
        self._in_style = tag == "style"

    def handle_endtag(self, tag):
        self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.css.append(data)


def _resources(page: str) -> _Resources:
    p = _Resources()
    p.feed(page)
    p.close()
    return p


def test_page_is_self_contained():
    sql = "SELECT * FROM url('https://evil.example/x.csv') -- @import url(//cdn.example/a.css)"
    r = rec("q-001", "wrong")
    r = r.model_copy(
        update={
            "answer": r.answer.model_copy(
                update={"sql": sql, "answer": '<link rel="stylesheet" href="https://x.example/">'}
            ),
            "execution": r.execution.model_copy(update={"sql": sql}),  # type: ignore[union-attr]
        }
    )
    page = html(records=[r], gold_sql={"q-001": {"clickhouse": sql}})
    found = _resources(page)
    assert found.problems == []
    assert found.scripts == 1
    css = "".join(found.css)
    assert css and "@import" not in css and "url(" not in css
    # the agent's SQL is shown, escaped, as text: present in the page but loading nothing
    assert "url(&#x27;https://evil.example/x.csv&#x27;)" in page
    assert "&lt;link rel=&quot;stylesheet&quot;" in page


def test_the_resource_check_catches_loading_markup():
    bad = (
        "<html><head><style>@import url(https://x.example/a.css)</style>"
        '<link rel="stylesheet" href="https://x.example/b.css"></head><body>'
        '<img src="https://x.example/c.png"><script src="https://x.example/d.js"></script>'
        '<a href="javascript:alert(1)">x</a><svg><use href="//x.example/e.svg#i"/></svg>'
        "</body></html>"
    )
    found = _resources(bad)
    assert "<link>" in found.problems and "<img>" in found.problems
    assert "<script src>" in found.problems and "<a href=javascript:>" in found.problems
    assert "<use>" in found.problems
    assert "@import" in "".join(found.css)


def test_no_temp_files_left_when_the_write_fails(tmp_path, monkeypatch):
    out = tmp_path / "run"
    render(out)
    before = (out / "index.html").read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("agentic_analytics.report.render.os.replace", boom)
    with pytest.raises(OSError):
        render(out, summary(as_of=summary().as_of.replace(day=2)))
    assert (out / "index.html").read_bytes() == before
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "summary.vl.json"]


def test_numbers_are_formatted_to_one_decimal():
    page = html()
    assert "62.5%" in page and "51.2–73.0%" in page
    assert "+8.0 pts" in page and "95% CI +2.0 to +14.0" in page
    assert "−12.0 pts" in page or "-12.0 pts" in page
    # no long float reprs leak into visible text
    visible = re.sub(r"<[^>]+>", " ", page.split("<body", 1)[1])
    assert re.search(r"\d\.\d{4,}", visible) is None


def test_a_summary_with_one_backend_renders(tmp_path):
    from tests.schemas import factories as f

    s = f.run_summary(uplifts=[], probes={})
    page = render_html(s, None)
    assert "Claude (MCP)" in page and "No fix runs" in page and "No probes" in page


def test_failures_of_one_question_are_ordered_by_trial_whatever_the_input_order():
    records = [rec("q-001", "wrong", trial=3), rec("q-001", "error", trial=1)]
    heads = re.findall(r"<summary>(q-001 · [^<]*)</summary>", html(records=records))
    assert [h.split(" · ")[2] for h in heads] == ["trial 1", "trial 3"]
    assert html(records=records) == html(records=list(reversed(records)))
