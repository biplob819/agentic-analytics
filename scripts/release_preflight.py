#!/usr/bin/env python3
"""Release gates the workflows run around `uv build` (B3.3: OSS-01, OSS-07).

    python scripts/release_preflight.py pyproject [pyproject.toml]
    python scripts/release_preflight.py wheel DIST_DIR_OR_WHEEL

`pyproject` fails while the project still depends on a direct reference (a `git+` URL or any
`name @ url` requirement) or sets `allow-direct-references = true`: PyPI rejects such a package,
so the release workflow refuses to build it (OWNER D-47 switches EvalKit to PyPI first).

`wheel` fails unless the built wheel carries the bundled dataset pack and the SYNTHETIC example,
without which `pack check`, `run` and `report --example` break after `pip install`.

Standard library only, so it runs before `uv sync`. Exit 0 on pass, 1 on a failed gate, 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "agentic_analytics"
PACK = f"{PACKAGE}/packs/uk_price_paid"

# Files the wheel must carry, as fnmatch patterns over wheel member names; each must match at
# least one member. A test keeps every pattern matched by the source tree too.
WHEEL_REQUIRED: tuple[str, ...] = (
    f"{PACK}/pack.yaml",
    f"{PACK}/parity.yaml",
    f"{PACK}/semantic.yaml",
    f"{PACK}/questions.jsonl",
    f"{PACK}/probes.jsonl",
    f"{PACK}/transforms.py",
    f"{PACK}/load/clickhouse.sql",
    f"{PACK}/load/snowflake.sql",
    f"{PACK}/probes/clickhouse.sql",
    f"{PACK}/probes/snowflake.sql",
    f"{PACK}/fixes/examples.yaml",
    f"{PACK}/fixes/self_check.yaml",
    f"{PACK}/fixes/FROZEN.json",
    f"{PACKAGE}/report/example/summary.json",
    f"{PACKAGE}/report/example/records.jsonl",
    f"{PACKAGE}/report/example/questions.jsonl",
    f"{PACKAGE}/report/example/traces/*.json",
)


def _requirements(data: dict[str, Any]) -> list[str]:
    project = data.get("project") or {}
    reqs = [str(r) for r in project.get("dependencies") or []]
    for extra in (project.get("optional-dependencies") or {}).values():
        reqs += [str(r) for r in extra]
    return reqs


def pyproject_problems(path: Path) -> list[str]:
    """Why this pyproject cannot be published to PyPI as it stands (empty means fine)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = []
    for req in _requirements(data):
        name = req.split("@", 1)[0].strip()
        if "@" in req or "git+" in req:  # PEP 508 uses `@` only for a direct URL
            problems.append(f"dependency {name!r} is a direct reference; PyPI rejects it")
    hatch = ((data.get("tool") or {}).get("hatch") or {}).get("metadata") or {}
    if hatch.get("allow-direct-references") is True:
        problems.append("[tool.hatch.metadata] allow-direct-references = true; remove it")
    return problems


def _wheels(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.glob("*.whl"))


def wheel_problems(target: Path) -> list[str]:
    """Why the wheel at `target` (a .whl, or a directory holding exactly one) is incomplete."""
    wheels = _wheels(target)
    if len(wheels) != 1:
        return [f"expected exactly one wheel in {target}, found {len(wheels)}"]
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    return [
        f"{wheels[0].name} is missing {pattern}"
        for pattern in WHEEL_REQUIRED
        if not fnmatch.filter(names, pattern)
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="check", required=True)
    p = sub.add_parser("pyproject", help="refuse direct references")
    p.add_argument("path", type=Path, nargs="?", default=ROOT / "pyproject.toml")
    w = sub.add_parser("wheel", help="require the dataset pack in the built wheel")
    w.add_argument("target", type=Path, help="a .whl file or the directory uv build wrote")
    args = ap.parse_args(argv)
    if args.check == "pyproject":
        problems = pyproject_problems(args.path)
    else:
        problems = wheel_problems(args.target)
    if problems:
        print(f"release preflight ({args.check}): FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"release preflight ({args.check}): OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
