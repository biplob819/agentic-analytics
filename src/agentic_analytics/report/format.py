"""Number formats, labels and link checks for the dashboard (B3.1).

Every function here is pure and returns plain text (not HTML) unless its name says otherwise,
so callers escape once. Rates print as percent to one decimal, uplifts as signed points to one
decimal, money as readable dollars (never scientific), hashes as their first 12 characters.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from evalkit.schemas import Interval

MINUS = "−"  # U+2212, so negative numbers read as such in any font
BACKEND_LABEL = {
    "claude_mcp": "Claude (MCP)",
    "gpt_mcp": "GPT (MCP)",
    "cortex_analyst": "Cortex Analyst",
}
FIX_LABEL = {"descriptions": "descriptions", "examples": "examples", "self_check": "self-check"}
# OWNER D-62: stated next to every Cortex latency figure on the page. M2.1 times a Cortex answer
# by its SQL execution alone (the Cortex API round trip is not on the record), so the note says so.
CORTEX_LATENCY_CAVEAT = (
    "Snowflake account in Thailand (AWS ap-southeast-7); Cortex inference crosses to the US "
    "(OWNER D-62), so any Cortex API time includes that distance. This figure is the SQL's "
    "execution on the Snowflake warehouse only: the Cortex API round trip is not on the record."
)
ALL_FIXES = ("descriptions", "examples", "self_check")
_TRACE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.json\Z")


def _signed(v: float, digits: int = 1) -> str:
    r = round(v, digits)
    if r == 0:
        r = 0.0  # never "-0.0"
    s = f"{abs(r):.{digits}f}"
    return f"{MINUS}{s}" if r < 0 else f"+{s}"


def pct(v: float) -> str:
    """0.625 -> '62.5%'."""
    return f"{v * 100:.1f}%"


def pct_range(iv: Interval) -> str:
    return f"{iv.lo * 100:.1f}–{iv.hi * 100:.1f}%"


def pct_ci(iv: Interval) -> str:
    """'95% CI 51.2–73.0%'."""
    return f"95% CI {pct_range(iv)}"


def pts(v: float) -> str:
    """Signed points, one decimal: '+8.0 pts', '−12.0 pts', and '+0.0 pts' for a zero."""
    return f"{_signed(v)} pts"


def pts_ci(iv: Interval) -> str:
    return f"95% CI {_signed(iv.lo)} to {_signed(iv.hi)}"


def p_value(p: float) -> str:
    """HTML-ready: '&lt;0.001' below 0.001, else three decimals."""
    return "&lt;0.001" if p < 0.001 else f"{p:.3f}"


def money(v: float) -> str:
    """Readable dollars at any magnitude (EvalKit's report format): never scientific."""
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.3f}".rstrip("0").rstrip(".")
    digits = max(2, -math.floor(math.log10(v)) + 1)
    return f"${v:.{digits}f}".rstrip("0").rstrip(".")


def short_hash(h: str) -> str:
    return h[:12]


def fixes_label(fixes: Sequence[str]) -> str:
    if not fixes:
        return "baseline"
    if tuple(fixes) == ALL_FIXES:
        return "all fixes"
    return " + ".join(FIX_LABEL.get(f, f) for f in fixes)


def fixes_key(fixes: Sequence[str]) -> str:
    return "+".join(fixes) if fixes else "baseline"


def config_label(backend: str, fixes: Sequence[str]) -> str:
    """'Claude (MCP) baseline', 'GPT (MCP) + self-check', 'Claude (MCP) + all fixes'."""
    name = BACKEND_LABEL.get(backend, backend)
    return f"{name} baseline" if not fixes else f"{name} + {fixes_label(fixes)}"


def uplift_fix_label(fix: str) -> str:
    return "all fixes" if fix == "all" else FIX_LABEL.get(fix, fix)


def safe_trace_href(path: str, run_id: str) -> str | None:
    """The link to an exported trace relative to the run folder, or None when unsafe.

    `results/<run_id>/traces/<file>.json` (what A2.3 writes) and `traces/<file>.json` both link
    as `traces/<file>.json`, which resolves next to `index.html` in the run folder. A scheme, an
    absolute path, a backslash, a `..` segment or an unusual file name is never linked.
    """
    if not path or "\\" in path or ":" in path or path.startswith("/"):
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    in_run = len(parts) == 2 or (len(parts) >= 3 and parts[-3] == run_id)
    if len(parts) >= 2 and parts[-2] == "traces" and _TRACE_NAME.match(parts[-1]) and in_run:
        return f"traces/{parts[-1]}"
    return None


def safe_http_url(url: str) -> str | None:
    """The URL when it is plain http(s) with a host, else None (javascript:, data:, ...)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if any(c.isspace() or ord(c) < 32 for c in url):
        return None
    return url
