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
    assert len(expected) == 26  # N1.7 OWNER D-55, M4.1 OWNER D-74, V5.1 OWNER D-115
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
    # SET D-55, DEM-06..15 D-56..D-60/D-93, MON-01..09 D-74/D-75/OWNER D-98/OWNER D-115,
    # REL-05 OWNER D-107
    assert len(expected) == 160
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


def _load(path: Path) -> dict:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path.name
    return data


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
        "release_preflight.py wheel dist",
    ):
        assert step in ci, step
    matrix = _load(WORKFLOWS / "ci.yml")["jobs"]["test"]["strategy"]["matrix"]
    assert matrix["python"] == ["3.11", "3.12", "3.13"]  # OSS-02, D-45 (supersedes D-26)
    assert matrix["os"] == ["ubuntu-latest", "macos-latest", "windows-latest"]


# M4.1 MON-01: the one job that may read secrets, from its environment, and only these: the
# smoke run's model keys and read-only warehouse credentials, never an admin credential. It runs
# by hand only; its weekly schedule stays off until the release (OWNER D-103).
SECRET_JOBS = {("monitor.yml", "smoke"): "monitoring"}
SECRET_TRIGGERS = {"monitor.yml": {"workflow_dispatch": None}}
MONITOR_SECRETS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLICKHOUSE_MCP_KEY_ID",
        "CLICKHOUSE_MCP_KEY_SECRET",
        "CLICKHOUSE_SERVICE_ID",
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_RO_USER",
        "CLICKHOUSE_RO_PASSWORD",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_RO_USER",
        "SNOWFLAKE_RO_PRIVATE_KEY",
        "SNOWFLAKE_WAREHOUSE",
    }
)
_SECRET_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")
# B3.3 least privilege (OWNER D-27; D-108 replaces B3.2's narrow pages.yml rule with this policy
# for every workflow). Every workflow is known, has exact triggers and top-level
# `contents: read`. Only two jobs may hold more, each exactly this set and behind its
# environment: the release job that publishes to PyPI by trusted publishing, and the hand-run
# Pages deploy of site/ (REL-02, D-46). D-114: pages.yml's `build` job renders and uploads with
# the read-only token and no environment; its `deploy` job holds the Pages grants and only runs
# the pinned deploy action after `build` (STEP_ALLOW), with no checkout and no `run:` step.
# D-116: release.yml's `build` job builds and smokes the wheel with the read-only token, and its
# `publish` job holds `id-token: write` alone and runs no repository code (STEP_ALLOW).
TRUSTED = "uv publish --trusted-publishing always"
PUBLISH_CMD = f"{TRUSTED} dist/*"
JOB_GRANTS = {
    ("release.yml", "publish"): ({"id-token": "write"}, "pypi"),
    ("pages.yml", "deploy"): ({"pages": "write", "id-token": "write"}, "github-pages"),
}
JOBS = {
    "release.yml": {"checks", "build", "publish", "smoke"},
    "pages.yml": {"build", "deploy"},
}
# job -> (the job it must wait for, the only actions its steps may use, the only `run:` commands)
STEP_ALLOW = {
    ("pages.yml", "deploy"): ("build", ("actions/deploy-pages@",), ()),
    ("release.yml", "publish"): (
        "build",
        ("actions/download-artifact@", "astral-sh/setup-uv@"),
        (PUBLISH_CMD,),
    ),
}
DEPLOY_ONLY = {k: v for k, v in STEP_ALLOW.items() if k[0] == "pages.yml"}  # D-114's name
PUBLISH_ONLY = {k: v for k, v in STEP_ALLOW.items() if k[0] == "release.yml"}  # D-116
# release jobs whose failure must stop the release: no `if:`, no `continue-on-error`, and no
# `|| true` / `; true` that would swallow a failing command (D-116)
STRICT_JOBS = {("release.yml", "checks"), ("release.yml", "build"), ("release.yml", "publish")}
SWALLOW = re.compile(r"\|\|\s*(?:true\b|:(?=\s|$)|exit 0\b)|;\s*(?:true|:)\s*$|\bset \+e\b", re.M)
# contexts an outsider can set (an issue title, a branch name, a dispatch input): never in a `run:`
UNTRUSTED = re.compile(
    r"\$\{\{[^}]*\b(?:github\.event\.|github\.head_ref\b|inputs\.|github\.event_path)"
)
UV_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
TRIGGERS = {
    "ci.yml": {"push": {"branches": ["main"]}, "pull_request": None},
    "release.yml": {"push": {"tags": ["v*"]}},
    "pages.yml": {"workflow_dispatch": None},
    **SECRET_TRIGGERS,
}
RELEASE_JOB = ("release.yml", "publish")
PUBLISH = re.compile(r"uv publish|twine|pypi-publish|gh-action-pypi|flit publish|poetry publish")


def _env_name(job: dict) -> str | None:
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def _job_problems(name: str, job_id: str, job: dict) -> list[str]:
    """One job's grants, environment and publishing against JOB_GRANTS and RELEASE_JOB."""
    problems = []
    where = f"{name}:{job_id}"
    grants, env = JOB_GRANTS.get((name, job_id), (None, None))
    if "permissions" in job and job["permissions"] != grants:
        problems.append(f"{where}: grants {job['permissions']!r}")
    if grants is not None:
        if job.get("permissions") != grants:
            problems.append(f"{where}: must grant exactly {grants!r}")
        if _env_name(job) != env:
            problems.append(f"{where}: must run in environment {env!r}")
    runs = [str(st.get("run", "")) + " " + str(st.get("uses", "")) for st in job.get("steps", [])]
    publishing = [r for r in runs if PUBLISH.search(r)]
    if publishing and (name, job_id) != RELEASE_JOB:
        problems.append(f"{where}: publishes a package")
    for r in publishing:
        if r.strip() != PUBLISH_CMD or re.search(r"token|password|username", r, re.I):
            problems.append(f"{where}: publishes other than by trusted publishing")
    if _env_name(job) is not None and grants is None and (name, job_id) not in SECRET_JOBS:
        problems.append(f"{where}: runs in an environment without being allowed one")
    if (name, job_id) in STEP_ALLOW:
        needs, allowed, commands = STEP_ALLOW[(name, job_id)]
        if job.get("needs") != needs:
            problems.append(f"{where}: must wait for the {needs} job")
        for st in job.get("steps", []):
            uses = str(st.get("uses", ""))
            ok = (
                "run" not in st and uses.startswith(allowed)
                if "uses" in st
                else str(st.get("run", "")).strip() in commands
            )
            if not ok:
                problems.append(f"{where}: runs a step other than {allowed + commands!r}: {st!r}")
    if (name, job_id) == RELEASE_JOB and [r.strip() for r in publishing] != [PUBLISH_CMD]:
        problems.append(f"{where}: must run exactly `{PUBLISH_CMD}` once")
    if (name, job_id) in STRICT_JOBS:
        for key in ("if", "continue-on-error"):
            if key in job:
                problems.append(f"{where}: `{key}` on a release job")
        for st in job.get("steps", []):
            for key in ("if", "continue-on-error"):
                if key in st:
                    problems.append(f"{where}: `{key}` on a step ({st.get('name') or st})")
            if SWALLOW.search(str(st.get("run", ""))):
                problems.append(f"{where}: a run step swallows a failure: {st.get('run')!r}")
    if (name, job_id) == ("release.yml", "checks"):
        runs_only = [str(st.get("run", "")).strip() for st in job.get("steps", [])]
        if "uv sync --locked" not in runs_only:
            problems.append(f"{where}: must install with `uv sync --locked`")
    for st in job.get("steps", []):
        if UNTRUSTED.search(str(st.get("run", ""))):
            problems.append(f"{where}: an untrusted context in a run step: {st.get('run')!r}")
        uses = str(st.get("uses", ""))
        if name == "release.yml" and uses.startswith("astral-sh/setup-uv@"):
            w = st.get("with") or {}
            if (
                not UV_VERSION.match(str(w.get("version", "")))
                or w.get("enable-cache") is not False
            ):
                problems.append(f"{where}: setup-uv needs a pinned `version` and no cache")
    return problems


def _secret_problems(name: str, code: str, data: dict) -> list[str]:
    """Secrets only in SECRET_JOBS, each as a plain `${{ secrets.NAME }}` from MONITOR_SECRETS,
    in the steps of a job that runs in its environment (the triggers are checked for every
    workflow against TRIGGERS, which holds SECRET_TRIGGERS)."""
    import json

    allowed = {job: env for (wf, job), env in SECRET_JOBS.items() if wf == name}
    if not allowed:
        return [f"{name}: references secrets"]
    problems = []
    if re.search(r"\bsecrets\b", _SECRET_REF.sub("", code)):
        problems.append(f"{name}: references secrets other than as ${{{{ secrets.NAME }}}}")
    if "secrets" in json.dumps({str(k): v for k, v in data.items() if k != "jobs"}):
        problems.append(f"{name}: secrets outside a job's steps (workflow level)")
    for job_id, job in (data.get("jobs") or {}).items():
        text = json.dumps(job)
        if "secrets" not in text:
            continue
        if job_id not in allowed:
            problems.append(f"{name}:{job_id}: references secrets")
            continue
        if _env_name(job) != allowed[job_id]:
            problems.append(f"{name}:{job_id}: reads secrets outside {allowed[job_id]!r}")
        if "secrets" in json.dumps({k: v for k, v in job.items() if k != "steps"}):
            problems.append(f"{name}:{job_id}: secrets outside its steps")
        bad = sorted(set(_SECRET_REF.findall(text)) - MONITOR_SECRETS)
        if bad:
            problems.append(f"{name}:{job_id}: reads secrets {bad}")
    return problems


def workflow_problems(files: list[Path]) -> list[str]:
    """Why these workflows break least privilege (empty means fine).

    Publishing is allowed only in release.yml's `publish` job, on a v* tag, after `checks`, in
    environment `pypi`, by `uv publish --trusted-publishing always`, with `id-token: write`
    granted to that job alone (OSS-01). Pages deploys only from its hand-run `deploy` job,
    which runs nothing but the pinned deploy action after the read-only `build` job (D-114)
    (REL-02). Secrets are read only by monitor.yml's hand-run `smoke` job, in environment
    `monitoring`, and only the smoke run's model keys and read-only warehouse credentials
    (M4.1 MON-01, OWNER D-103). A workflow TRIGGERS does not know is a problem until it is
    reviewed and added here.
    """
    problems = []
    if not files:
        return ["no workflows"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        code = _yaml_code(f)
        if re.search(r"write-all|read-all", text):
            problems.append(f"{f.name}: blanket permissions")
        if re.search(r"(?i)pypi_token|pypi_api_token|UV_PUBLISH_(TOKEN|PASSWORD)", text):
            problems.append(f"{f.name}: names a stored PyPI token")
        if f.name not in TRIGGERS:
            if re.search(r"\bsecrets\b", code):
                problems.append(f"{f.name}: references secrets")
            problems.append(f"{f.name}: unknown workflow")
            continue
        try:
            data = _load(f)
        except Exception as exc:  # a workflow that does not parse cannot be checked
            problems.append(f"{f.name}: does not parse ({type(exc).__name__})")
            continue
        on = data.get("on", data.get(True))  # PyYAML reads a bare `on` key as True
        if on != TRIGGERS[f.name]:
            problems.append(f"{f.name}: triggers {on!r}, expected {TRIGGERS[f.name]!r}")
        if data.get("permissions") != {"contents": "read"}:
            problems.append(f"{f.name}: no top-level read-only permissions")
        granted = [job for wf, job in JOB_GRANTS if wf == f.name]
        # YAML keeps only the last of two equal keys, so count the blocks in the text as well
        if len(re.findall(r"^\s*permissions:.*$", code, re.M)) != 1 + len(granted):
            problems.append(f"{f.name}: permissions set more than once")
        if not granted and re.search(r":\s*write\b|id-token", code):
            problems.append(f"{f.name}: grants write or id-token")
        found = PUBLISH.findall(code)
        if found and f.name != RELEASE_JOB[0]:
            problems.append(f"{f.name}: publishes a package")
        elif f.name == RELEASE_JOB[0] and len(found) != 1:
            problems.append(f"{f.name}: publishes {len(found)} times, expected once")
        if re.search(r"\bsecrets\b", code):
            problems += _secret_problems(f.name, code, data)
        jobs = data.get("jobs") or {}
        if f.name in JOBS and set(jobs) != JOBS[f.name]:
            problems.append(f"{f.name}: jobs {sorted(jobs)!r}, expected {sorted(JOBS[f.name])!r}")
        for job_id in granted:
            if job_id not in jobs:
                problems.append(f"{f.name}: job {job_id} missing")
        for job_id, job in jobs.items():
            problems += _job_problems(f.name, job_id, job)
    return problems


def _workflow_files(d: Path) -> list[Path]:
    return sorted([*d.glob("*.yml"), *d.glob("*.yaml")])


@pytest.mark.contract
def test_workflows_grant_least_privilege_and_never_publish():
    """Nothing publishes except release.yml's trusted-publishing job (the name predates B3.3)."""
    files = _workflow_files(WORKFLOWS)
    assert {f.name for f in files} == set(TRIGGERS)
    assert workflow_problems(files) == []


def test_release_workflow_uses_trusted_publishing():
    """OSS-01, as EvalKit's: on tags only, environment pypi, id-token in that job alone."""
    rel = _yaml_code(WORKFLOWS / "release.yml")
    assert re.search(r"^on:\n  push:\n    tags: \[\"v\*\"\]$", rel, re.M)
    assert rel.count("id-token: write") == 1
    assert rel.count("uv publish") == 1 and PUBLISH_CMD in rel
    assert "environment: pypi" in rel
    assert re.search(r"permissions:\n\s+id-token: write\n\s+steps:", rel)
    assert "PYPI_TOKEN" not in rel and "password" not in rel and "secrets." not in rel
    data = _load(WORKFLOWS / "release.yml")
    holders = [j for j, job in data["jobs"].items() if "permissions" in job]
    assert holders == ["publish"]
    assert data["jobs"]["build"]["needs"] == "checks"
    assert data["jobs"]["publish"]["needs"] == "build"
    assert not any("checkout" in str(s.get("uses", "")) for s in data["jobs"]["publish"]["steps"])
    assert data["jobs"]["smoke"]["needs"] == "publish"
    assert data["jobs"]["smoke"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    ]


_STEPS = "    steps:\n"
_SMOKE_STEP = "      - name: The short alias and the no-key onboarding commands\n"
_PUBLISH_RUN = "      - run: uv publish --trusted-publishing always dist/*\n"
_PUBLISH_UV = (
    "          path: dist/\n"
    "      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0\n"
    "        with:\n"
)


@pytest.mark.parametrize(
    ("workflow", "old", "new", "why"),
    [
        # the committed W0.1 cases, planted into ci.yml's one job
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - run: uv publish --token ${{ secrets.PYPI }}\n",
            "ci.yml: publishes a package",
        ),
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
            "ci.yml: publishes a package",
        ),
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - run: echo '${{ toJSON(secrets) }}'\n",
            "ci.yml: references secrets",
        ),
        ("ci.yml", _STEPS, "    permissions: write-all\n" + _STEPS, "blanket permissions"),
        (
            "ci.yml",
            _STEPS,
            "    permissions:\n      id-token: write\n" + _STEPS,
            "ci.yml:test: grants",
        ),
        # B3.3: trusted publishing only from release.yml's publish job
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - run: uv publish --trusted-publishing always\n",
            "ci.yml:test: publishes a package",
        ),
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - run: echo ${{ secrets.OPENAI_API_KEY }}\n",
            "ci.yml: references secrets",
        ),
        (
            "ci.yml",
            "    branches: [main]\n",
            "    branches: [main]\n    tags: ['v*']\n",
            "triggers",
        ),
        ("ci.yml", "jobs:\n", "permissions:\n  contents: read\njobs:\n", "more than once"),
        ("release.yml", '    tags: ["v*"]\n', "    branches: [main]\n", "triggers"),
        ("release.yml", "    environment: pypi\n", "", "must run in environment 'pypi'"),
        (
            "release.yml",
            "uv publish --trusted-publishing always dist/*",
            "uv publish --token ${{ secrets.PYPI_TOKEN }} dist/*",
            "other than by trusted publishing",
        ),
        ("release.yml", "uv publish --trusted-publishing always dist/*", "uv publish", "trusted"),
        (
            "release.yml",
            "      - run: uv publish --trusted-publishing always dist/*\n",
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
            "release.yml:publish: runs a step other than",
        ),
        (
            "release.yml",
            "    environment: pypi\n",
            "    environment: pypi\n    env:\n      UV_PUBLISH_TOKEN: x\n",
            "names a stored PyPI token",
        ),
        (
            "release.yml",
            "permissions:\n  contents: read\n",
            "permissions:\n  id-token: write\n",
            "no top-level read-only permissions",
        ),
        (
            "release.yml",
            "    # Nothing is published from a commit whose checks fail.\n",
            "    permissions:\n      id-token: write\n",
            "release.yml:checks: grants",
        ),
        (
            "release.yml",
            "    needs: publish\n",
            "    needs: publish\n    permissions:\n      id-token: write\n",
            "release.yml:smoke: grants",
        ),
        (
            "release.yml",
            _SMOKE_STEP,
            "      - run: uv publish --trusted-publishing always dist/*\n" + _SMOKE_STEP,
            "release.yml:smoke: publishes a package",
        ),
        # D-116: publish waits for build, runs only the artifact download, uv and `uv publish`
        ("release.yml", "    needs: build\n", "", "release.yml:publish: must wait for the build"),
        (
            "release.yml",
            _PUBLISH_RUN,
            "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
            + _PUBLISH_RUN,
            "release.yml:publish: runs a step other than",
        ),
        (
            "release.yml",
            _PUBLISH_RUN,
            "      - run: curl -F file=@dist/x.whl https://example.org/upload\n" + _PUBLISH_RUN,
            "release.yml:publish: runs a step other than",
        ),
        (
            "release.yml",
            _PUBLISH_RUN,
            "      - uses: some/action@0123456789abcdef0123456789abcdef01234567 # v1\n"
            + _PUBLISH_RUN,
            "release.yml:publish: runs a step other than",
        ),
        (
            "release.yml",
            "    needs: build\n    runs-on: ubuntu-latest\n    environment: pypi\n",
            "    needs: build\n    runs-on: ubuntu-latest\n    environment: pypi\n"
            "    if: github.ref_type == 'tag'\n",
            "release.yml:publish: `if` on a release job",
        ),
        (
            "release.yml",
            "  build:\n    needs: checks\n",
            "  build:\n    needs: checks\n    continue-on-error: true\n",
            "release.yml:build: `continue-on-error` on a release job",
        ),
        (
            "release.yml",
            "      - name: Check the tag matches the package version\n",
            "      - name: Check the tag matches the package version\n        if: false\n",
            "release.yml:build: `if` on a step",
        ),
        (
            "release.yml",
            "      - name: Check the tag matches the package version\n",
            "      - name: Check the tag matches the package version\n"
            "        continue-on-error: true\n",
            "release.yml:build: `continue-on-error` on a step",
        ),
        (
            "release.yml",
            "      - run: uv run pyright\n",
            "      - run: uv run pyright || true\n",
            "swallows a failure",
        ),
        (
            "release.yml",
            "      - run: uv build\n",
            "      - run: uv build; true\n",
            "swallows a failure",
        ),
        (
            "release.yml",
            "release_preflight.py pyproject\n",
            "release_preflight.py pyproject || exit 0\n",
            "swallows a failure",
        ),
        (
            "release.yml",
            "      - run: uv sync --locked\n",
            "      - run: uv sync\n",
            "uv sync --locked",
        ),
        (
            "release.yml",
            _PUBLISH_UV + '          version: "0.11.21"',
            _PUBLISH_UV + '          version: "latest"',
            "release.yml:publish: setup-uv needs a pinned `version`",
        ),
        (
            "release.yml",
            "          enable-cache: false\n" + _PUBLISH_RUN,
            "          enable-cache: true\n" + _PUBLISH_RUN,
            "release.yml:publish: setup-uv needs a pinned `version` and no cache",
        ),
        # D-116: no attacker-controlled context is expanded inside a shell step
        (
            "ci.yml",
            _STEPS,
            _STEPS + "      - run: echo ${{ github.event.pull_request.title }}\n",
            "ci.yml:test: an untrusted context",
        ),
        (
            "ci.yml",
            _STEPS,
            _STEPS + '      - run: git checkout "${{ github.head_ref }}"\n',
            "ci.yml:test: an untrusted context",
        ),
        # the Pages deploy (REL-02): hand-run only, exactly its grants
        ("pages.yml", "  workflow_dispatch:\n", "  workflow_dispatch:\n  push:\n", "triggers"),
        (
            "pages.yml",
            "      pages: write\n",
            "      pages: write\n      contents: write\n",
            "pages.yml:deploy: grants",
        ),
        (
            "pages.yml",
            '          python-version: "3.12"\n',
            '          python-version: "3.12"\n        env:\n'
            "          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n",
            "pages.yml: references secrets",
        ),
        (
            "pages.yml",
            "  build:\n    runs-on: ubuntu-latest\n",
            "  build:\n    runs-on: ubuntu-latest\n    permissions:\n      pages: write\n",
            "pages.yml:build: grants",
        ),
        (
            "pages.yml",
            "  build:\n    runs-on: ubuntu-latest\n",
            "  build:\n    runs-on: ubuntu-latest\n    environment: github-pages\n",
            "pages.yml:build: runs in an environment",
        ),
        ("pages.yml", "    needs: build\n", "", "pages.yml:deploy: must wait for the build job"),
        (
            "pages.yml",
            "      - id: deployment\n",
            "      - run: ls site\n      - id: deployment\n",
            "pages.yml:deploy: runs a step other than",
        ),
        (
            "pages.yml",
            "      - id: deployment\n",
            "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
            "      - id: deployment\n",
            "pages.yml:deploy: runs a step other than",
        ),
        (
            "pages.yml",
            "      - id: deployment\n",
            "      - name: x\n"
            "        uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e\n"
            "        run: echo hi\n      - id: deployment\n",
            "pages.yml:deploy: runs a step other than",
        ),
        ("pages.yml", "  deploy:\n", "  deploy2:\n", "pages.yml: job deploy missing"),
        # M4.1 MON-01: the monitor's secrets stay in its smoke job, environment and allow-list
        (
            "monitor.yml",
            "  workflow_dispatch:\n",
            "  workflow_dispatch:\n  pull_request:\n",
            "triggers",
        ),
        ("monitor.yml", "  # schedule:\n  #   - cron:", "  schedule:\n    - cron:", "triggers"),
        ("monitor.yml", "    environment: monitoring\n", "", "outside 'monitoring'"),
        (
            "monitor.yml",
            "    timeout-minutes: 45\n",
            "    timeout-minutes: 45\n    permissions:\n      contents: write\n",
            "monitor.yml:smoke: grants",
        ),
    ],
)
def test_workflow_check_catches_planted_defects(tmp_path, workflow, old, new, why):
    """Each planted defect, in a copy of every real workflow, is named (L-11: the edit must
    match once, and the unplanted copy must be clean, or the case proves nothing)."""
    for f in _workflow_files(WORKFLOWS):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    assert workflow_problems(_workflow_files(tmp_path)) == []
    target = tmp_path / workflow
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    target.write_text(text.replace(old, new), encoding="utf-8")
    found = workflow_problems(_workflow_files(tmp_path))
    assert any(why in p for p in found), found


def test_workflow_check_catches_an_unreviewed_workflow(tmp_path):
    for f in _workflow_files(WORKFLOWS):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    (tmp_path / "publish.yaml").write_text(ci, encoding="utf-8")  # .yaml, not .yml: both read
    assert workflow_problems(_workflow_files(tmp_path)) == ["publish.yaml: unknown workflow"]


MONITOR_ENV_LINE = "SNOWFLAKE_WAREHOUSE: ${{ secrets.SNOWFLAKE_WAREHOUSE }}\n"


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n", "triggers"),
        ("  # schedule:\n  #   - cron:", "  schedule:\n    - cron:", "triggers"),
        ("    environment: monitoring\n", "", "outside 'monitoring'"),
        ("    environment: monitoring\n", "    environment: production\n", "outside"),
        (
            MONITOR_ENV_LINE,
            MONITOR_ENV_LINE + "          CLICKHOUSE_ADMIN_PASSWORD: "
            "${{ secrets.CLICKHOUSE_ADMIN_PASSWORD }}\n",
            "reads secrets ['CLICKHOUSE_ADMIN_PASSWORD']",
        ),
        (
            "        run: uv run agentic-analytics smoke",
            "        run: echo '${{ toJSON(secrets) }}'",
            "other than as",
        ),
        (
            "    timeout-minutes: 45\n",
            "    timeout-minutes: 45\n    env:\n      K: ${{ secrets.OPENAI_API_KEY }}\n",
            "secrets outside its steps",
        ),
        (
            "permissions:\n  contents: read\n",
            "env:\n  K: ${{ secrets.OPENAI_API_KEY }}\npermissions:\n  contents: read\n",
            "secrets outside a job's steps (workflow level)",
        ),
    ],
)
def test_monitor_secrets_stay_in_its_smoke_job(tmp_path, old, new, why):
    """M4.1 MON-01: the monitor's secrets stay in its hand-run smoke job, environment and
    allow-list; each planted defect is caught (L-11: the edit must match once)."""
    text = (WORKFLOWS / "monitor.yml").read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    (tmp_path / "monitor.yml").write_text(text.replace(old, new), encoding="utf-8")
    found = workflow_problems(_workflow_files(tmp_path))
    assert any(why in p for p in found), found


def test_secrets_in_any_other_workflow_are_refused(tmp_path):
    text = (WORKFLOWS / "monitor.yml").read_text(encoding="utf-8")
    (tmp_path / "other.yml").write_text(text, encoding="utf-8")
    assert "other.yml: references secrets" in workflow_problems(_workflow_files(tmp_path))


def test_private_files_stay_out_of_public_repo():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (
        "Portfolio notes.md",
        ".env",
        ".env.*",
        "*.p8",
        "*.pem",
        "/data/",
        ".harness/",
        "/site/",  # built by the pages workflow (OWNER D-111), never committed
    ):
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
