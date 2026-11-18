"""B3.2: the hand-run Pages workflow keeps least privilege and refuses an empty site or the
synthetic example (REL-02, D-46). The shared policy check lives in
tests/harness/test_records.py; the release workflow's checks come with B3.3."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.harness.test_records import _workflow_files, workflow_problems

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_workflow_is_least_privilege_with_pages():
    files = _workflow_files(WORKFLOWS)
    assert "pages.yml" in {f.name for f in files}
    assert workflow_problems(files) == []


def test_pages_deploys_site_by_hand_and_refuses_the_example():
    pages = _load("pages.yml")
    assert pages[True] == {"workflow_dispatch": None}  # PyYAML reads `on` as True
    steps = pages["jobs"]["deploy"]["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-pages"))
    assert upload["with"]["path"] == "site"
    guard = next(s for s in steps if "SYNTHETIC EXAMPLE" in str(s.get("run", "")))
    assert "exit 1" in guard["run"] and "! grep" not in guard["run"]  # `!` would not fail under -e
    assert steps.index(guard) < steps.index(upload)


PAGES_TEXT = (WORKFLOWS / "pages.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("  workflow_dispatch:\n", "  workflow_dispatch:\n  push:\n", "triggers"),
        ("      pages: write\n", "      pages: write\n      contents: write\n", "grants"),
        ("      id-token: write", "      actions: write", "grants"),
        (
            "    environment:\n      name: github-pages\n",
            "    environment:\n      name: prod\n",
            "env",
        ),
        ("permissions:\n  contents: read\n", "permissions:\n  id-token: write\n", "top-level"),
        ("permissions:\n  contents: read\n", "permissions: write-all\n", "blanket"),
        (
            "    steps:\n",
            "    steps:\n      - run: echo ${{ secrets.PAGES_TOKEN }}\n",
            "references secrets",
        ),
        ("jobs:\n", "jobs:\n  other:\n    runs-on: ubuntu-latest\n    steps: []\n", "jobs"),
        ("    steps:\n", "    steps:\n      - run: uv publish\n", "publishes"),
    ],
)
def test_pages_policy_catches_planted_defects(tmp_path, old, new, why):
    """Each planted change to pages.yml is a policy problem (L-11: each edit matches once)."""
    assert PAGES_TEXT.count(old) == 1, old
    (tmp_path / "pages.yml").write_text(PAGES_TEXT.replace(old, new), encoding="utf-8")
    found = workflow_problems(_workflow_files(tmp_path))
    assert any(why in p for p in found), found


def test_pages_scans_the_site_for_secrets_before_upload():
    steps = _load("pages.yml")["jobs"]["deploy"]["steps"]
    scan = next(i for i, s in enumerate(steps) if "secret_scan.py site" in str(s.get("run", "")))
    upload = next(
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/upload")
    )
    assert scan < upload


# --- the guard script itself, run the way GitHub runs a `run:` step --------------------------

GH_BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="the guarded job runs on ubuntu; Windows bash may be WSL",
)

# The guard as first written: under `bash -e` a failing `test` on the left of `&&` does not stop
# the script, and the grep on a missing file is just a false `if`, so an empty site exited 0.
OLD_PAGES_GUARD = """test -f site/index.html && test -f site/summary.json
if grep -q "SYNTHETIC EXAMPLE" site/index.html; then
  echo "site/ holds the synthetic example, not results" >&2
  exit 1
fi
"""


def _step_script(workflow: str, job: str, name: str) -> str:
    steps = _load(workflow)["jobs"][job]["steps"]
    return next(s["run"] for s in steps if s.get("name") == name)


def _bash(script: str, cwd: Path, **env: str) -> int:
    path = cwd / "step.sh"
    path.write_text(script, encoding="utf-8")
    full = {**os.environ, **env}
    return subprocess.run([*GH_BASH, str(path)], cwd=cwd, env=full, capture_output=True).returncode


def _site(root: Path, *, index: str | None = None, summary: bool = True) -> Path:
    site = root / "site"
    site.mkdir(parents=True)
    if index is not None:
        (site / "index.html").write_text(index, encoding="utf-8")
    if summary:
        (site / "summary.json").write_text("{}", encoding="utf-8")
    return root


PAGES_GUARD = ("pages.yml", "deploy", "Refuse an empty site or the synthetic example")


@needs_bash
def test_pages_guard_refuses_an_empty_or_partial_site(tmp_path):
    guard = _step_script(*PAGES_GUARD)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "site").mkdir()
    assert _bash(guard, empty) != 0
    assert _bash(guard, _site(tmp_path / "no-index", summary=True)) != 0
    assert _bash(guard, _site(tmp_path / "no-summary", index="<p>ok</p>", summary=False)) != 0
    example = _site(tmp_path / "example", index="<p>SYNTHETIC EXAMPLE</p>")
    assert _bash(guard, example) != 0
    assert _bash(guard, _site(tmp_path / "ok", index="<p>results</p>")) == 0


@needs_bash
def test_the_old_pages_guard_let_an_empty_site_through(tmp_path):
    """Why the guard changed (L-1): the runner above catches this defect."""
    (tmp_path / "site").mkdir()
    assert _bash(OLD_PAGES_GUARD, tmp_path) == 0
    assert chained_test_problems(OLD_PAGES_GUARD)


def chained_test_problems(script: str) -> list[str]:
    """Lines where a failing command would not stop a `bash -e` step: an `&&` list anywhere but
    the last line (a retry loop's `... && exit 0` is the one allowed form)."""
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    return [
        ln for ln in lines[:-1] if re.search(r"&&", ln) and not re.search(r"&&\s*exit 0\s*$", ln)
    ]


def test_no_pages_step_hides_a_failure_in_an_and_list():
    for job_id, job in _load("pages.yml")["jobs"].items():
        for step in job.get("steps", []):
            script = str(step.get("run", ""))
            assert chained_test_problems(script) == [], f"pages.yml:{job_id}: {script}"


PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*$")


def test_pages_actions_are_pinned_to_commit_shas():
    for line in PAGES_TEXT.splitlines():
        if re.match(r"^\s*(?:-\s+)?uses:", line):
            assert PINNED.match(line), line.strip()
    assert not PINNED.match("      - uses: actions/checkout@v4")
