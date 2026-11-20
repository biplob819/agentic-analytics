"""B3.3 (OWNER D-110) and V5.1 (OWNER D-115): each release was tagged, published to PyPI by
the release workflow's trusted publisher, and its smoke job passed against the published
package on Linux, macOS and Windows. These offline tests pin the committed record of each run."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import agentic_analytics
from secret_scan import scan

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ("0.1.0", "0.1.1")
JOBS = {
    "checks": "success",
    "build": "success",
    "publish": "success",
    "smoke (macos-latest)": "success",
    "smoke (ubuntu-latest)": "success",
    "smoke (windows-latest)": "success",
}


def release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def record(version: str) -> dict:
    path = ROOT / f"docs/measurements/release-v{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_release_has_its_record() -> None:
    found = sorted(p.name for p in (ROOT / "docs/measurements").glob("release-v*.json"))
    assert found == [f"release-v{v}.json" for v in VERSIONS]
    assert agentic_analytics.__version__ == VERSIONS[-1]  # a new version comes with its record


@pytest.mark.parametrize("version", VERSIONS)
def test_the_release_workflow_published_and_smoked_it(version: str) -> None:
    r = record(version)
    assert r["tag"] == f"v{version}" and r["workflow"] == "release.yml" and r["event"] == "push"
    assert r["conclusion"] == "success" and r["jobs"] == JOBS
    assert str(r["workflow_run_id"]) in r["workflow_run_url"]
    assert r["workflow_run_url"].startswith("https://github.com/biplob819/agentic-analytics/")


@pytest.mark.parametrize("version", VERSIONS)
def test_pypi_holds_the_wheel_and_sdist_of_this_version(version: str) -> None:
    p = record(version)["pypi"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert p["project"] == project["name"] == "agentic-analytics"
    assert p["version"] == version and p["requires_python"] == project["requires-python"]
    files = {f["packagetype"]: f for f in p["files"]}
    assert files["bdist_wheel"]["filename"] == f"agentic_analytics-{version}-py3-none-any.whl"
    assert files["sdist"]["filename"] == f"agentic_analytics-{version}.tar.gz"
    assert all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in p["files"])


@pytest.mark.parametrize("version", VERSIONS)
def test_the_tagged_commit_carries_the_released_version(version: str) -> None:
    r = record(version)
    assert re.fullmatch(r"[0-9a-f]{40}", r["commit"])
    tagged = subprocess.run(
        ["git", "show", f"{r['commit']}:pyproject.toml"], cwd=ROOT, capture_output=True, text=True
    )
    if tagged.returncode == 0:  # a shallow checkout may not hold the tagged commit
        assert tomllib.loads(tagged.stdout)["project"]["version"] == version
    assert release(agentic_analytics.__version__) >= release(version)


def test_releases_move_forward_on_distinct_commits() -> None:
    commits = [record(v)["commit"] for v in VERSIONS]
    assert len(set(commits)) == len(commits)
    assert [release(v) for v in VERSIONS] == sorted(release(v) for v in VERSIONS)


@pytest.mark.parametrize("version", VERSIONS)
def test_the_record_carries_no_secret(version: str) -> None:
    assert scan([ROOT / f"docs/measurements/release-v{version}.json"]) == []
