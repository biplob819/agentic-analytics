"""B3.3 (OSS-03, OSS-04): the docs exist, every link resolves to a file in the repository, and
every documented command parses with the real CLI parser."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import unquote

import pytest

from agentic_analytics.cli import BUILT, COMMANDS, build_parser

ROOT = Path(__file__).resolve().parents[2]
REPO_URL = re.compile(r"^https://github\.com/biplob819/agentic-analytics/(?:blob|tree)/main/(.+)$")
DOCS = [
    "quickstart",
    "reproduce-the-uk-benchmark",
    "writing-a-dataset-pack",
    "adding-questions",
    "cost",
    "versioning",
]
PAGES = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "PRIVACY.md",
    "CODE_OF_CONDUCT.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    *(f"docs/{d}.md" for d in DOCS),
]
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"```(?:bash|shell|sh|console)?\n(.*?)```", re.S)
PROGS = ("agentic-analytics", "aa")


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def link_problems(page: Path, text: str) -> list[str]:
    """Links in `text` that do not resolve to an existing file or folder of the repository."""
    problems = []
    for target in LINK.findall(text):
        m = REPO_URL.match(target)
        if m:
            path = ROOT / unquote(m.group(1).split("#")[0])
        elif re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue  # external sites and in-page anchors
        else:
            path = (page.parent / unquote(target.split("#")[0])).resolve()
        if not path.exists():
            problems.append(f"{page.name}: {target}")
    return problems


def commands(text: str) -> list[list[str]]:
    """Every `agentic-analytics` / `aa` invocation in a fenced shell block, as argv."""
    out = []
    for block in FENCE.findall(text):
        for line in block.splitlines():
            for part in re.split(r"&&|;", line):
                try:
                    argv = shlex.split(part, comments=True)
                except ValueError:
                    continue
                while argv[:2] in (["uv", "run"],) or argv[:1] == ["uvx"]:
                    argv = argv[2:] if argv[:1] == ["uv"] else argv[1:]
                if argv and argv[0] in PROGS:
                    out.append(argv[1:])
    return out


def command_problems(argvs: list[list[str]]) -> list[str]:
    """Documented commands the parser rejects. Built commands parse strictly; a command that is
    not built yet must at least be a known subcommand."""
    problems = []
    parser = build_parser()
    for argv in argvs:
        sub = next((a for a in argv if not a.startswith("-")), None)
        if sub is not None and sub not in COMMANDS:
            problems.append(f"unknown subcommand: {' '.join(argv)}")
            continue
        if sub is not None and sub not in BUILT:
            continue
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code != 0:  # --version and --help exit 0
                problems.append(f"does not parse: {' '.join(argv)}")
    return problems


def test_the_docs_exist():
    for rel in PAGES:
        assert _text(rel).startswith("# ") or rel.startswith(".github"), rel


@pytest.mark.parametrize("rel", PAGES)
def test_links_resolve_to_files(rel):
    assert link_problems(ROOT / rel, _text(rel)) == []


def test_readme_links_every_doc_and_record():
    readme = _text("README.md")
    for target in [*(f"docs/{d}.md" for d in DOCS[:4]), "docs/cost.md", "docs/versioning.md"]:
        assert f"/blob/main/{target}" in readme, target
    for record in ("docs/decisions.md", "DATA_CARD.md", "PRIVACY.md", "SECURITY.md"):
        assert f"/blob/main/{record}" in readme, record  # REL-03
    assert "Agentic%20Analytics%20Tech%20Spec.md" in readme
    assert "Agentic%20Analytics%20PRD.md" in readme


def test_link_check_bites(tmp_path):
    page = ROOT / "docs" / "quickstart.md"
    assert link_problems(page, "[x](no-such.md)") == ["quickstart.md: no-such.md"]
    assert link_problems(
        page, "[x](https://github.com/biplob819/agentic-analytics/blob/main/nope.md)"
    )
    assert link_problems(page, "[x](cost.md#spend-less) [y](https://example.org)") == []


@pytest.mark.parametrize("rel", PAGES)
def test_every_documented_command_exists_in_the_cli(rel):
    assert command_problems(commands(_text(rel))) == []


def test_docs_document_the_no_key_onboarding():
    found = {" ".join(a) for rel in PAGES for a in commands(_text(rel))}
    for cmd in ("init", "pack check", "report --example", "run --dry-run", "doctor"):
        assert cmd in found, cmd  # OSS-03
    for rel in ("README.md", "docs/quickstart.md"):  # single-engine mode and its stamp
        assert "single-engine gold" in _text(rel), rel


def test_versioning_states_the_semver_policy():
    """OSS-07: what is public, how it may change, deprecation, pack versions and releases."""
    text = _text("docs/versioning.md")
    for phrase in (
        "Semantic Versioning",
        "**The command line**",
        "**The results format**",
        "**The dataset pack format**",
        "**`run.yaml`**",
        "## Deprecation",
        "own `version`",
        "trusted publishing",
    ):
        assert phrase in text, phrase


def test_command_check_bites():
    assert command_problems([["frobnicate"]])
    assert command_problems([["report", "--no-such-flag"]])
    assert command_problems([["run", "--fix"]])  # missing value
    assert command_problems([["--version"], ["load", "--engine", "all"]]) == []
    fenced = "```bash\nuv run agentic-analytics run --dry-run  # estimate\naa smoke\n```\n"
    assert commands(fenced) == [["run", "--dry-run"], ["smoke"]]
