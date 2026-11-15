"""Load `.env` without overriding variables already set (no extra dependency)."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: str | Path = ".env", environ: MutableMapping[str, str] | None = None) -> int:
    """Set each NAME=value from `path` that is not already set; returns how many were set.

    Quotes around a value are removed; lines without `=` and comments are ignored.
    """
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name not in env and value:
            env[name] = value
            count += 1
    return count
