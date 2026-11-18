"""B3.3: community files, issue templates and the privacy statement (OSS-05, OSS-06)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "agentic_analytics"
# Modules allowed to reach the network, each behind a destination listed in PRIVACY.md.
NETWORK_MODULES = {
    "models/anthropic.py": "Anthropic",
    "models/openai.py": "OpenAI",
    "mcp/client.py": "remote MCP endpoint",
    "backends/cortex.py": "Cortex Analyst",
    "warehouse/clickhouse.py": "ClickHouse service",
    "warehouse/snowflake.py": "Snowflake account",
    "load/clickhouse.py": "ClickHouse service",
    "load/snowflake.py": "Snowflake account",
    "load/snapshot.py": "Dataset download",
    "setup/clickhouse.py": "ClickHouse service",  # N1.7 role setup as the admin
    "setup/snowflake.py": "Snowflake account",  # N1.7 bootstrap SQL and key-pair login check
    "demo/keys.py": "visitor's own key",  # D4.1: SDK clients on a demo visitor's key (D-56)
    "demo/ask.py": "EVALKIT_DEMO_DATABASE_URL",  # psycopg, imported by name (the spend store)
    "demo/p95.py": "Playwright",  # drives a browser to the deployed page (DEM-05, DEM-15)
    "demo/page.py": "Streamlit",  # serves the demo page; its usage statistics are off
    "tracing.py": "Langfuse",
}
NETWORK_LIBS = {
    "anthropic",
    "openai",
    "mcp",
    "httpx",
    "httpx2",
    "requests",
    "aiohttp",
    "urllib.request",
    "http.client",
    "socket",
    "clickhouse_connect",
    "snowflake",
    "langfuse",
    "psycopg",
    "playwright",
    "streamlit",
    "ssl",
    "asyncio.open_connection",
}
_IMPORTERS = {"importlib.import_module", "import_module", "__import__"}


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for a Name/Attribute chain, else ''."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def network_importers(base: Path) -> set[str]:
    """Modules under `base` that import a networking or provider library anywhere."""
    found = set()
    for path in base.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif (
                isinstance(node, ast.Call)
                and _dotted(node.func) in _IMPORTERS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names = [node.args[0].value]  # importlib.import_module("x") / __import__("x")
            elif isinstance(node, ast.Attribute):
                names = [_dotted(node)]  # e.g. asyncio.open_connection(...)
            if any(n == lib or n.startswith(lib + ".") for n in names for lib in NETWORK_LIBS):
                found.add(path.relative_to(base).as_posix())
    return found


def test_privacy_statement_matches_the_code():
    """OSS-06: no telemetry, and only the modules behind PRIVACY.md's destinations go online."""
    privacy = _text("PRIVACY.md")
    assert "**agentic-analytics has no telemetry.**" in privacy
    assert network_importers(SRC) == set(NETWORK_MODULES)
    for destination in set(NETWORK_MODULES.values()):
        assert destination in privacy, destination


def test_network_check_bites(tmp_path):
    (tmp_path / "ok.py").write_text(
        "import json\nimport importlib\nimportlib.import_module('json')\n", encoding="utf-8"
    )
    (tmp_path / "phone_home.py").write_text(
        "def f():\n    from urllib.request import urlopen\n", encoding="utf-8"
    )
    (tmp_path / "sdk.py").write_text("import snowflake.connector\n", encoding="utf-8")
    planted = {
        "by_name.py": "import importlib\ndef f():\n    return importlib.import_module('psycopg')\n",
        "bare_name.py": "from importlib import import_module\nimport_module('requests')\n",
        "dunder.py": "def f():\n    return __import__('http.client')\n",
        "raw_socket.py": (
            "import asyncio\nasync def f():\n    await asyncio.open_connection('h', 1)\n"
        ),
        "tls.py": "def f():\n    import ssl\n    return ssl.create_default_context()\n",
        "browser.py": "import importlib\nimportlib.import_module('playwright.sync_api')\n",
    }
    for name, code in planted.items():
        (tmp_path / name).write_text(code, encoding="utf-8")
    assert network_importers(tmp_path) == {"phone_home.py", "sdk.py", *planted}


def test_streamlit_sends_no_usage_statistics():
    """PRIVACY.md: no telemetry, so the demo's Streamlit config turns its usage statistics off."""
    import tomllib

    config = tomllib.loads(_text(".streamlit/config.toml"))
    assert config["browser"]["gatherUsageStats"] is False
    assert "gatherUsageStats = false" in _text("PRIVACY.md")


def test_security_covers_keys_roles_and_reporting():
    sec = _text("SECURITY.md")
    for phrase in (
        "private vulnerability reporting",
        "never prints, logs or writes a key value",
        "Read-only by design",
        "analytics_ro",
        "ANALYTICS_RO",
        "SQL guard",
    ):
        assert phrase in sec, phrase


def test_contributing_and_conduct():
    contrib = _text("CONTRIBUTING.md")
    for phrase in ("uv run pyright", 'pytest -q -m "not live"', "3.11 to 3.13", "changelog.md"):
        assert phrase in contrib, phrase
    assert "marked\n`build`" in contrib and "fetches the build backend" in contrib
    conduct = _text("CODE_OF_CONDUCT.md")
    assert "Enforcement" in conduct and '"Conduct contact request"' in conduct


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")


def test_reporting_routes_need_no_personal_contact_detail():
    """A reporter without private vulnerability reporting asks for a channel in a detail-free
    public issue; no file names an email address or a personal profile as the route."""
    sec, conduct = _text("SECURITY.md"), _text("CODE_OF_CONDUCT.md")
    assert '"Security contact request"' in sec and "no details" in sec
    assert '"Conduct contact request"' in conduct and "no details" in conduct
    for rel in ("SECURITY.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "PRIVACY.md"):
        text = _text(rel)
        assert not EMAIL.search(text), rel
        assert "GitHub profile" not in text, rel
    assert EMAIL.search("write to someone@example.org")  # the check bites


def test_issue_and_pr_templates():
    tmpl = ROOT / ".github" / "ISSUE_TEMPLATE"
    bug = yaml.safe_load((tmpl / "bug_report.yml").read_text(encoding="utf-8"))
    assert "Never paste API keys" in bug["body"][0]["attributes"]["value"]
    feature = yaml.safe_load((tmpl / "feature_request.yml").read_text(encoding="utf-8"))
    assert feature["labels"] == ["enhancement"]
    config = yaml.safe_load((tmpl / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is False
    assert "security/advisories/new" in config["contact_links"][0]["url"]
    pr = _text(".github/PULL_REQUEST_TEMPLATE.md")
    assert "no test makes a paid call" in pr and "No hand-typed result numbers" in pr
