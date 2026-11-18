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
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "socketserver",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc.client",
    "websocket",
    "websockets",
    "paramiko",
    "pycurl",
    "httplib2",
    "grpc",
    "boto3",
    "botocore",
    "clickhouse_connect",
    "snowflake",
    "langfuse",
    "psycopg",
    "playwright",
    "streamlit",
    "ssl",
    "asyncio.open_connection",
}
# an event loop's or socket helper's connection calls, by attribute or bare name, whatever the
# object is called (`loop.create_connection(...)`, `create_connection(...)`)
NETWORK_CALLS = {
    "create_connection",
    "open_connection",
    "create_datagram_endpoint",
    "create_unix_connection",
    "open_unix_connection",
    "sock_connect",
    "create_server",
    "start_server",
}
_IMPORTERS = {
    "importlib.import_module",
    "importlib.__import__",
    "builtins.__import__",
    "__import__",
}
# modules allowed to import by a computed name (none today): each one is a reviewed exception
DYNAMIC_IMPORTS: dict[str, str] = {}
# Modules that start a process, and what it is; PRIVACY.md must name any that goes online
# (D-117). The git lookups read the local checkout only (`git_subcommands` below proves it).
SUBPROCESS_MODULES = {
    "setup/clickhousectl.py": "clickhousectl",  # the ClickHouse Cloud API (PRIVACY.md)
    "run/runner.py": "git",  # local: rev-parse, ls-files, status
    "demo/p95.py": "git",  # local: rev-parse
}
LOCAL_GIT = {"rev-parse", "ls-files", "status"}
_SPAWNERS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "os.system",
    "os.popen",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "pty.spawn",
}
_SPAWN_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
NETWORK_TOOLS = {
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "socat",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "telnet",
    "ftp",
}
GIT_NETWORK = {"clone", "fetch", "pull", "push", "ls-remote", "remote", "submodule", "send-email"}


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


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local names bound to a module or a module's attribute: `import x as y` (y -> x),
    `from a import b as c` (c -> a.b) and `n = a.b` (n -> a.b, followed through earlier
    aliases), so `im = importlib.import_module; im("requests")` resolves to the importer."""
    names: dict[str, str] = {}
    for _ in range(3):  # a chain of re-bindings settles in a few passes
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        names[a.asname] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    names[a.asname or a.name] = f"{node.module}.{a.name}"
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                value = _resolve(node.value, names) if node.value is not None else ""
                if not value:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        names[t.id] = value
    return names


def _resolve(node: ast.AST, names: dict[str, str]) -> str:
    dotted = _dotted(node)
    if not dotted:
        return ""
    head, _, rest = dotted.partition(".")
    if head in names:
        return names[head] + ("." + rest if rest else "")
    return dotted


def _strings(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _spawns(name: str) -> bool:
    return name in _SPAWNERS or name.startswith(_SPAWN_PREFIXES)


def _argv_words(call: ast.Call) -> list[str]:
    """Every word of every string constant in a process call's command (list or shell form)."""
    args = list(call.args[:1]) + [k.value for k in call.keywords if k.arg in ("args", "cmd")]
    return [w for a in args for s in _strings(a) for w in s.split()]


def _network_argv(words: list[str]) -> bool:
    tools = {w.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe") for w in words}
    if tools & NETWORK_TOOLS:
        return True
    return "git" in tools and bool(set(words) & GIT_NETWORK)


def network_importers(base: Path) -> set[str]:
    """Modules under `base` that import a networking or provider library anywhere, connect a
    socket by an event loop's call, import by a computed name (unless DYNAMIC_IMPORTS lists
    them), or start a network tool (curl, ssh, `git fetch`, ...) as a process."""
    found = set()
    for path in base.rglob("*.py"):
        rel = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _aliases(tree)
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Call):
                func = _resolve(node.func, names)
                if func in _IMPORTERS:
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        mods = [arg.value]  # importlib.import_module("x") / __import__("x")
                    elif rel not in DYNAMIC_IMPORTS:
                        found.add(rel)  # a computed name could be any library
                elif _dotted(node.func) == "getattr" and set(_strings(node)) & {
                    "import_module",
                    "__import__",
                }:
                    found.add(rel)  # getattr(importlib, "import_module")
                elif _spawns(func) and _network_argv(_argv_words(node)):
                    found.add(rel)
                elif func.rsplit(".", 1)[-1] in NETWORK_CALLS:
                    found.add(rel)  # loop.create_connection(...), create_connection(...)
            elif isinstance(node, ast.Attribute):
                mods = [_resolve(node, names)]  # e.g. asyncio.open_connection, an aliased module
            if any(n == lib or n.startswith(lib + ".") for n in mods for lib in NETWORK_LIBS):
                found.add(rel)
    return found


def subprocess_modules(base: Path) -> set[str]:
    """Modules under `base` that start a process by any spelling (aliases followed)."""
    found = set()
    for path in base.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _spawns(_resolve(node.func, names)):
                found.add(path.relative_to(base).as_posix())
    return found


def git_subcommands(path: Path) -> set[str]:
    """The git subcommand of every git process `path` starts: the word after `git` (and any
    `-C dir`) in a literal argv, or the first argument of each call of a local `_git` helper."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _dotted(node.func) == "_git" and len(node.args) > 1:
            arg = node.args[1]
            found.add(str(arg.value) if isinstance(arg, ast.Constant) else "<computed>")
        elif node.args and isinstance(node.args[0], ast.List | ast.Tuple):
            items = node.args[0].elts
            if items and isinstance(items[0], ast.Constant) and items[0].value == "git":
                rest = items[1:]
                if rest and isinstance(rest[0], ast.Constant) and rest[0].value == "-C":
                    rest = rest[2:]
                if rest and isinstance(rest[0], ast.Constant):
                    found.add(str(rest[0].value))
                elif rest and not isinstance(rest[0], ast.Starred):
                    found.add("<computed>")
    return found


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_privacy_statement_matches_the_code():
    """OSS-06: no telemetry, and only the modules behind PRIVACY.md's destinations go online."""
    privacy = _text("PRIVACY.md")
    assert "**agentic-analytics has no telemetry.**" in privacy
    assert network_importers(SRC) == set(NETWORK_MODULES)
    for destination in set(NETWORK_MODULES.values()):
        assert destination in privacy, destination


def test_every_process_the_code_starts_is_listed_and_local_or_documented():
    """D-117: a process can reach the network too. Each module that starts one is listed; git
    runs only local subcommands; clickhousectl (the ClickHouse Cloud API) is in PRIVACY.md."""
    assert subprocess_modules(SRC) == set(SUBPROCESS_MODULES)
    privacy = _text("PRIVACY.md")
    for rel, tool in SUBPROCESS_MODULES.items():
        if tool == "git":
            subs = git_subcommands(SRC / rel)
            assert subs and subs <= LOCAL_GIT, (rel, subs)
        else:
            assert f"`{tool}`" in privacy, tool
    assert "git" in privacy  # the code-version lookup is named as local


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
        # D-117: the verifier's survivors of the D-116 check
        "aliased.py": "import importlib\nim = importlib.import_module\nim('requests')\n",
        "aliased_from.py": "from importlib import import_module as load\nload('httpx')\n",
        "aliased_module.py": "import importlib as il\nf = il.import_module\ng = f\ng('openai')\n",
        "computed.py": "import importlib\ndef f(name):\n    return importlib.import_module(name)\n",
        "computed_dunder.py": "def f(n):\n    return __import__('re' + n)\n",
        "getattr_importer.py": "import importlib\ngetattr(importlib, 'import_module')('x')\n",
        "loop.py": (
            "import asyncio\nasync def f(p):\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.create_connection(p, 'h', 1)\n"
        ),
        "bare_create.py": "def f(create_connection):\n    return create_connection(('h', 1))\n",
        "urllib3_pool.py": "import urllib3\nurllib3.PoolManager().request('GET', 'https://x')\n",
        "curl.py": "import subprocess\nsubprocess.run(['curl', '-d', '@f', 'https://x'])\n",
        "curl_path.py": "import subprocess\nsubprocess.run(['/usr/bin/curl', 'https://x'])\n",
        "wget_shell.py": "import os\nos.system('wget -q https://x')\n",
        "ssh_alias.py": "from subprocess import Popen as P\nP(['ssh', 'host', 'ls'])\n",
        "nc_kw.py": "import subprocess\nsubprocess.check_output(args=['nc', 'h', '1'])\n",
        "scp_exec.py": "import os\nos.execvp('scp', ['scp', 'f', 'h:'])\n",
        "git_push.py": "import subprocess\nsubprocess.run(['git', '-C', '.', 'push'])\n",
    }
    for name, code in planted.items():
        (tmp_path / name).write_text(code, encoding="utf-8")
    (tmp_path / "local_proc.py").write_text(
        "import subprocess\nsubprocess.run(['git', 'rev-parse', 'HEAD'])\n"
        "subprocess.run(['clickhousectl', 'cloud', 'service', 'list', '--json'])\n",
        encoding="utf-8",
    )
    (tmp_path / "loop_ok.py").write_text(
        "import asyncio\nloop = asyncio.new_event_loop()\nloop.run_until_complete(None)\n",
        encoding="utf-8",
    )
    assert network_importers(tmp_path) == {"phone_home.py", "sdk.py", *planted}


def test_process_checks_bite(tmp_path):
    (tmp_path / "a.py").write_text("import subprocess\nsubprocess.run(['ls'])\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from os import system as s\ns('ls')\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("import subprocess\n", encoding="utf-8")  # imported, never run
    assert subprocess_modules(tmp_path) == {"a.py", "b.py"}
    git = tmp_path / "g.py"
    git.write_text(
        "import subprocess\n"
        "def _git(folder, *args):\n"
        "    return subprocess.run(['git', '-C', str(folder), *args])\n"
        "_git('.', 'rev-parse', 'HEAD')\n"
        "_git('.', 'fetch', 'origin')\n"
        "subprocess.run(['git', 'pull'])\n",
        encoding="utf-8",
    )
    assert git_subcommands(git) == {"rev-parse", "fetch", "pull"}
    assert not git_subcommands(git) <= LOCAL_GIT


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
