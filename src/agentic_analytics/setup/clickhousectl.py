"""A thin, injectable wrapper around the `clickhousectl` CLI (SET-01, SET-02).

Every call is `clickhousectl cloud <args> --json`. `clickhousectl` finds the admin Cloud API key
itself (saved credentials from `cloud auth login --interactive` in `./.clickhouse/`, else
CLICKHOUSE_CLOUD_API_KEY / CLICKHOUSE_CLOUD_API_SECRET from the shell or `.env`), so no secret
is ever put on a command line where `ps` could see it. Tests inject a fake `Runner`; nothing
here opens a network connection itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_analytics.setup._common import SecretBox, SetupError, json_values, parse_json

EXIT_AUTH_REQUIRED = 4  # clickhousectl: "auth required"
DEFAULT_TIMEOUT_S = 180
AUTH_HINT = (
    "clickhousectl has no admin Cloud API key: run `clickhousectl cloud auth login "
    "--interactive` (it saves the key in ./.clickhouse/, which git ignores) or set "
    "CLICKHOUSE_CLOUD_API_KEY and CLICKHOUSE_CLOUD_API_SECRET, then run setup again"
)


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Mapping[str, str]], Completed]


def subprocess_runner(argv: Sequence[str], env: Mapping[str, str]) -> Completed:  # pragma: no cover
    """Run a command with captured text output and a timeout (the real runner)."""
    try:
        p = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(env),
            timeout=DEFAULT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"timed out after {DEFAULT_TIMEOUT_S}s")
    return Completed(p.returncode, p.stdout or "", p.stderr or "")


def find_clickhousectl(explicit: str | None = None) -> str:
    """The clickhousectl executable: `explicit`, then PATH, then ~/.local/bin."""
    if explicit:
        if not Path(explicit).is_file():
            raise SetupError(f"--clickhousectl {explicit} does not exist")
        return explicit
    found = shutil.which("clickhousectl")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "clickhousectl"
    if local.is_file():
        return str(local)
    raise SetupError(
        "clickhousectl is not installed: install clickhousectl 0.5 or later and put it on PATH, "
        "or pass --clickhousectl PATH"
    )


class Clickhousectl:
    """`clickhousectl cloud ... --json`, with exit codes turned into clear SetupErrors."""

    def __init__(
        self,
        executable: str,
        runner: Runner,
        environ: Mapping[str, str],
        secrets: SecretBox,
    ) -> None:
        self.executable, self.runner, self.secrets = executable, runner, secrets
        self.environ = clickhousectl_env(environ)
        self.calls: list[tuple[str, ...]] = []

    def json(self, *args: str) -> Any:
        argv = (self.executable, "cloud", *args, "--json")
        self.calls.append(tuple(args))
        shown = "clickhousectl cloud " + " ".join(args[:3])
        done = self.runner(argv, self.environ)
        for text in (done.stdout, done.stderr):  # a secret printed anywhere, even on failure
            for value in json_values(text):  # every document, not only the one parsed
                self.secrets.add_from_json(value)
        if done.returncode == EXIT_AUTH_REQUIRED:
            raise SetupError(AUTH_HINT)
        if done.returncode != 0:
            # scrub first: a cut or re-spaced secret would no longer match
            detail = self.secrets.scrub(done.stderr or done.stdout or "")
            detail = " ".join(detail.split())[:400]
            raise SetupError(f"`{shown}` failed (exit {done.returncode}): {detail}")
        return parse_json(done.stdout, shown)


def clickhousectl_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for clickhousectl: ours, with its telemetry off (`DO_NOT_TRACK=1`, as
    `clickhousectl telemetry --help` documents; it writes no config)."""
    env = dict(os.environ if environ is None else environ)
    env["DO_NOT_TRACK"] = "1"  # forced: a DO_NOT_TRACK=0 in the shell must not turn it back on
    return env


__all__ = [
    "AUTH_HINT",
    "Clickhousectl",
    "Completed",
    "Runner",
    "clickhousectl_env",
    "find_clickhousectl",
    "subprocess_runner",
]
