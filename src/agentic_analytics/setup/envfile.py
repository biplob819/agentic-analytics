"""Atomic `.env` updates that keep every other line (SET-04, OWNER D-55).

`update_env_file(path, {"NAME": "value"})` replaces the first `NAME=` (or `export NAME=`) line in
place, drops later duplicate assignments of the same name (the first one wins in `load_dotenv`,
so a stale duplicate would never be read anyway), appends names that were not there, and keeps
every other line and comment byte for byte. The new file is written next to the old one with
mode 600, flushed, then renamed over it, so a crash leaves either the old file or the new one.
Values are never echoed: errors name the variable, not its value.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from agentic_analytics.env import load_dotenv

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")  # as env.py
_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_\-.+/:@%^*,=]*$")  # no quoting needed
ENV_MODE = 0o600


class EnvFileError(ValueError):
    """A name or value that cannot be written to `.env` safely."""


def _render(name: str, value: str) -> str:
    if not _NAME.match(name):
        raise EnvFileError(f"{name!r} is not an environment variable name")
    # str.splitlines (which load_dotenv uses) also breaks on \v \f \x1c-\x1e \x85 \u2028
    # \u2029: any of them would let a value add a variable on a line of its own
    if not isinstance(value, str) or "\0" in value or value.splitlines() not in ([], [value]):
        raise EnvFileError(f"the value for {name} has a line break or NUL; not written")
    if _PLAIN_VALUE.match(value):
        return f"{name}={value}"
    if "'" not in value:
        return f"{name}='{value}'"
    if '"' not in value and "\\" not in value and "$" not in value and "`" not in value:
        return f'{name}="{value}"'
    raise EnvFileError(f"the value for {name} cannot be quoted safely; not written")


def _line_value(line: str) -> str | None:
    """The value `load_dotenv` reads from one assignment line (same rules as env.py)."""
    m = _LINE.match(line)
    if not m:
        return None
    value = m.group(2)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _split_lines(text: str) -> tuple[list[str], list[str]]:
    """Lines as `str.splitlines` (and so `load_dotenv`) sees them, and each one's line break."""
    lines: list[str] = []
    ends: list[str] = []
    for chunk in text.splitlines(keepends=True):
        body = chunk.splitlines()[0] if chunk.splitlines() else ""
        lines.append(body)
        ends.append(chunk[len(body) :])
    return lines, ends


def read_env_file(path: Path) -> dict[str, str]:
    """The NAME=value pairs `load_dotenv` would read from `path` (empty values left out)."""
    found: dict[str, str] = {}
    load_dotenv(path, found)
    return found


def update_env_file(path: Path, updates: Mapping[str, str]) -> list[str]:
    """Set each name in `updates`, atomically, chmod 600. Returns the names whose line changed."""
    for name in updates:
        if not _NAME.match(name):
            raise EnvFileError(f"{name!r} is not an environment variable name")
    target = path.resolve() if path.is_symlink() else path
    old = ""
    if target.is_file():
        with open(target, encoding="utf-8", newline="") as fh:  # no newline translation
            old = fh.read()
    # split where load_dotenv (str.splitlines) splits, so a name hidden after a \v, \x85 or
    # \u2028 is seen here too; every line we do not set keeps its bytes and its line break
    lines, ends = _split_lines(old)
    first: dict[str, str] = {}
    for line in lines:
        m = _ASSIGN.match(line)
        if m and m.group(1) in updates and m.group(1) not in first:
            first[m.group(1)] = line
    # a line that already reads back as the new value is kept as it is; the rest are rendered
    # (and refused) before anything is written
    rendered = {
        name: first[name]
        if name in first and _line_value(first[name]) == value
        else _render(name, value)
        for name, value in updates.items()
    }
    out: list[str] = []
    seen: set[str] = set()
    changed: list[str] = []
    for line, end in zip(lines, ends, strict=True):
        m = _ASSIGN.match(line)
        name = m.group(1) if m else None
        if name is None or name not in rendered:
            out.append(line + (end or "\n"))
            continue
        if name in seen:  # a later duplicate of a name we set: drop it
            if name not in changed:
                changed.append(name)
            continue
        seen.add(name)
        if line == rendered[name]:
            out.append(line + (end or "\n"))
        else:
            out.append(rendered[name] + "\n")
            changed.append(name)
    missing = [n for n in rendered if n not in seen]
    for name in missing:
        out.append(rendered[name] + "\n")
        changed.append(name)
    text = "".join(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=target.parent)
    try:
        os.chmod(tmp, ENV_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(target, ENV_MODE)
    return changed


__all__ = ["ENV_MODE", "EnvFileError", "read_env_file", "update_env_file"]
