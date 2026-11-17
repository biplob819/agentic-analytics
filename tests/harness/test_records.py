import re
from pathlib import Path

import pytest

import status
from harness_lib import ROOT, load_catalog, load_graph

WORKFLOWS = ROOT / ".github" / "workflows"
SPEC = (ROOT / "Agentic Analytics Tech Spec.md").read_text(encoding="utf-8")


def test_decisions_seeded_d1_to_d31():
    text = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    ids = [int(n) for n in re.findall(r"^\| (?:OWNER )?D-(\d+) \|", text, re.M)]
    assert set(range(1, 32)) <= set(ids)
    assert len(ids) == len(set(ids)), "decision ids must be unique"


def test_lessons_and_changelog_exist():
    assert "L-1" in (ROOT / "docs" / "lessons.md").read_text(encoding="utf-8")
    assert (ROOT / "changelog.md").read_text(encoding="utf-8").startswith("# Changelog")


def test_graph_matches_tech_spec_node_table():
    section = SPEC[SPEC.index("### 12.2") : SPEC.index("### 12.3")]
    expected = dict(re.findall(r"^\| \*\*(\w\d\.\d)\*\* \| [^|]+ \| ([^|]+) \|", section, re.M))
    nodes = load_graph()
    assert len(expected) == 25  # N1.7 added by OWNER D-55, M4.1 by OWNER D-74
    assert set(nodes) == set(expected)
    for nid, deps in expected.items():
        want = [] if deps.strip() == "\u2014" else [d.strip() for d in deps.split(",")]
        assert nodes[nid].depends_on == want, nid


def test_catalog_matches_tech_spec_capabilities():
    section = SPEC[SPEC.index("### 12.4") : SPEC.index("### 12.5")]
    expected, node = [], None
    for line in section.splitlines():
        m = re.match(r"\*\*(\w\d\.\d)", line)
        if m:
            node = m.group(1)
            continue
        m = re.match(r"\| ([A-Z]{3}-\d\d) \| (.*) \| (.*?) \|$", line)
        if m:
            expected.append((m.group(1), node, f"{m.group(2)} [{m.group(3)}]"))
    caps = load_catalog()
    assert len(expected) == 155  # SET D-55, DEM-06..14 D-56..D-60, MON-01..06 D-74/D-75
    assert [(c.cap_id, c.node, c.note) for c in caps] == expected


def test_status_prints_graph_and_node(capsys):
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "W0.1" in out and "D4.2" in out and "capabilities green" in out
    assert status.main(["W0.1"]) == 0
    assert "HAR-07" in capsys.readouterr().out
    assert status.main(["Z9.9"]) == 1


def _yaml_code(path: Path) -> str:
    """Workflow text with comment-only lines and trailing comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split(" #", 1)[0].rstrip()
        if code.strip() and not code.lstrip().startswith("#"):
            lines.append(code)
    return "\n".join(lines)


def test_ci_workflow_covers_gates_and_matrix():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^on:\n  push:\n    branches: \[main\]$", ci, re.M)
    assert "pull_request" in ci
    for step in (
        "ruff check",
        "ruff format --check",
        "pyright",
        'pytest -q -m "not live"',
        "secret_scan.py",
        "verify_graph_eligibility.py",
        "uv build",
        "uv sync --locked",
    ):
        assert step in ci, step
    for v in ('"3.11"', '"3.12"'):
        assert v in ci
    for os_name in ("ubuntu-latest", "macos-latest"):
        assert os_name in ci


def workflow_problems(files: list[Path]) -> list[str]:
    """Why these workflows break least privilege or publish before B3.3 (empty means fine)."""
    problems = []
    if not files:
        return ["no workflows"]
    for f in files:
        code = _yaml_code(f)
        if re.search(r"\bsecrets\b", code):
            problems.append(f"{f.name}: references secrets")
        if re.search(r"write-all|:\s*write\b|id-token", code):
            problems.append(f"{f.name}: grants write or id-token")
        if re.search(r"uv publish|twine|pypi-publish|gh-action-pypi", code):
            problems.append(f"{f.name}: publishes a package")
        if not re.search(r"^permissions:\n  contents: read$", code, re.M):
            problems.append(f"{f.name}: no top-level read-only permissions")
        perms = re.findall(r"^\s*permissions:.*$", code, re.M)
        if len(perms) != 1:
            problems.append(f"{f.name}: permissions set more than once")
    return problems


def _workflow_files(d: Path) -> list[Path]:
    return sorted([*d.glob("*.yml"), *d.glob("*.yaml")])


@pytest.mark.contract
def test_workflows_grant_least_privilege_and_never_publish():
    assert workflow_problems(_workflow_files(WORKFLOWS)) == []


@pytest.mark.parametrize(
    "extra",
    [
        "      - run: uv publish --token ${{ secrets.PYPI }}\n",
        "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
        "      - run: echo '${{ toJSON(secrets) }}'\n",
        "    permissions: write-all\n",
        "    permissions:\n      id-token: write\n",
    ],
)
def test_workflow_check_catches_planted_defects(tmp_path, extra):
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    bad = tmp_path / "publish.yaml"  # .yaml, not .yml: both are read
    bad.write_text(ci.replace("    steps:\n", extra + "    steps:\n", 1), encoding="utf-8")
    assert workflow_problems(_workflow_files(tmp_path))


def test_private_files_stay_out_of_public_repo():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("Portfolio notes.md", ".env", ".env.*", "*.p8", "*.pem", "/data/", ".harness/"):
        assert entry in ignored, entry


def test_catalog_round_trip_is_byte_identical(tmp_path):
    from harness_lib import CATALOG_TSV, write_catalog

    out = tmp_path / "catalog.tsv"
    write_catalog(load_catalog(), out)
    assert out.read_bytes() == CATALOG_TSV.read_bytes()


def test_status_output_is_deterministic(capsys):
    status.main([])
    first = capsys.readouterr().out
    status.main([])
    assert capsys.readouterr().out == first


def test_gitattributes_force_lf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text
