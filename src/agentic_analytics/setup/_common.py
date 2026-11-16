"""Shared pieces of `agentic-analytics setup`: errors, the secret box and tolerant JSON reading.

Every line `setup` prints goes through a `SecretBox`, which holds each secret the command has
seen (passwords and key secrets from `clickhousectl`, the read-only password it generates, the
Snowflake password and MFA code, every secret-looking environment value) and replaces it with
`[REDACTED]`. PEM private-key blocks are removed too. The JSON helpers look for a field by any of
several spellings at any depth, because the exact `clickhousectl --json` shapes are not pinned;
when a field is missing the error lists the key names it saw, never the values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

from agentic_analytics.tracing import SECRET_ENV_NAMES

REDACTED = "[REDACTED]"
MIN_SECRET_CHARS = 4
# a whole PEM private-key block, or a cut one (BEGIN with no END) up to the end of the text
_PEM_PRIVATE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)", re.DOTALL
)
# JSON field names whose string values are secrets wherever they appear (normalized: a-z0-9)
_SECRET_FIELDS = re.compile(r"password|secret|token|passcode|privatekey|^keyid$|^apikey$")
_SECRET_ENV = re.compile(r"(?i)(KEY|SECRET|PASSWORD|PASSWD|PASSCODE|TOKEN|DATABASE_URL)")
# environment names that look secret but hold paths or ids we print on purpose
_NOT_SECRET = frozenset({"SNOWFLAKE_RO_PRIVATE_KEY_PATH", "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH"})


class SetupError(Exception):
    """A configuration or runtime problem that stops `setup` (exit 2). The text is safe to show."""


class SecretBox:
    """Every secret seen so far; `scrub()` removes them from any text before it is shown."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for s in secrets:
            self.add(s)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> SecretBox:
        box = cls()
        for name, value in environ.items():
            if name in _NOT_SECRET:
                continue
            if name in SECRET_ENV_NAMES or _SECRET_ENV.search(name):
                box.add(value)
        return box

    def add(self, value: Any) -> None:
        """Remember a secret in every form it may be echoed in: raw, stripped, JSON-escaped
        (in `scrub`), URL-encoded, and SQL-escaped (backslash or doubled quote)."""
        if not isinstance(value, str) or len(value.strip()) < MIN_SECRET_CHARS:
            return
        for v in {value, value.strip()}:
            sql = v.replace("\\", "\\\\").replace("'", "\\'")
            self._values.update(
                {v, quote(v, safe=""), quote_plus(v, safe=""), sql, v.replace("'", "''")}
            )

    def add_from_json(self, obj: Any) -> None:
        """Remember every string under a secret-looking field name, at any depth."""
        for k, v, _d in walk(obj):
            if isinstance(v, str) and _SECRET_FIELDS.search(_norm(k)):
                self.add(v)

    def __contains__(self, text: object) -> bool:
        return isinstance(text, str) and any(v in text for v in self._values)

    def scrub(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, REDACTED)
            escaped = json.dumps(v)[1:-1]
            if escaped != v:
                text = text.replace(escaped, REDACTED)
        return _PEM_PRIVATE.sub("[REDACTED private key]", text)


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def parse_json(text: str, what: str) -> Any:
    """Parse `clickhousectl --json` output; a non-JSON reply is a clear SetupError."""
    body = text.strip()
    if not body:
        raise SetupError(f"`{what}` printed nothing; expected JSON")
    try:
        return json.loads(body)
    except ValueError:
        pass
    # some CLIs print a notice line before the JSON document: take the first JSON value
    for i, ch in enumerate(body):
        if ch in "{[":
            try:
                value, _ = json.JSONDecoder().raw_decode(body[i:])
            except ValueError:
                continue
            return value
    raise SetupError(f"`{what}` did not print JSON (is clickhousectl 0.5 or later installed?)")


def json_values(text: str) -> Iterator[Any]:
    """Every JSON object or array in `text`, in order (a reply may hold a notice line, several
    documents or JSON lines); text that is not JSON is skipped."""
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        if text[i] in "{[":
            try:
                value, end = decoder.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            yield value
            i = end
        else:
            i += 1


def walk(obj: Any) -> Iterator[tuple[str, Any, dict[str, Any]]]:
    """Every (key, value, owning dict) in a JSON value, shallow keys before deep ones."""
    level: list[Any] = [obj]
    for _ in range(12):
        nxt: list[Any] = []
        for item in level:
            if isinstance(item, dict):
                for k, v in item.items():
                    yield str(k), v, item
                    nxt.append(v)
            elif isinstance(item, list):
                nxt.extend(item)
        if not nxt:
            return
        level = nxt


def key_names(obj: Any, limit: int = 30) -> str:
    """The distinct key names in a JSON value (never values), for error messages."""
    names: list[str] = []
    for k, _v, _d in walk(obj):
        if k not in names:
            names.append(k)
    return ", ".join(names[:limit]) or "none"


def find_value(obj: Any, names: Iterable[str], *, kind: type = str) -> Any:
    """The shallowest non-empty value of type `kind` under any of `names` (case and `_`/`-`
    insensitive), or None."""
    wanted = {_norm(n) for n in names}
    for k, v, _d in walk(obj):
        if _norm(k) in wanted and isinstance(v, kind) and v not in ("", [], {}):
            return v
    return None


def require_value(obj: Any, names: Iterable[str], what: str, command: str) -> str:
    names = list(names)
    value = find_value(obj, names)
    if not isinstance(value, str):
        raise SetupError(
            f"could not find {what} in the output of `{command}` "
            f"(looked for {', '.join(names)}; keys seen: {key_names(obj)})"
        )
    return value


def find_records(obj: Any, required: Iterable[str], *, nested: bool = True) -> list[dict[str, Any]]:
    """Every dict (at any depth) that has all the `required` keys, in document order. With
    `nested=False`, a matching dict's own values are not searched (so the `actors` of a role,
    which may also carry an id and a name, are never taken for roles)."""
    req = [_norm(r) for r in required]
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(x: Any, depth: int) -> None:
        if depth > 12:
            return
        if isinstance(x, dict):
            keys = {_norm(str(k)) for k in x}
            if all(r in keys for r in req) and id(x) not in seen:
                seen.add(id(x))
                out.append(x)
                if not nested:
                    return
            for v in x.values():
                visit(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                visit(v, depth + 1)

    visit(obj, 0)
    return out


def field(record: Mapping[str, Any], *names: str) -> Any:
    """A record's own field under any of `names` (case and `_`/`-` insensitive), or None."""
    wanted = {_norm(n) for n in names}
    for k, v in record.items():
        if _norm(str(k)) in wanted:
            return v
    return None


__all__ = [
    "REDACTED",
    "SecretBox",
    "SetupError",
    "field",
    "find_records",
    "find_value",
    "json_values",
    "key_names",
    "parse_json",
    "require_value",
    "walk",
]
