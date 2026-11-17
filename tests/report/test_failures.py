"""DSH-05: the failure explorer, its links and escaping."""

from __future__ import annotations

import re

from tests.aggregate.helpers import rec
from tests.report.helpers import HOSTILE, html, section, summary, text
from tests.schemas import factories as f


def _items(page: str) -> list[str]:
    return re.findall(r"<details[^>]*>.*?</details>", section(page, "failures"), re.S)


def _with(record, **answer_kw):
    return record.model_copy(update={"answer": record.answer.model_copy(update=answer_kw)})


def test_lists_incorrect_benchmark_records_in_a_fixed_order():
    records = [
        rec("q-002", "wrong", backend="cortex_analyst"),
        rec("q-003", "right"),
        rec("q-002", "error", trial=2),
        rec("q-001", "wrong", fixes=("descriptions",)),
        rec("q-001", "exec_error"),
        rec("p-w01", "clarify", injection_followed=True),  # probe: not in the explorer
    ]
    items = _items(html(records=records))
    heads = [text(re.search(r"<summary>(.*?)</summary>", i, re.S).group(1)) for i in items]  # type: ignore[union-attr]
    assert heads == [
        "q-001 · Claude (MCP) baseline · trial 1 · execution error: sql_error",
        "q-002 · Claude (MCP) baseline · trial 2 · answer error: step_budget",
        "q-001 · Claude (MCP) + descriptions · trial 1 · result did not match gold",
        "q-002 · Cortex Analyst baseline · trial 1 · result did not match gold",
    ]
    assert "4 incorrect answers" in text(section(html(records=records), "failures"))


def test_filters_are_keyboard_operable_selects():
    sec = section(html(records=[rec("q-001", "wrong")]), "failures")
    assert '<label for="f-backend">' in sec and '<select id="f-backend">' in sec
    assert '<select id="f-config">' in sec
    assert 'data-backend="claude_mcp"' in sec and 'data-config="claude_mcp:baseline"' in sec


def test_shows_question_both_sqls_and_a_diff_with_a_gold_mapping():
    q = f.question(id="q-001", text="Median detached price in Leeds, 2024?")
    r = rec("q-001", "wrong")
    r = r.model_copy(
        update={"scores": r.scores.model_copy(update={"mismatch_reason": "row count differs"})}
    )
    item = _items(html(records=[r], questions={"q-001": q}))[0]
    t = text(item)
    assert "Median detached price in Leeds, 2024?" in t
    assert "Agent SQL" in t and "Gold SQL (clickhouse)" in t
    assert "row count differs" in t
    assert "The agent's SQL is the gold SQL, clause for clause." in t.replace("&#x27;", "'")
    # the gold mapping argument overrides the question's gold
    item2 = _items(html(records=[r], gold_sql={"q-001": {"clickhouse": "SELECT 42"}}))[0]
    assert '<pre class="diff"' in item2
    diff = re.search(r'<pre class="diff"><code>(.*?)</code>', item2, re.S)
    assert diff and "\n-SELECT 42\n" in diff.group(1)
    assert "\n+SELECT medianExact(price)\n+FROM uk.uk_price_paid" in diff.group(1)


def test_without_questions_or_gold_says_so():
    item = _items(html(records=[rec("q-001", "wrong")]))[0]
    t = text(item)
    assert "Question text not provided" in t and "No gold mapping passed" in t


def test_clarification_failures_get_derived_reasons():
    records = [
        rec("q-001", "clarify"),  # false clarification
        rec("q-090", "wrong", expected="clarify"),  # answered an ambiguous question
    ]
    t = text(section(html(records=records), "failures"))
    assert "asked for clarification on an answer-expected question" in t
    assert "answered an ambiguous question without asking back" in t


def test_trace_links_are_relative_and_langfuse_only_when_http():
    good = _with(
        rec("q-001", "wrong"),
        trace_export="results/r-0001/traces/q-001-claude_mcp-1.json",
        trace_url="https://cloud.langfuse.com/project/p/traces/abc",
    )
    item = _items(html(records=[good]))[0]
    assert '<a href="traces/q-001-claude_mcp-1.json">' in item
    assert (
        '<a href="https://cloud.langfuse.com/project/p/traces/abc" rel="noopener noreferrer">'
        in item
    )
    bad = _with(
        rec("q-002", "wrong"),
        trace_export="../../etc/passwd",
        trace_url="javascript:alert(1)",
    )
    item = _items(html(records=[bad]))[0]
    trace = re.search(r"<strong>Trace\.</strong>(.*?)</p>", item, re.S)
    assert trace and "href" not in trace.group(1)
    assert text(trace.group(1)).count("not linked") == 2
    none = _items(html(records=[rec("q-003", "wrong")]))[0]
    assert "No exported trace" in text(none)


def test_hostile_strings_are_escaped_everywhere():
    q = f.question(id="q-001", text=HOSTILE)
    r = rec("q-001", "wrong")
    sql = f"SELECT '{HOSTILE}'"
    r = r.model_copy(
        update={
            "run_id": HOSTILE,
            "answer": r.answer.model_copy(
                update={
                    "sql": sql,
                    "answer": HOSTILE,
                    "model_snapshot": HOSTILE,
                    "trace_url": f"https://x.example/{HOSTILE}",
                    "trace_export": f"results/r/traces/{HOSTILE}.json",
                }
            ),
            "execution": r.execution.model_copy(update={"sql": sql}),  # type: ignore[union-attr]
            "scores": r.scores.model_copy(update={"mismatch_reason": HOSTILE}),
        }
    )
    s = summary(run_id=HOSTILE)
    page = html(
        s,
        [r],
        questions={"q-001": q},
        gold_sql={"q-001": {"clickhouse": HOSTILE}},
        attribution=HOSTILE,
    )
    assert "<img" not in page and "onerror=alert(1)>" not in page
    assert page.count("<script") == 1 and page.count("</script>") == 1
    assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    # the hostile Langfuse URL is linked only in escaped form, never breaking out of the attribute
    for href in re.findall(r'href="([^"]*)"', page):
        assert "<" not in href and ">" not in href


def test_no_records_says_so():
    assert "No records were passed" in text(section(html(records=None), "failures"))
    assert "No incorrect answers" in text(section(html(records=[rec("q-001")]), "failures"))


def test_a_linked_url_cannot_break_out_of_its_attribute():
    """A Langfuse URL with no space passes the http(s) check and is linked; its quotes must be
    escaped inside the attribute, so `onmouseover` stays part of the URL."""
    url = 'https://x.example/a"onmouseover="alert(1)'
    item = _items(html(records=[_with(rec("q-001", "wrong"), trace_url=url)]))[0]
    links = re.findall(r"<a [^>]*>", item)
    assert links == [
        '<a href="https://x.example/a&quot;onmouseover=&quot;alert(1)" rel="noopener noreferrer">'
    ]
    assert 'onmouseover="' not in item


def test_an_answer_error_is_named_before_an_execution_error():
    """Verifier: the agent's own error is the first reason, then the re-execution's."""
    r = rec("q-001", "exec_error")
    r = _with(r, error="step_budget")
    head = text(re.search(r"<summary>(.*?)</summary>", _items(html(records=[r]))[0], re.S).group(1))  # type: ignore[union-attr]
    assert head.endswith("answer error: step_budget")


def test_the_filter_script_matches_the_select_values_to_the_item_attributes():
    """The selects' values are exactly the items' data attributes, and the script compares the
    backend select with data-backend and the configuration select with data-config."""
    from agentic_analytics.report.templates import JS

    records = [rec("q-001", "wrong"), rec("q-002", "wrong", fixes=("descriptions",))]
    sec = section(html(records=records), "failures")
    sel_c = re.search(r'<select id="f-config">(.*?)</select>', sec, re.S).group(1)  # type: ignore[union-attr]
    sel_b = re.search(r'<select id="f-backend">(.*?)</select>', sec, re.S).group(1)  # type: ignore[union-attr]
    assert (
        set(re.findall(r'<option value="([^"]+)"', sel_c))
        == set(re.findall(r'data-config="([^"]+)"', sec))
        == {"claude_mcp:baseline", "claude_mcp:descriptions"}
    )
    assert set(re.findall(r'<option value="([^"]+)"', sel_b)) == {"claude_mcp"}
    assert "(!b.value||d.dataset.backend===b.value)" in JS
    assert "(!c.value||d.dataset.config===c.value)" in JS
