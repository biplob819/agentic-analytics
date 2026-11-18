"""B3.2/B3.3: the hand-run Pages workflow keeps least privilege and refuses an empty site or
the synthetic example (REL-02, D-46); the release workflow checks, publishes by trusted
publishing and smokes the published package, and CI runs the 3 x 3 matrix (OSS-01, OSS-02,
OSS-03, OSS-07). The shared policy check lives in tests/harness/test_records.py (D-108)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from tests.harness.test_records import STRICT_SHELL, TRIGGERS, _workflow_files, workflow_problems

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_workflow_is_least_privilege_with_pages():
    files = _workflow_files(WORKFLOWS)
    assert "pages.yml" in {f.name for f in files}
    assert sorted(f.name for f in files) == sorted(TRIGGERS)
    assert workflow_problems(files) == []


def test_pages_deploys_site_by_hand_and_refuses_the_example():
    pages = _load("pages.yml")
    assert pages[True] == {"workflow_dispatch": None}  # PyYAML reads `on` as True
    steps = pages["jobs"]["build"]["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-pages"))
    assert upload["with"]["path"] == "site"
    guard = next(s for s in steps if "SYNTHETIC EXAMPLE" in str(s.get("run", "")))
    assert "exit 1" in guard["run"] and "! grep" not in guard["run"]  # `!` would not fail under -e
    assert steps.index(guard) < steps.index(upload)
    deploy = pages["jobs"]["deploy"]  # D-114: the privileged job only deploys the artifact
    assert deploy["needs"] == "build"
    assert [str(s.get("uses", "")).split("@")[0] for s in deploy["steps"]] == [
        "actions/deploy-pages"
    ]
    assert not any("run" in s for s in deploy["steps"])
    assert (
        "permissions" not in pages["jobs"]["build"] and "environment" not in pages["jobs"]["build"]
    )


RENDER = "uv run --locked --no-dev python scripts/publish_results.py --from-record"


def pages_build_problems(pages: dict) -> list[str]:
    """OWNER D-111, D-114: the read-only build job renders site/ from the committed record,
    before the guard, the secret scan and the upload; nothing else writes site/."""
    steps = pages["jobs"]["build"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    uses = [str(s.get("uses", "")) for s in steps]
    render = [i for i, r in enumerate(runs) if "publish_results.py" in r]
    if [runs[i].strip() for i in render] != [RENDER]:
        return [f"render step must be exactly `{RENDER}` once, found {render!r}"]
    i = render[0]
    problems = []
    later = {
        "guard": next((j for j, r in enumerate(runs) if "SYNTHETIC EXAMPLE" in r), -1),
        "scan": next((j for j, r in enumerate(runs) if "secret_scan.py site" in r), -1),
        "upload": next((j for j, u in enumerate(uses) if "upload-pages-artifact" in u), -1),
    }
    for name, j in later.items():
        if j <= i:
            problems.append(f"the {name} step must come after the render step")
    setup = next((j for j, u in enumerate(uses) if u.startswith("astral-sh/setup-uv@")), -1)
    if not 0 <= setup < i:
        problems.append("uv must be set up before the render step")
    if any(re.search(r"\bgit (?:add|commit|push)\b", r) for r in runs):
        problems.append("the deploy job must not commit the site")
    return problems


def test_pages_builds_the_site_from_the_committed_record():
    assert pages_build_problems(_load("pages.yml")) == []


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        (RENDER, "uv run python scripts/publish_results.py results/latest", "render step"),
        (f"        run: {RENDER}\n", "        run: echo skipped\n", "render step"),
        (
            "      - name: Render the dashboard from the committed record\n"
            f"        run: {RENDER}\n",
            "",
            "render step",
        ),
        (
            "      - name: Secret-scan the site, traces included\n"
            "        run: python3 scripts/secret_scan.py site\n",
            "",
            "scan step must come after",
        ),
        (
            "      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0\n"
            '        with:\n          python-version: "3.12"\n',
            "",
            "uv must be set up",
        ),
        (
            "        run: python3 scripts/secret_scan.py site\n",
            "        run: python3 scripts/secret_scan.py site\n"
            "      - run: git add site && git commit -m site && git push\n",
            "must not commit",
        ),
    ],
)
def test_pages_build_check_catches_planted_defects(old, new, why):
    assert PAGES_TEXT.count(old) == 1, old
    found = pages_build_problems(yaml.safe_load(PAGES_TEXT.replace(old, new)))
    assert any(why in p for p in found), found


def test_release_checks_publish_then_smoke_the_published_package():
    """OSS-01, OSS-02, OSS-03, OSS-07: nothing publishes before the checks pass; the smoke job
    installs the version just published on every OS and runs the no-key onboarding."""
    rel = _load("release.yml")
    checks = " ".join(str(s.get("run", "")) for s in rel["jobs"]["checks"]["steps"])
    for gate in ("ruff check", "ruff format --check", "pyright", "pytest", "secret_scan.py"):
        assert gate in checks, gate
    assert rel["jobs"]["build"]["needs"] == "checks"
    assert rel["jobs"]["publish"]["needs"] == "build"
    build = " ".join(str(s.get("run", "")) for s in rel["jobs"]["build"]["steps"])
    assert "GITHUB_REF_NAME" in build and "uv build" in build  # tag must match the version
    assert rel["jobs"]["smoke"]["needs"] == "publish"
    smoke = " ".join(str(s.get("run", "")) for s in rel["jobs"]["smoke"]["steps"])
    for cmd in (
        'uvx --refresh --from "agentic-analytics==$v" agentic-analytics --version',
        "aa --version",
        "agentic-analytics init",
        "agentic-analytics pack check",
        "agentic-analytics report --example",
    ):
        assert cmd in smoke, cmd


def test_release_builds_only_a_publishable_complete_package():
    """D-116 order: tag check < preflight < build < wheel check < wheel smoke < upload, all in
    the read-only build job; the publish job then downloads and publishes that artifact."""
    rel = _load("release.yml")
    steps = rel["jobs"]["build"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    uses = [str(s.get("uses", "")) for s in steps]
    tag = next(i for i, r in enumerate(runs) if "GITHUB_REF_NAME" in r)
    build = runs.index("uv build")
    pre = next(i for i, r in enumerate(runs) if "release_preflight.py pyproject" in r)
    wheel = next(i for i, r in enumerate(runs) if "release_preflight.py wheel dist" in r)
    smoke = next(i for i, r in enumerate(runs) if "uv pip install" in r and "dist/*.whl" in r)
    upload = next(i for i, u in enumerate(uses) if u.startswith("actions/upload-artifact@"))
    assert tag < pre < build < wheel < smoke < upload
    for cmd in ("--version", '"$bin/aa" --version', " init", " pack check", " report --example"):
        assert cmd in runs[smoke], cmd
    assert runs[smoke].count("env -i ") == 5  # an empty environment: no key can leak in
    assert steps[upload]["with"]["path"] == "dist/"
    pub = rel["jobs"]["publish"]["steps"]
    assert [str(s.get("uses", "")).split("@")[0] for s in pub if "uses" in s] == [
        "actions/download-artifact",
        "astral-sh/setup-uv",
    ]
    assert pub[0]["with"]["name"] == steps[upload]["with"]["name"]
    assert [s["run"] for s in pub if "run" in s] == [
        "uv publish --trusted-publishing always dist/*"
    ]
    ci = [str(s.get("run", "")) for s in _load("ci.yml")["jobs"]["test"]["steps"]]
    assert ci.index("uv build") < next(i for i, r in enumerate(ci) if "preflight.py wheel" in r)


def test_ci_matrix_is_three_by_three():
    matrix = _load("ci.yml")["jobs"]["test"]["strategy"]["matrix"]
    assert len(matrix["os"]) * len(matrix["python"]) == 9


PAGES_TEXT = (WORKFLOWS / "pages.yml").read_text(encoding="utf-8")
BUILD_STEPS = "  build:\n    runs-on: ubuntu-latest\n    steps:\n"


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
            BUILD_STEPS,
            BUILD_STEPS + "      - run: echo ${{ secrets.PAGES_TOKEN }}\n",
            "references secrets",
        ),
        ("jobs:\n", "jobs:\n  other:\n    runs-on: ubuntu-latest\n    steps: []\n", "jobs"),
        (BUILD_STEPS, BUILD_STEPS + "      - run: uv publish\n", "publishes"),
    ],
)
def test_pages_policy_catches_planted_defects(tmp_path, old, new, why):
    """Each planted change to pages.yml is a policy problem (L-11: each edit matches once)."""
    assert PAGES_TEXT.count(old) == 1, old
    (tmp_path / "pages.yml").write_text(PAGES_TEXT.replace(old, new), encoding="utf-8")
    found = workflow_problems(_workflow_files(tmp_path))
    assert any(why in p for p in found), found


def test_pages_scans_the_site_for_secrets_before_upload():
    steps = _load("pages.yml")["jobs"]["build"]["steps"]
    scan = next(i for i, s in enumerate(steps) if "secret_scan.py site" in str(s.get("run", "")))
    upload = next(
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/upload")
    )
    assert scan < upload


# --- the guard script itself, run the way GitHub runs a `run:` step --------------------------

GH_BASH = ["bash", "--noprofile", "--norc", "-euo", "pipefail"]  # the release jobs' shell
# D-117: release.yml's checks, build and publish jobs run under STRICT_SHELL
STRICT_BASH = STRICT_SHELL.removesuffix(" {0}").split()
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="the guarded jobs run on ubuntu; Windows bash may be WSL",
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


def _bash(script: str, cwd: Path, shell: Sequence[str] = GH_BASH, **env: str) -> int:
    path = cwd / "step.sh"
    path.write_text(script, encoding="utf-8")
    full = {**os.environ, **env}
    return subprocess.run([*shell, str(path)], cwd=cwd, env=full, capture_output=True).returncode


def _site(root: Path, *, index: str | None = None, summary: bool = True) -> Path:
    site = root / "site"
    site.mkdir(parents=True)
    if index is not None:
        (site / "index.html").write_text(index, encoding="utf-8")
    if summary:
        (site / "summary.json").write_text("{}", encoding="utf-8")
    return root


PAGES_GUARD = ("pages.yml", "build", "Refuse an empty site or the synthetic example")


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


TAG_CHECK = ("release.yml", "build", "Check the tag matches the package version")
needs_bash_and_uv = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("uv") is None,
    reason="the build job runs on ubuntu and reads the version with uv",
)


@needs_bash_and_uv
def test_tag_check_compares_tag_and_version(tmp_path):
    """OSS-07: the publish job refuses a tag that is not `v` + the pyproject version."""
    script = _step_script(*TAG_CHECK)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    strict = STRICT_BASH  # as the build job runs it
    assert _bash(script, tmp_path, strict, GITHUB_REF_NAME="v0.1.0") == 0
    assert _bash(script, tmp_path, strict, GITHUB_REF_NAME="v0.1.1") != 0
    assert _bash(script, tmp_path, strict, GITHUB_REF_NAME="0.1.0") != 0
    assert _bash(script, tmp_path, strict, GITHUB_REF_NAME="") != 0


def chained_test_problems(script: str) -> list[str]:
    """Lines where a failing command would not stop a `bash -e` step: an `&&` list anywhere but
    the last line (a retry loop's `... && exit 0` is the one allowed form)."""
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    return [
        ln for ln in lines[:-1] if re.search(r"&&", ln) and not re.search(r"&&\s*exit 0\s*$", ln)
    ]


def test_no_workflow_step_hides_a_failure_in_an_and_list():
    """Every step of every known workflow (B3.2 checked pages.yml; B3.3 checks them all)."""
    for name in TRIGGERS:
        for job_id, job in _load(name)["jobs"].items():
            for step in job.get("steps", []):
                script = str(step.get("run", ""))
                assert chained_test_problems(script) == [], f"{name}:{job_id}: {script}"


PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*$")


# The commit each pinned tag points to, looked up on GitHub (`gh api repos/<action>/git/ref/
# tags/<tag>`); a pin to any other commit, or a tag comment that does not match, fails.
EXPECTED_PINS = {
    "actions/checkout": ("v4.4.0", "11d5960a326750d5838078e36cf38b85af677262"),
    "astral-sh/setup-uv": ("v6.8.0", "d0cc045d04ccac9d8b7881df0226f9e82c39688e"),
    "actions/upload-pages-artifact": ("v3.0.1", "56afc609e74202658d3ffba0e8f6dda462b719fa"),
    "actions/deploy-pages": ("v4.0.5", "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e"),
    "actions/upload-artifact": ("v4.6.2", "ea165f8d65b6e75b540449e92b4886f43607fa02"),
    "actions/download-artifact": ("v4.3.0", "d3f86a106a0bac45b974a628896c90dbdf5c8093"),
}
USES = re.compile(r"^\s*(?:-\s+)?uses:\s+([\w.-]+/[\w./-]+)@([0-9a-f]{40}) # (v[\d.]+)$")


def _parsed_uses(data: object) -> list[str]:
    """Every `uses:` value in a parsed workflow, step or job, block or flow style (D-117)."""
    found: list[str] = []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    for job in (jobs or {}).values() if isinstance(jobs, dict) else []:
        if not isinstance(job, dict):
            continue
        if "uses" in job:
            found.append(str(job["uses"]))  # a reusable workflow
        for st in job.get("steps") or []:
            if isinstance(st, dict) and "uses" in st:
                found.append(str(st["uses"]))
    return found


def pin_problems(text: str) -> list[str]:
    """Each action a workflow uses, read from the parsed file (so `- {uses: x@main}` is seen),
    must be a reviewed action at the reviewed commit of its tag; and its line must carry that
    tag as a comment, so a reader sees the version without looking the commit up."""
    problems = []
    # each parsed use needs its own `uses: action@sha # tag` line: counted, not "any line"
    commented = Counter(
        f"{m[1]}@{m[2]}"
        for line in text.splitlines()
        if (m := USES.match(line))
        and PINNED.match(line)
        and EXPECTED_PINS.get(m[1]) == (m[3], m[2])
    )
    for value in _parsed_uses(yaml.safe_load(text)):
        action, _, ref = value.partition("@")
        _tag, sha = EXPECTED_PINS.get(action, ("", ""))
        if not sha:
            problems.append(f"not a reviewed action: {value}")
        elif ref != sha:
            problems.append(f"unexpected pin for {action}: {ref}")
        elif commented[value] > 0:
            commented[value] -= 1
        else:
            problems.append(f"not pinned to a commit with its tag comment: {value}")
    for line in text.splitlines():  # a `uses:` line that does not parse as a step still counts
        if re.match(r"^\s*(?:-\s+)?uses:", line) and not PINNED.match(line):
            problems.append(f"not pinned to a commit with its tag: {line.strip()}")
    return problems


def test_actions_are_pinned_to_commit_shas():
    """Every action in every known workflow is pinned to the reviewed commit of its tag."""
    for name in TRIGGERS:
        assert pin_problems((WORKFLOWS / name).read_text(encoding="utf-8")) == [], name
    assert not PINNED.match("      - uses: actions/checkout@v4")


_CI_STEPS = "    steps:\n"


@pytest.mark.parametrize(
    "step",
    [
        "      - uses: actions/checkout@v4\n",
        "      - uses: actions/checkout@" + "0" * 40 + " # v4.4.0\n",
        "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.3.0\n",
        "      - uses: someone/action@" + "a" * 40 + " # v1.0.0\n",
        # D-117: flow-style steps carry no `uses:` line for a line regex to find
        "      - {uses: evil/act@main}\n",
        "      - {name: x, uses: actions/checkout@v4}\n",
        "      - {uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262}\n",
        "      - uses: docker://alpine:3\n",
        "      - uses: ./local-action\n",
    ],
)
def test_pin_check_catches_planted_pins(step):
    """Each planted step, in a copy of ci.yml, is a pin problem (L-11: the edit matches once)."""
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert pin_problems(ci) == []
    assert ci.count(_CI_STEPS) == 1
    assert pin_problems(ci.replace(_CI_STEPS, _CI_STEPS + step))


def test_pin_check_reads_a_reusable_workflow_job():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    planted = ci.replace(
        "jobs:\n", "jobs:\n  evil: {uses: evil/repo/.github/workflows/x.yml@main}\n"
    )
    assert planted != ci
    assert pin_problems(planted) == [
        "not a reviewed action: evil/repo/.github/workflows/x.yml@main"
    ]


@needs_bash
def test_a_pipe_fails_under_the_release_shell(tmp_path):
    """D-117: without pipefail, `pytest | tee` exits with tee's 0; the release shell fails it."""
    script = "false | tee out.txt\n"
    assert _bash(script, tmp_path, ["bash", "--noprofile", "--norc", "-e"]) == 0
    assert _bash(script, tmp_path, STRICT_BASH) != 0
    assert _bash('echo "$UNSET_IN_THE_RELEASE"\n', tmp_path, STRICT_BASH) != 0
