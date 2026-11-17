"""Print the hosted demo's Streamlit secrets as TOML, built from `.env`, for the clipboard (D-57).

    uv run python scripts/streamlit_secrets.py | pbcopy

then paste into the Streamlit Community Cloud app's Settings > Secrets. The output holds exactly
the variables the hosted demo reads: the page's `SECRETS` list with a value in `.env`, where
`SNOWFLAKE_RO_PRIVATE_KEY` is the PEM text read from the file named by
`SNOWFLAKE_RO_PRIVATE_KEY_PATH` (the host has no key file), as a TOML multi-line string. Never an
admin credential (`CLICKHOUSE_ADMIN_*`, `SNOWFLAKE_ADMIN_*`, `CLICKHOUSE_CLOUD_API_*`), never a
`.env` comment, never a local path.

Secrets go to stdout only. When stdout is a terminal the script refuses (exit 2) and says to
pipe it to `pbcopy`, so the values never land in a terminal, its scrollback or a transcript.
Errors name a variable and an exception type, never a value.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from agentic_analytics.demo.page import SECRETS
from agentic_analytics.env import load_dotenv

KEY_TEXT = "SNOWFLAKE_RO_PRIVATE_KEY"
KEY_PATH = "SNOWFLAKE_RO_PRIVATE_KEY_PATH"
FORBIDDEN = re.compile(r"^(CLICKHOUSE_ADMIN_|SNOWFLAKE_ADMIN_|CLICKHOUSE_CLOUD_API_)")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
TTY_REFUSAL = (
    "Refusing to print secrets to a terminal. Pipe them to the clipboard instead:\n"
    "    uv run python scripts/streamlit_secrets.py | pbcopy"
)
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
            "\r": "\\r"}  # fmt: skip


class SecretsError(Exception):
    """Why the secrets could not be built; the message names no value."""


def _escape(value: str, *, multiline: bool) -> str:
    out = []
    for ch in value:
        if multiline and ch in "\n\t":
            out.append(ch)
        elif ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def toml_line(name: str, value: str, *, multiline: bool = False) -> str:
    """One `NAME = "value"` line (a multi-line basic string for the PEM text)."""
    if not _BARE_KEY.fullmatch(name):
        raise SecretsError(f"{name!r} is not a plain TOML key")
    if multiline:  # the newline after the opening quotes is not part of the value
        return f'{name} = """\n{_escape(value, multiline=True)}"""'
    return f'{name} = "{_escape(value, multiline=False)}"'


def wanted_names(names: Sequence[str] = SECRETS) -> tuple[str, ...]:
    """The page's secrets list; refused outright if it ever names an admin credential."""
    bad = [n for n in names if FORBIDDEN.match(n)]
    if bad:
        raise SecretsError(f"admin credentials never go to the demo host: {', '.join(bad)}")
    return tuple(n for n in names if n != KEY_PATH)


def read_key_text(env: Mapping[str, str], base: Path) -> str | None:
    """The PEM text: from the file named by `SNOWFLAKE_RO_PRIVATE_KEY_PATH` (relative to the
    `.env` folder), else `SNOWFLAKE_RO_PRIVATE_KEY` itself, else None."""
    path = (env.get(KEY_PATH) or "").strip()
    if path:
        p = Path(path).expanduser()
        try:
            text = (p if p.is_absolute() else base / p).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise SecretsError(
                f"cannot read the key file named by {KEY_PATH} ({type(e).__name__})"
            ) from None
    else:
        text = env.get(KEY_TEXT) or ""
    if not text.strip():
        return None
    if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text:
        raise SecretsError(f"the key named by {KEY_PATH} is not a PEM private key")
    return text


def build_toml(env: Mapping[str, str], base: Path, names: Sequence[str] = SECRETS) -> str:
    """The secrets TOML for `env` (a parsed `.env`): only `names` with a value, in that order."""
    lines = []
    for name in wanted_names(names):
        if name == KEY_TEXT:
            text = read_key_text(env, base)
            if text is not None:
                lines.append(toml_line(name, text, multiline=True))
            continue
        value = (env.get(name) or "").strip()
        if value:
            lines.append(toml_line(name, value))
    return "\n".join(lines) + "\n" if lines else ""


def read_env(path: Path) -> dict[str, str]:
    """`.env` as a mapping (comments and blank values dropped); the process environment is
    not consulted, so only what the owner wrote in the file is copied."""
    if not path.is_file():
        raise SecretsError(f"no .env at {path}")
    env: dict[str, str] = {}
    load_dotenv(path, environ=env)
    return env


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    ap = argparse.ArgumentParser(prog="streamlit_secrets.py", description=(__doc__ or "").strip())
    ap.add_argument("--env", default=".env", help="the .env to read (default ./.env)")
    ns = ap.parse_args(argv)
    if out.isatty():
        print(TTY_REFUSAL, file=err)
        return 2
    path = Path(ns.env)
    try:
        text = build_toml(read_env(path), path.resolve().parent)
    except SecretsError as e:
        print(f"streamlit_secrets: {e}", file=err)
        return 1
    if not text:
        print("streamlit_secrets: .env holds none of the demo's variables", file=err)
        return 1
    try:  # the names come from the parsed TOML, never from the text (a key file line could
        names = list(tomllib.loads(text))  # hold " = ")
    except tomllib.TOMLDecodeError as e:
        print(f"streamlit_secrets: the TOML did not parse ({type(e).__name__})", file=err)
        return 1
    out.write(text)
    print(f"streamlit_secrets: wrote {len(names)} variables: {', '.join(names)}", file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
