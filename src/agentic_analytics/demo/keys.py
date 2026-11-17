"""A demo visitor's own API keys (OWNER D-56; DEM-06, DEM-08).

After the visitor passcode, a visitor may give their own Anthropic key (the Claude panel) and/or
OpenAI key (the GPT panel). This module holds everything that touches the key itself:

- **Offline format checks** (`check_key`): the provider's prefix (`sk-ant-` for Anthropic, `sk-`
  but not `sk-ant-` for OpenAI), 40 to 256 characters, only the characters keys use (letters,
  digits, `-`, `_`): no whitespace or control character inside. Surrounding spaces and line
  breaks from a paste are trimmed. The deployment's own key is refused as a visitor key (it would
  bypass the owner's budget). No network call is made and no message quotes any part of the key.
- **Session memory only** (`VisitorKey`, `VisitorKeys`): the value is kept in one private slot
  and handed out only by `reveal()`. `repr`/`str`/f-strings show the provider alone, pickling is
  refused (so it cannot be written to a session store, cache or file by accident), and
  `forget()` drops it. The page keeps one `VisitorKeys` in that browser session's state.
- **Per-request clients** (`sdk_client`, `visitor_agent`): a fresh SDK client per ask, built with
  `api_key=` and `max_retries=0` (the adapters' `with_retries` owns retries; a visitor key gets one
  quick retry at most), never through `os.environ`. The model and effort come from the
  deployment's run configuration: a visitor cannot pick another model.
- **Secret-free failures** (`KeyedModel`, `VisitorKeyFailed`): any error from a call on the
  visitor's key is replaced by a `VisitorKeyFailed` with a fixed message per kind (rejected, out
  of quota, rate-limited, model not available, other) and no chained cause or context, so a
  provider message that quotes part of the key never reaches a panel, a log or a traceback. The
  panel fails; nothing falls back to the owner's key.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Any, Literal

Provider = Literal["anthropic", "openai"]
FailureKind = Literal["rejected", "quota", "rate_limited", "model", "failed"]

PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai")
PROVIDER_NAMES: dict[str, str] = {"anthropic": "Anthropic", "openai": "OpenAI"}
PANEL_NAMES: dict[str, str] = {"anthropic": "Claude panel", "openai": "GPT panel"}
PREFIXES: dict[str, str] = {"anthropic": "sk-ant-", "openai": "sk-"}
OWNER_KEY_VARS: dict[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
REDACT_NAMES: dict[str, str] = {
    "anthropic": "VISITOR_ANTHROPIC_KEY",
    "openai": "VISITOR_OPENAI_KEY",
}
KEY_MIN_CHARS = 40
KEY_MAX_CHARS = 256
_KEY_CHARS = re.compile(r"[A-Za-z0-9_-]+")
_PASTE_EDGES = " \t\r\n"
VISITOR_RETRY: dict[str, Any] = {"attempts": 2, "cap": 1.0}  # one quick retry, then the panel fails
NOT_USED = "The demo's own key was not used."


class KeyFormatError(ValueError):
    """A typed key that is not in its provider's form. `reason` names no part of the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def provider_for_model(model: str) -> Provider | None:
    """The provider whose key pays for `model`, as `demo_agent` picks the adapter."""
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gpt-"):
        return "openai"
    return None


class VisitorKey:
    """One visitor key in memory. Shows only its provider; cannot be pickled."""

    __slots__ = ("_provider", "_secret")

    def __init__(self, provider: Provider, secret: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        self._provider: Provider = provider
        self._secret: str = secret

    @property
    def provider(self) -> Provider:
        return self._provider

    def reveal(self) -> str:
        """The key itself: only for building that request's SDK client and the redactor."""
        if not self._secret:
            raise ValueError("this key was forgotten")
        return self._secret

    def forget(self) -> None:
        self._secret = ""

    @property
    def forgotten(self) -> bool:
        return not self._secret

    def __repr__(self) -> str:
        state = "forgotten" if self.forgotten else "set"
        return f"VisitorKey({self._provider}, {state})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a visitor key cannot be pickled: it lives in session memory only")

    def __copy__(self) -> VisitorKey:
        return self

    def __deepcopy__(self, memo: Any) -> VisitorKey:
        return self


class VisitorKeys:
    """The keys one browser session gave, at most one per provider."""

    __slots__ = ("_keys",)

    def __init__(self, keys: Mapping[str, VisitorKey] | None = None) -> None:
        self._keys: dict[str, VisitorKey] = {}
        for provider, key in (keys or {}).items():
            if key.provider != provider:
                raise ValueError("a key is filed under the wrong provider")
            self._keys[provider] = key

    def get(self, provider: str | None) -> VisitorKey | None:
        key = self._keys.get(provider or "")
        return None if key is None or key.forgotten else key

    def providers(self) -> tuple[Provider, ...]:
        return tuple(p for p in PROVIDERS if self.get(p) is not None)

    def merged(self, newer: VisitorKeys) -> VisitorKeys:
        """These keys with `newer`'s replacing any for the same provider."""
        out = {p: k for p, k in self._keys.items() if not k.forgotten}
        out.update({p: k for p, k in newer._keys.items() if not k.forgotten})
        return VisitorKeys(out)

    def secrets(self) -> dict[str, str]:
        """{redaction label: key} for `tracing.Redactor.with_secrets`."""
        return {REDACT_NAMES[p]: k.reveal() for p in self.providers() if (k := self.get(p))}

    def forget(self) -> None:
        for key in self._keys.values():
            key.forget()
        self._keys.clear()

    def __bool__(self) -> bool:
        return bool(self.providers())

    def __repr__(self) -> str:
        shown = ", ".join(f"{p}={'set' if self.get(p) else 'unset'}" for p in PROVIDERS)
        return f"VisitorKeys({shown})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("visitor keys cannot be pickled: they live in session memory only")


def check_key(
    provider: Provider, raw: str, *, owner_keys: Mapping[str, str] | None = None
) -> VisitorKey:
    """The typed key as a `VisitorKey`, or `KeyFormatError`. Offline: no client, no call."""
    name = PROVIDER_NAMES[provider]
    value = (raw or "").strip(_PASTE_EDGES)
    if not value:
        raise KeyFormatError(f"Paste your {name} API key.")
    if not _KEY_CHARS.fullmatch(value):
        raise KeyFormatError(
            f"The {name} key contains a space, a control character or a character API keys "
            "never have."
        )
    if provider == "openai" and value.startswith(PREFIXES["anthropic"]):
        raise KeyFormatError("That is an Anthropic key; put it in the Anthropic field.")
    if not value.startswith(PREFIXES[provider]):
        raise KeyFormatError(f"An {name} API key starts with {PREFIXES[provider]}.")
    if not KEY_MIN_CHARS <= len(value) <= KEY_MAX_CHARS:
        raise KeyFormatError(
            f"The {name} key should be {KEY_MIN_CHARS} to {KEY_MAX_CHARS} characters long."
        )
    own = ((owner_keys or {}).get(OWNER_KEY_VARS[provider]) or "").strip()
    if own and hmac.compare_digest(own.encode(), value.encode()):
        raise KeyFormatError(f"That {name} key cannot be used as a visitor key here.")
    return VisitorKey(provider, value)


def accept_keys(
    typed: Mapping[str, str], *, owner_keys: Mapping[str, str] | None = None
) -> VisitorKeys:
    """Every non-blank typed key checked (`KeyFormatError` on the first bad one)."""
    keys: dict[str, VisitorKey] = {}
    for provider in PROVIDERS:
        raw = typed.get(provider) or ""
        if raw.strip():
            keys[provider] = check_key(provider, raw, owner_keys=owner_keys)
    return VisitorKeys(keys)


# -- clients and the model wrapper -------------------------------------------------------------


def sdk_client(key: VisitorKey) -> Any:
    """A new async SDK client on the visitor's key for one request: `api_key=` (never the
    environment) and `max_retries=0`. Constructing it makes no network call."""
    if key.provider == "anthropic":
        import anthropic

        return anthropic.AsyncAnthropic(api_key=key.reveal(), max_retries=0)
    import openai

    return openai.AsyncOpenAI(api_key=key.reveal(), max_retries=0)


def visitor_agent(
    model: str,
    effort: Any,
    prices: Mapping[str, Any],
    max_tokens: int,
    key: VisitorKey,
    *,
    client: Any = None,
) -> Any:
    """The benchmark's adapter for `model` (fixed by the deployment) on the visitor's key."""
    from agentic_analytics.models import AnthropicAgent, OpenAIAgent

    provider = provider_for_model(model)
    if provider is None or provider != key.provider:
        raise ValueError(f"a {PROVIDER_NAMES[key.provider]} key cannot pay for this model")
    client = sdk_client(key) if client is None else client
    cls = AnthropicAgent if provider == "anthropic" else OpenAIAgent
    return cls(model, effort, dict(prices), client=client, max_tokens=max_tokens, **VISITOR_RETRY)


def _status(exc: BaseException) -> int | None:
    try:
        status = getattr(exc, "status_code", None)
    except Exception:
        return None
    return status if isinstance(status, int) else None


def classify_failure(exc: BaseException) -> FailureKind:
    """What went wrong with a call on a visitor key, from the error's type and status only."""
    from agentic_analytics.models.errors import QuotaExhausted, _is_quota

    names = {cls.__name__ for cls in type(exc).__mro__}
    status = _status(exc)
    if isinstance(exc, QuotaExhausted) or status == 402 or _is_quota(exc):
        return "quota"
    if status in (401, 403) or names & {"AuthenticationError", "PermissionDeniedError"}:
        return "rejected"
    if status == 429 or "RateLimitError" in names:
        return "rate_limited"
    if status == 404 or "NotFoundError" in names:
        return "model"
    return "failed"


def failure_text(provider: str, kind: FailureKind) -> str:
    name = PROVIDER_NAMES.get(provider, provider)
    what = {
        "rejected": f"{name} rejected your API key. Check it, or forget it to use the demo's "
        "budget.",
        "quota": f"Your {name} account is out of credit or quota.",
        "rate_limited": f"Your {name} key hit its rate limit. Wait a minute and ask again.",
        "model": f"Your {name} key cannot use the benchmark's model.",
        "failed": f"The call on your {name} key failed.",
    }[kind]
    return f"{what} {NOT_USED}"


FAILURE_TEXTS = frozenset(
    failure_text(p, k)
    for p in PROVIDERS
    for k in ("rejected", "quota", "rate_limited", "model", "failed")
)


class VisitorKeyFailed(Exception):
    """A call on a visitor's key failed. The message is fixed per kind (never the provider's);
    it is not a transient error, so the agent loop stops the panel instead of retrying."""

    def __init__(self, provider: str, kind: FailureKind) -> None:
        self.provider = provider
        self.kind = kind
        self.reason = failure_text(provider, kind)
        super().__init__(self.reason)


class KeyedModel:
    """An `AgentModel` on a visitor's key that turns every provider error into a secret-free
    `VisitorKeyFailed`, raised outside the handler so no cause or context is chained."""

    def __init__(self, inner: Any, provider: Provider) -> None:
        self.inner = inner
        self.key_provider: Provider = provider
        self.provider = inner.provider
        self.model = inner.model
        self.effort = inner.effort
        self.max_tokens = getattr(inner, "max_tokens", None)

    def start(self, system: str, user: str, tools: Any) -> Any:
        return self.inner.start(system, user, tools)

    async def step(self, conv: Any) -> Any:
        kind: FailureKind | None = None
        try:
            return await self.inner.step(conv)
        except Exception as exc:
            kind = classify_failure(exc)
        assert kind is not None
        raise VisitorKeyFailed(self.key_provider, kind)

    def add_tool_results(self, conv: Any, results: Any) -> None:
        self.inner.add_tool_results(conv, results)

    def add_user(self, conv: Any, text: str) -> None:
        self.inner.add_user(conv, text)

    def __repr__(self) -> str:
        return f"KeyedModel({self.key_provider}, {self.model})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a model on a visitor key cannot be pickled")


__all__ = [
    "FAILURE_TEXTS",
    "KEY_MAX_CHARS",
    "KEY_MIN_CHARS",
    "NOT_USED",
    "PANEL_NAMES",
    "PREFIXES",
    "PROVIDERS",
    "PROVIDER_NAMES",
    "REDACT_NAMES",
    "VISITOR_RETRY",
    "KeyFormatError",
    "KeyedModel",
    "Provider",
    "VisitorKey",
    "VisitorKeyFailed",
    "VisitorKeys",
    "accept_keys",
    "check_key",
    "classify_failure",
    "failure_text",
    "provider_for_model",
    "sdk_client",
    "visitor_agent",
]
