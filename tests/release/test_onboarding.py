"""B3.3 OSS-03: the no-key onboarding the README, the quickstart and the release smoke job
document, run as a user would, in an empty folder with no keys and no network."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Runs the real CLI with every outbound connection refused, so a command that tries to reach a
# provider or a warehouse fails loudly instead of passing on a machine that happens to be online.
NO_NETWORK = """
import socket, sys

def _refuse(*args, **kwargs):
    raise OSError("onboarding test: network is off")

socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = _refuse
socket.getaddrinfo = _refuse
from agentic_analytics.cli import main
sys.exit(main(sys.argv[1:]))
"""

ONBOARDING = [
    ["init"],
    ["pack", "check"],
    ["report", "--example", "--out", "example"],
    ["run", "--dry-run"],
    ["doctor"],
]


def _clean_env(home: Path) -> dict[str, str]:
    """Only what an interpreter needs: no key, account or Langfuse variable leaks in."""
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    return {**env, "HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"}


def _cli(argv: list[str], cwd: Path, script: str = NO_NETWORK) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=cwd,
        env=_clean_env(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_no_key_onboarding_runs_offline_in_an_empty_folder(tmp_path):
    """Every documented no-key command exits 0 with the network refused (OSS-03, PRIVACY.md:
    `init`, `pack check`, `report`, `run --dry-run` make no network requests)."""
    outputs = {}
    for argv in ONBOARDING:
        p = _cli(argv, tmp_path)
        assert p.returncode == 0, (argv, p.stdout, p.stderr)
        outputs[" ".join(argv)] = p.stdout + p.stderr
    assert (tmp_path / "run.yaml").is_file() and (tmp_path / ".env.example").is_file()
    assert "SYNTHETIC" in (tmp_path / "example" / "index.html").read_text(encoding="utf-8")
    assert "no calls made" in outputs["run --dry-run"]
    assert "network is off" not in "".join(outputs.values())
    assert not (tmp_path / ".cache").exists()  # nothing was asked, so nothing was cached


@pytest.mark.safety
def test_the_offline_harness_refuses_a_connection(tmp_path):
    """The check bites: the same harness makes a command that connects fail."""
    connect = NO_NETWORK.replace(
        "from agentic_analytics.cli import main\nsys.exit(main(sys.argv[1:]))",
        "socket.create_connection(('example.org', 443))",
    )
    p = _cli([], tmp_path, script=connect)
    assert p.returncode != 0 and "network is off" in p.stderr
