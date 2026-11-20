"""B3.3 (OWNER D-110): v0.1.0 was tagged, published to PyPI by the release workflow's trusted
publisher, and its smoke job passed against the published package on Linux, macOS and Windows.
These offline tests pin the committed record of that run."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import agentic_analytics
from secret_scan import scan

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/measurements/release-v0.1.0.json"


def release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_the_release_workflow_published_and_smoked_v0_1_0() -> None:
    r = record()
    assert r["tag"] == "v0.1.0" and r["workflow"] == "release.yml" and r["event"] == "push"
    assert r["conclusion"] == "success"
    assert r["jobs"] == {
        "checks": "success",
        "build": "success",
        "publish": "success",
        "smoke (macos-latest)": "success",
        "smoke (ubuntu-latest)": "success",
        "smoke (windows-latest)": "success",
    }
    assert str(r["workflow_run_id"]) in r["workflow_run_url"]
    assert r["workflow_run_url"].startswith("https://github.com/biplob819/agentic-analytics/")


def test_pypi_holds_the_wheel_and_sdist_of_this_version() -> None:
    p = record()["pypi"]
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert p["project"] == version["name"] == "agentic-analytics"
    assert p["version"] == "0.1.0" and record()["tag"] == f"v{p['version']}"
    assert p["requires_python"] == version["requires-python"]
    files = {f["packagetype"]: f for f in p["files"]}
    assert files["bdist_wheel"]["filename"] == "agentic_analytics-0.1.0-py3-none-any.whl"
    assert files["sdist"]["filename"] == "agentic_analytics-0.1.0.tar.gz"
    assert all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in p["files"])


def test_the_tagged_commit_carries_the_released_version() -> None:
    r = record()
    assert re.fullmatch(r"[0-9a-f]{40}", r["commit"])
    tagged = subprocess.run(
        ["git", "show", f"{r['commit']}:pyproject.toml"], cwd=ROOT, capture_output=True, text=True
    )
    if tagged.returncode == 0:  # a shallow checkout may not hold the tagged commit
        assert tomllib.loads(tagged.stdout)["project"]["version"] == r["pypi"]["version"]
    # later releases move the version forward, never back past a published one
    assert release(agentic_analytics.__version__) >= release(r["pypi"]["version"])


def test_the_record_carries_no_secret() -> None:
    assert scan([RECORD]) == []
