"""SET-04: `.env` updates are atomic, mode 600, keep every other line, and never echo a value."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentic_analytics.env import load_dotenv
from agentic_analytics.setup import envfile
from agentic_analytics.setup.envfile import EnvFileError, read_env_file, update_env_file
from harness_lib import ROOT
from tests.file_modes import mode_problems

ORIGINAL = """\
# Copy to .env and fill in. Never commit .env.
ANTHROPIC_API_KEY=sk-ant-keep-this
  # an indented comment
export OPENAI_API_KEY="quoted value kept"
CLICKHOUSE_HOST=old.example.com
weird line without equals
CLICKHOUSE_HOST=duplicate-is-dropped
LANGFUSE_HOST=
"""


def test_update_keeps_other_lines_and_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(ORIGINAL, encoding="utf-8")
    changed = update_env_file(env, {"CLICKHOUSE_HOST": "new.clickhouse.cloud", "NEW_VAR": "v1"})
    assert changed == ["CLICKHOUSE_HOST", "NEW_VAR"]
    assert env.read_text(encoding="utf-8") == (
        "# Copy to .env and fill in. Never commit .env.\n"
        "ANTHROPIC_API_KEY=sk-ant-keep-this\n"
        "  # an indented comment\n"
        'export OPENAI_API_KEY="quoted value kept"\n'
        "CLICKHOUSE_HOST=new.clickhouse.cloud\n"
        "weird line without equals\n"
        "LANGFUSE_HOST=\n"
        "NEW_VAR=v1\n"
    )
    got: dict[str, str] = {}
    load_dotenv(env, got)
    assert got["CLICKHOUSE_HOST"] == "new.clickhouse.cloud"
    assert got["OPENAI_API_KEY"] == "quoted value kept"


def test_export_lines_are_replaced_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nexport B=2\nC=3\n", encoding="utf-8")
    update_env_file(env, {"B": "two"})
    assert env.read_text(encoding="utf-8") == "A=1\nB=two\nC=3\n"


def test_env_file_mode_is_600_and_write_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    os.chmod(env, 0o644)
    update_env_file(env, {"B": "2"})
    assert mode_problems(env, 0o600) == []
    new = tmp_path / "fresh" / ".env"
    update_env_file(new, {"X": "y"})
    assert new.read_text(encoding="utf-8") == "X=y\n" and mode_problems(new, 0o600) == []

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(envfile.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        update_env_file(env, {"A": "changed"})
    assert env.read_text(encoding="utf-8") == "A=1\nB=2\n"  # the old file, untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "fresh"]  # no temp file left


def test_update_is_idempotent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(ORIGINAL, encoding="utf-8")
    update_env_file(env, {"CLICKHOUSE_HOST": "h", "NEW_VAR": "v"})
    once = env.read_text(encoding="utf-8")
    assert update_env_file(env, {"CLICKHOUSE_HOST": "h", "NEW_VAR": "v"}) == []
    assert env.read_text(encoding="utf-8") == once


@pytest.mark.safety
@pytest.mark.parametrize("value", ["line\nbreak", "cr\rhere", "nul\0x"])
def test_values_with_line_breaks_are_refused_without_echo(tmp_path: Path, value: str) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(EnvFileError) as err:
        update_env_file(env, {"SECRET_THING": value})
    assert value not in str(err.value) and "SECRET_THING" in str(err.value)
    assert env.read_text(encoding="utf-8") == "A=1\n"


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("plain-Value_1.+/:@%^*,=", "K=plain-Value_1.+/:@%^*,="),
        ("~home", "K='~home'"),
        ("$HOME", "K='$HOME'"),
        ("has space", "K='has space'"),
        ("has#hash", "K='has#hash'"),
        ("it's", 'K="it\'s"'),
    ],
)
def test_values_are_quoted_so_load_dotenv_reads_them_back(
    tmp_path: Path, value: str, line: str
) -> None:
    env = tmp_path / ".env"
    update_env_file(env, {"K": value})
    assert env.read_text(encoding="utf-8") == line + "\n"
    assert read_env_file(env) == {"K": value}


def test_unwritable_names_and_quote_mixes_are_refused(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError):
        update_env_file(tmp_path / ".env", {"lower": "x"})
    with pytest.raises(EnvFileError):
        update_env_file(tmp_path / ".env", {"K": "both ' and \""})
    assert not (tmp_path / ".env").exists()


def test_symlinked_env_updates_its_target(tmp_path: Path) -> None:
    real = tmp_path / "real.env"
    real.write_text("A=1\n", encoding="utf-8")
    link = tmp_path / ".env"
    link.symlink_to(real)
    update_env_file(link, {"B": "2"})
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "A=1\nB=2\n"


@pytest.mark.safety
def test_clickhousectl_credentials_are_git_ignored() -> None:
    r = subprocess.run(
        ["git", "check-ignore", "-q", ".clickhouse/credentials.json"], cwd=ROOT, check=False
    )
    assert r.returncode == 0
