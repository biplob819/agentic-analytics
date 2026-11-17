"""Fakes for the visitor-own-keys tests (OWNER D-56). No network, no real key, no spend.

The planted keys are made-up strings in each provider's form (split so the repo's secret scan
does not flag them); nothing here can reach a provider:
`FakeClient` stands in for the SDK client and raises a real SDK error class (built offline on an
`httpx.Response`) whose message quotes the whole key, as some provider errors quote part of one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx2 as httpx
import openai

from agentic_analytics.demo import ask as A
from agentic_analytics.demo import keys as K
from tests.demo.helpers import SlowModel, SpyStore

VISITOR = "open-sesame"
OWNER = "owner-only-7f3c"
ANTHROPIC_KEY = "sk-ant-" + "api03-PLANTEDvisitorKEY0123456789abcdefghijKLMNOPQRSTuvwxyz-_AA"
OPENAI_KEY = "sk-" + "proj-PLANTEDopenaiVISITORkey0123456789abcdefghijKLMNOPQRSTuvwxyz_BB"
OWNER_ANTHROPIC_KEY = "sk-ant-" + "api03-OWNERdeploymentKEY0123456789abcdefghijKLMNOPQRSTuvwxyzCC"
ENV = {"DEMO_PASSCODE": VISITOR, "DEMO_OWNER_PASSCODE": OWNER}

_REQ = httpx.Request("POST", "https://api.invalid/v1/messages")


def sdk_error(cls: type[Exception], status: int, message: str, body: Any = None) -> Exception:
    """A real SDK status error, built offline."""
    return cls(message, response=httpx.Response(status, request=_REQ), body=body)  # type: ignore[call-arg]


def anthropic_errors(key: str = ANTHROPIC_KEY) -> dict[str, Exception]:
    return {
        "rejected": sdk_error(anthropic.AuthenticationError, 401, f"invalid x-api-key {key}"),
        "rate_limited": sdk_error(anthropic.RateLimitError, 429, f"rate limit for key {key}"),
        "model": sdk_error(anthropic.NotFoundError, 404, f"model not found for {key}"),
        "failed": RuntimeError(f"socket said {key}"),
    }


def openai_errors(key: str = OPENAI_KEY) -> dict[str, Exception]:
    quota = {"code": "insufficient_quota", "message": f"quota exceeded for {key}"}
    return {
        "rejected": sdk_error(openai.AuthenticationError, 401, f"Incorrect API key {key}"),
        "quota": sdk_error(openai.RateLimitError, 429, f"quota for {key}", body=quota),
        "rate_limited": sdk_error(openai.RateLimitError, 429, f"slow down {key}"),
        "failed": ValueError(f"odd reply quoting {key}"),
    }


@dataclass
class _Endpoint:
    error: Exception
    calls: int = 0

    async def create(self, **params: Any) -> Any:
        self.calls += 1
        raise self.error


@dataclass
class FakeClient:
    """An SDK client whose every call raises `error` (Messages and Responses alike)."""

    error: Exception
    messages: _Endpoint = field(init=False)
    responses: _Endpoint = field(init=False)

    def __post_init__(self) -> None:
        self.messages = _Endpoint(self.error)
        self.responses = self.messages

    @property
    def calls(self) -> int:
        return self.messages.calls


@dataclass
class Factory:
    """An `agent_factory` that records what it was asked to build and returns fakes."""

    model_cost: float = 0.001
    built: list[tuple[str, Any, str]] = field(default_factory=list)  # model, effort, provider
    agents: list[SlowModel] = field(default_factory=list)

    def __call__(self, model: str, effort: Any, prices: Any, max_tokens: int, key: Any) -> Any:
        assert isinstance(key, K.VisitorKey) and key.reveal()
        self.built.append((model, effort, key.provider))
        provider = "anthropic" if model.startswith("claude-") else "openai"
        agent = SlowModel(provider=provider, model=model)
        self.agents.append(agent)
        return agent


def failing_factory(clients: list[FakeClient], error: Exception) -> Any:
    """The real `visitor_agent` adapters on a `FakeClient` raising `error`."""

    def build(model: str, effort: Any, prices: Any, max_tokens: int, key: Any) -> Any:
        client = FakeClient(error)
        clients.append(client)
        return K.visitor_agent(model, effort, prices, max_tokens, key, client=client)

    return build


def keys(anthropic_key: str | None = ANTHROPIC_KEY, openai_key: str | None = None) -> K.VisitorKeys:
    return K.accept_keys({"anthropic": anthropic_key or "", "openai": openai_key or ""})


@dataclass
class Stores:
    visitor: SpyStore
    owner: SpyStore
    byok: SpyStore


def stores(where: Path) -> Stores:
    return Stores(
        SpyStore(where / "visitor.sqlite"),
        SpyStore(where / "owner.sqlite"),
        SpyStore(where / "byok.sqlite"),
    )


def guards(s: Stores) -> tuple[Any, Any, Any]:
    owner = A.make_owner_guard(ENV, store=s.owner)
    return A.make_guard(ENV, store=s.visitor), owner, A.make_byok_guard(ENV, store=s.byok)
