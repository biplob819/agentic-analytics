"""Content-addressed answer cache (RUN-02, D-1, Tech Spec §2.1).

One entry holds what Ask and Execute produced for one (backend, question, fix set, trial): the
assembled answer, the execution of its final SQL, cost, tokens, Cortex messages, warehouse
time, the ask's wall time and Cortex's own latency. Scores are never cached: they are
recomputed from the entry and the stored gold, so `--rescore` needs no model or warehouse call.

- **Key.** sha256 over canonical JSON (sorted keys, no spaces, UTF-8) of every input that can
  change the answer: node name and `CODE_VERSION`, backend, model snapshot, question hash, the
  fix set and its content hash, prompt hash, semantic hash, snapshot sha256, trial, as-of date
  and the loop limits. Dict ordering never changes a key.
- **Store.** `<root>/<key[:2]>/<key>.json`, written to a temp file in the same folder, fsynced
  and renamed, so a crash leaves either the old entry or the new one, never half of one.
- **Reads** check that the stored key matches the file name and that the value validates; a
  corrupt or foreign entry is a miss (and is overwritten by the next write), never an answer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentic_analytics.schemas import SCHEMA_VERSION, AnalyticsAnswer, Execution

CODE_VERSION = "ask-v3"  # bump when Ask or Execute logic changes: every key changes with it
# ask-v3: the value holds the ask's wall time and Cortex's own latency
NODE = "ask"
_KEY = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 text; NaN and infinity refused."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeyParts:
    """Everything the cache key covers (D-1). `limits` holds the loop limits and row caps,
    `params` the model parameters given to the adapter (effort) or, for Cortex, the semantic
    view. `question_hash` covers only what the backend sees (id and text), not the gold.
    `semantic_hash` is None where the semantic model cannot change the answer (an MCP baseline).
    """

    backend: str
    model_snapshot: str
    question_hash: str
    fixes: tuple[str, ...]
    fix_hash: str
    prompt_hash: str
    semantic_hash: str | None
    snapshot_sha256: str
    trial: int
    as_of: str
    limits: Mapping[str, float | int]
    params: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": NODE,
            "code_version": CODE_VERSION,
            "backend": self.backend,
            "model_snapshot": self.model_snapshot,
            "question_hash": self.question_hash,
            "fixes": list(self.fixes),
            "fix_hash": self.fix_hash,
            "prompt_hash": self.prompt_hash,
            "semantic_hash": self.semantic_hash,
            "snapshot_sha256": self.snapshot_sha256,
            "trial": self.trial,
            "as_of": self.as_of,
            "limits": dict(self.limits),
            "params": dict(self.params),
        }


def cache_key(parts: KeyParts | Mapping[str, Any]) -> str:
    data = parts.as_dict() if isinstance(parts, KeyParts) else dict(parts)
    return sha256_json(data)


VALUE_FIELDS = frozenset(
    {
        "answer",
        "execution",
        "cost_usd",
        "tokens",
        "cortex_messages",
        "warehouse_elapsed_ms",
        "turns",
        "fix_hash",
        "ask_wall_ms",
        "analyst_latency_ms",
    }
)
TRANSIENT_ERRORS = frozenset({"transport", "timeout"})


@dataclass(frozen=True)
class AskResult:
    """What Ask and Execute produced; the cached value."""

    answer: AnalyticsAnswer
    execution: Execution | None
    cost_usd: float
    tokens: Mapping[str, int]
    cortex_messages: int
    warehouse_elapsed_ms: int
    turns: int = 0  # model turns (MCP agents); feeds the measured cost assumptions
    fix_hash: str | None = None  # the frozen fix_hash in force when the answer was made (D-52)
    ask_wall_ms: int | None = None  # the runner's wall time, question in to answer out
    analyst_latency_ms: float | None = None  # Cortex's own time (A2.2), Cortex only

    @property
    def transient(self) -> bool:
        """A transport failure or timeout (answer or execution): never cached, so a resume
        retries it."""
        ex = self.execution
        return self.answer.error in TRANSIENT_ERRORS or (
            ex is not None and ex.error in TRANSIENT_ERRORS
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "answer": self.answer.model_dump(mode="json"),
            "execution": None if self.execution is None else self.execution.model_dump(mode="json"),
            "cost_usd": self.cost_usd,
            "tokens": dict(sorted(self.tokens.items())),
            "cortex_messages": self.cortex_messages,
            "warehouse_elapsed_ms": self.warehouse_elapsed_ms,
            "turns": self.turns,
            "fix_hash": self.fix_hash,
            "ask_wall_ms": self.ask_wall_ms,
            "analyst_latency_ms": self.analyst_latency_ms,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> AskResult:
        if set(data) != VALUE_FIELDS:
            raise ValueError("cache value has unexpected fields")
        stamp = data["fix_hash"]
        if stamp is not None and not _KEY.match(str(stamp)):
            raise ValueError("cache fix_hash must be a sha256 or null")
        cost = data["cost_usd"]
        if not isinstance(cost, int | float) or isinstance(cost, bool) or cost < 0:
            raise ValueError("cache cost_usd must be a non-negative number")
        tokens = data["tokens"]
        if not isinstance(tokens, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in tokens.values()
        ):
            raise ValueError("cache tokens must map names to non-negative ints")
        for name in ("cortex_messages", "warehouse_elapsed_ms", "turns"):
            v = data[name]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"cache {name} must be a non-negative int")
        wall = data["ask_wall_ms"]
        if wall is not None and (not isinstance(wall, int) or isinstance(wall, bool) or wall < 0):
            raise ValueError("cache ask_wall_ms must be a non-negative int or null")
        took = data["analyst_latency_ms"]
        if took is not None and (
            not isinstance(took, int | float)
            or isinstance(took, bool)
            or not math.isfinite(took)
            or took < 0
        ):
            raise ValueError("cache analyst_latency_ms must be a finite, non-negative number")
        ex = data["execution"]
        return cls(
            answer=AnalyticsAnswer.model_validate(data["answer"]),
            execution=None if ex is None else Execution.model_validate(ex),
            cost_usd=float(cost),
            tokens=dict(tokens),
            cortex_messages=data["cortex_messages"],
            warehouse_elapsed_ms=data["warehouse_elapsed_ms"],
            turns=data["turns"],
            fix_hash=stamp,
            ask_wall_ms=wall,
            analyst_latency_ms=None if took is None else float(took),
        )


class AnswerCache:
    """`<root>/<key[:2]>/<key>.json`; atomic writes; corrupt entries read as misses."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError(f"not a cache key: {key!r}")
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> AskResult | None:
        p = self.path(key)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        try:
            return AskResult.from_json(data["value"])
        except (KeyError, TypeError, ValueError, ValidationError):
            return None

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def put(self, key: str, value: AskResult, parts: KeyParts | None = None) -> Path:
        if value.transient:
            raise ValueError("transport and timeout failures are never cached (resume retries)")
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "parts": parts.as_dict() if parts is not None else None,
            "value": value.to_json(),
        }
        text = json.dumps(entry, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{key[:8]}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return p


__all__ = [
    "CODE_VERSION",
    "TRANSIENT_ERRORS",
    "AnswerCache",
    "AskResult",
    "KeyParts",
    "cache_key",
    "canonical_json",
    "sha256_json",
    "sha256_text",
]
