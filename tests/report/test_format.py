"""Number formats and labels used across the dashboard."""

from __future__ import annotations

import pytest

from agentic_analytics.report.format import (
    config_label,
    money,
    p_value,
    pct,
    pct_ci,
    pts,
    pts_ci,
    safe_trace_href,
    short_hash,
)
from agentic_analytics.schemas import Interval


def test_percent_and_points_have_one_decimal():
    iv = Interval(point=0.625, lo=0.5125, hi=0.73)
    assert pct(iv.point) == "62.5%"
    assert pct_ci(iv) == "95% CI 51.2–73.0%"
    assert pts(8.04) == "+8.0 pts" and pts(-12.0) == "−12.0 pts" and pts(-0.04) == "+0.0 pts"
    assert pts_ci(Interval(point=1.0, lo=-3.0, hi=8.0)) == "95% CI −3.0 to +8.0"


@pytest.mark.parametrize(
    ("v", "s"),
    [(0, "$0"), (4.2, "$4.20"), (1234.5, "$1,234.50"), (0.0712, "$0.071"), (0.00123, "$0.0012")],
)
def test_money_is_readable(v, s):
    assert money(v) == s


def test_p_values_hashes_and_labels():
    assert p_value(0.0004) == "&lt;0.001" and p_value(0.31) == "0.310"
    assert short_hash("0123456789abcdef" * 4) == "0123456789ab"
    assert config_label("claude_mcp", ()) == "Claude (MCP) baseline"
    assert config_label("gpt_mcp", ("self_check",)) == "GPT (MCP) + self-check"
    assert config_label("claude_mcp", ("descriptions", "examples", "self_check")) == (
        "Claude (MCP) + all fixes"
    )


@pytest.mark.parametrize(
    ("path", "run_id", "href"),
    [
        ("results/r-1/traces/q-001-claude_mcp-1.json", "r-1", "traces/q-001-claude_mcp-1.json"),
        ("traces/q-001-claude_mcp-1.json", "r-1", "traces/q-001-claude_mcp-1.json"),
        ("../../etc/passwd", "r-1", None),
        ("/etc/passwd", "r-1", None),
        ("https://evil.example/x.json", "r-1", None),
        ("javascript:alert(1)", "r-1", None),
        ("results/r-1/traces/a b.json", "r-1", None),
        ("results/r-1/traces/../../x.json", "r-1", None),
        ("C:\\x.json", "r-1", None),
    ],
)
def test_trace_hrefs_are_safe_relative_paths(path, run_id, href):
    assert safe_trace_href(path, run_id) == href


@pytest.mark.parametrize(
    "path",
    [
        "x/../r-0001/traces/q-001-claude_mcp-1.json",
        "results/./r-0001/traces/q-001-claude_mcp-1.json",
        "results//r-0001/traces/q-001-claude_mcp-1.json",
    ],
)
def test_a_dot_segment_before_the_run_folder_is_never_linked(path):
    """The tail looks like the run's traces folder, but the path leaves or bends the tree."""
    assert safe_trace_href(path, "r-0001") is None


def test_p_value_prints_the_threshold_itself_as_a_number():
    """The `<0.001` cut is strict: 0.001 prints as 0.001 (verifier boundary mutant)."""
    assert p_value(0.001) == "0.001" and p_value(0.000999) == "&lt;0.001"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://cloud.langfuse.com/t/abc",  # scheme only
        "https:cloud.langfuse.com/t/abc",  # no host only (a browser would still go there)
        "https://cloud.langfuse.com/t/a bc",  # whitespace only
        "https://cloud.langfuse.com/t/a\x00bc",  # control character only
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "//cloud.langfuse.com/t/abc",
    ],
)
def test_each_rule_of_the_langfuse_link_check_refuses_alone(url):
    """Verifier (L-2, L-8): one refusal per rule, each input breaking only that rule."""
    from agentic_analytics.report.format import safe_http_url

    assert safe_http_url(url) is None
    assert safe_http_url("https://cloud.langfuse.com/t/abc") == "https://cloud.langfuse.com/t/abc"


@pytest.mark.parametrize(
    ("path", "run_id"),
    [
        ("results/r\\1/traces/q-001-claude_mcp-1.json", "r\\1"),  # backslash only
        ("C:/r-1/traces/q-001-claude_mcp-1.json", "r-1"),  # drive colon only
        ("results/other-run/traces/q-001-claude_mcp-1.json", "r-1"),  # another run's folder
        ("results/r-1/traces/q-001-claude_mcp-1.json\n", "r-1"),  # trailing newline
    ],
)
def test_each_rule_of_the_trace_link_check_refuses_alone(path, run_id):
    """Verifier (L-2): each input would link if its one rule were dropped."""
    assert safe_trace_href(path, run_id) is None
