"""Fix content frozen and hashed before the first baseline (FIX-05, OWNER D-13, D-48, C5, gate 8).

`fix_hash(pack)` (also `current_fix_hash`) is the sha256 of canonical JSON over everything a fix
puts in front of a model or uses to judge it::

    {"files":  {"fixes/examples.yaml": "<text>", "fixes/self_check.yaml": "<text>"},
     "blocks": fix_blocks(pack),          # the RENDERED prompt texts: descriptions block,
                                          # examples header and body, self-check rules text
     "rules":  load_rules(pack).model_dump(mode="json")}   # the self-check rules as loaded

so a changed example, rule, threshold, message or semantic description changes it, and so does
a change in the code that renders a block (a new header sentence in `examples_block`), even
when no source file changed.

`freeze_fixes(pack, reason=...)` writes `fixes/FROZEN.json` (schema 1.0)::

    {"schema_version": "1.0", "kind": "fix_freeze", "pack": "uk_price_paid@1.0",
     "fix_hash": "<64 hex>", "files": {"fixes/examples.yaml": "<sha256>", ...},
     "blocks": {"descriptions": "<sha256>", "examples": "<sha256>", "self_check": "<sha256>"},
     "rules_sha256": "<64 hex>", "frozen_at": "<ISO 8601>", "reason": "<why; a decision id>"}

It refuses when the record already exists unless `force=True`, and a forced re-freeze needs a
`reason` (D-13/D-48: a re-freeze is a logged decision). Helpers for the runner and gate 8:
`current_fix_hash(pack)`, `frozen_fix_hash(pack)` (None when not frozen), `frozen_problems(pack)`
(what changed; empty when the content matches) and `fix_hash_matches_frozen(pack)`.

`fix_blocks(pack)` is what the runner takes as `RunOptions.fix_blocks`: the descriptions and
examples prompt texts, and the self-check rules file's text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from agentic_analytics.fixes import examples as examples_mod
from agentic_analytics.fixes.examples import EXAMPLES_FILE, FixError, load_examples
from agentic_analytics.fixes.self_check import SELF_CHECK_FILE, load_rules
from agentic_analytics.packs import Pack, PackError, load_pack, pack_file, read_pack_text
from agentic_analytics.schemas._base import NonBlank, Sha256, Timestamp, Versioned
from agentic_analytics.semantic import SemanticError, descriptions_block, load_semantic

FIX_FILES: tuple[str, ...] = (EXAMPLES_FILE, SELF_CHECK_FILE)
FROZEN_FILE = "fixes/FROZEN.json"
INITIAL_REASON = "initial freeze before the first baseline run (OWNER D-13)"


class FrozenRecord(Versioned):
    kind: Literal["fix_freeze"]
    pack: NonBlank
    fix_hash: Sha256
    files: dict[str, Sha256]
    blocks: dict[str, Sha256]
    rules_sha256: Sha256
    frozen_at: Timestamp
    reason: NonBlank


def sha256_json(value: object) -> str:
    """sha256 of canonical JSON (sorted keys, no spaces, UTF-8)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha(text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(pack: Pack, relpath: str) -> str:
    try:
        return read_pack_text(pack, relpath)
    except (PackError, OSError, UnicodeDecodeError) as exc:
        raise FixError(f"{pack.ref}: cannot read {relpath} ({exc})") from exc


def descriptions_text(pack: Pack) -> str:
    """FIX-01: the descriptions fix is N1.4's SEM-03 block for the pack's semantic model."""
    try:
        return descriptions_block(load_semantic(pack))
    except SemanticError as exc:
        raise FixError(str(exc)) from exc


def fix_blocks(pack: Pack) -> dict[str, str]:
    """Fix name -> the exact text the fix uses (validated: the fix files must parse).

    `descriptions` and `examples` are prompt blocks. `self_check` is the rules file's text, kept
    here so the hash covers it; it is never a prompt block (the runner passes only the first two
    to the system prompt, and the self-check works through feedback turns alone)."""
    load_rules(pack)
    return {
        "descriptions": descriptions_text(pack),
        "examples": examples_mod.examples_block(load_examples(pack)),
        "self_check": _text(pack, SELF_CHECK_FILE),
    }


def fix_contents(pack: Pack) -> dict[str, object]:
    return {
        "files": {rel: _text(pack, rel) for rel in FIX_FILES},
        "blocks": fix_blocks(pack),
        "rules": load_rules(pack).model_dump(mode="json"),
    }


def fix_hash(pack: Pack) -> str:
    return sha256_json(fix_contents(pack))


def current_fix_hash(pack: Pack) -> str:
    """The fix_hash of the pack's fix content as it is now (for the runner's checks)."""
    return fix_hash(pack)


def _record(pack: Pack, now: datetime, reason: str) -> FrozenRecord:
    contents = fix_contents(pack)
    files: dict[str, str] = contents["files"]  # type: ignore[assignment]
    blocks: dict[str, str] = contents["blocks"]  # type: ignore[assignment]
    return FrozenRecord(
        schema_version="1.0",
        kind="fix_freeze",
        pack=pack.ref,
        fix_hash=sha256_json(contents),
        files={rel: _sha(text) for rel, text in files.items()},
        blocks={name: _sha(text) for name, text in blocks.items()},
        rules_sha256=sha256_json(contents["rules"]),
        frozen_at=now,
        reason=reason,
    )


def freeze_fixes(
    pack: Pack,
    *,
    force: bool = False,
    reason: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Write `fixes/FROZEN.json` for the pack's current fix content; refuse to overwrite unless
    forced, and refuse a forced re-freeze without a reason."""
    if not isinstance(pack.root, Path):
        raise FixError(f"{pack.ref} is not a directory on disk; copy it and use --pack PATH")
    load_examples(pack)
    load_rules(pack)
    path = pack.root / FROZEN_FILE
    if path.exists() and not force:
        raise FixError(
            f"{path} exists: the fixes are already frozen (re-freezing needs a logged decision; "
            "pass force=True with a reason / --force --reason)"
        )
    if path.exists() and not (reason or "").strip():
        raise FixError("a forced re-freeze needs a reason (a decision id, D-13/D-48)")
    record = _record(pack, now(), (reason or "").strip() or INITIAL_REASON)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def load_frozen(pack: Pack) -> FrozenRecord | None:
    try:
        f = pack_file(pack, FROZEN_FILE)
    except PackError:
        return None
    try:
        return FrozenRecord.model_validate_json(f.read_text(encoding="utf-8"))
    except (ValidationError, OSError, UnicodeDecodeError) as exc:
        raise FixError(f"{pack.ref} {FROZEN_FILE}: {exc}") from exc


def frozen_fix_hash(pack: Pack) -> str | None:
    """The fix_hash in the pack's frozen record, or None when the fixes are not frozen."""
    record = load_frozen(pack)
    return None if record is None else record.fix_hash


def frozen_problems(pack: Pack) -> list[str]:
    """Why the current fix content does not match the frozen record; empty when it does."""
    record = load_frozen(pack)
    if record is None:
        return [f"{pack.ref} has no {FROZEN_FILE}: freeze the fixes before the baseline"]
    problems: list[str] = []
    if record.pack != pack.ref:
        problems.append(f"{FROZEN_FILE} is for {record.pack}, not {pack.ref}")
    contents = fix_contents(pack)
    files: dict[str, str] = contents["files"]  # type: ignore[assignment]
    blocks: dict[str, str] = contents["blocks"]  # type: ignore[assignment]
    for rel in sorted(set(files) | set(record.files)):
        if rel not in record.files:
            problems.append(f"{rel} is not in the frozen record")
        elif rel not in files:
            problems.append(f"{rel} is frozen but no longer a fix file")
        elif _sha(files[rel]) != record.files[rel]:
            problems.append(f"{rel} changed after the freeze")
    for name in sorted(set(blocks) | set(record.blocks)):
        if name not in blocks or name not in record.blocks:
            problems.append(f"the {name} block is new or gone since the freeze")
        elif _sha(blocks[name]) != record.blocks[name]:
            problems.append(f"the rendered {name} block changed after the freeze")
    if sha256_json(contents["rules"]) != record.rules_sha256:
        problems.append("the self-check rules as loaded changed after the freeze")
    current = sha256_json(contents)
    if current != record.fix_hash and not problems:
        problems.append(f"fix_hash {current[:12]} != frozen {record.fix_hash[:12]}")
    return problems


def fix_hash_matches_frozen(pack: Pack) -> bool:
    """Gate 8 (`pytest -m gold`): the fix content is exactly what was frozen."""
    return not frozen_problems(pack)


def main(argv: list[str] | None = None) -> int:
    """`python -m agentic_analytics.fixes [--pack P] [--force --reason R] [--check]`."""
    ap = argparse.ArgumentParser(prog="python -m agentic_analytics.fixes")
    ap.add_argument("--pack", default="uk_price_paid", help="bundled pack name or directory")
    ap.add_argument("--force", action="store_true", help="overwrite an existing record")
    ap.add_argument("--reason", default=None, help="why (a decision id); required with --force")
    ap.add_argument("--check", action="store_true", help="only compare with the frozen record")
    ns = ap.parse_args(argv)
    if ns.force and not (ns.reason or "").strip():
        print("--force needs --reason (a decision id, D-13/D-48)", file=sys.stderr)
        return 2
    try:
        pack = load_pack(ns.pack)
        if ns.check:
            problems = frozen_problems(pack)
            for p in problems:
                print(p, file=sys.stderr)
            if not problems:
                print(f"{pack.ref}: fix_hash {fix_hash(pack)[:12]} matches the frozen record")
            return 1 if problems else 0
        path = freeze_fixes(pack, force=ns.force, reason=ns.reason)
    except (PackError, FixError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {path} (fix_hash {fix_hash(pack)})")
    return 0


__all__ = [
    "FIX_FILES",
    "FROZEN_FILE",
    "INITIAL_REASON",
    "FrozenRecord",
    "current_fix_hash",
    "descriptions_text",
    "fix_blocks",
    "fix_contents",
    "fix_hash",
    "fix_hash_matches_frozen",
    "freeze_fixes",
    "frozen_fix_hash",
    "frozen_problems",
    "load_frozen",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
