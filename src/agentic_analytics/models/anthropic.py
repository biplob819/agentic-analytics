"""Claude through the Messages API with native tool use (MOD-01).

- Effort is set explicitly (EvalKit D-24); no sampling parameters, which current Claude models
  reject (EvalKit D-22); no server-side refusal fallbacks (D-31): a refusal is recorded.
- The tool list and system prompt are stable across a conversation and automatic caching
  moves the breakpoint forward as the history grows, so later turns read the prefix from cache.
"""

from __future__ import annotations

import time
from typing import Any

from evalkit.cost import Price, call_cost, price_for
from evalkit.retry import with_retries

from agentic_analytics.models.base import (
    Conversation,
    Effort,
    ModelTurn,
    Stop,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from agentic_analytics.models.errors import quota_guard

STOP_MAP: dict[str, Stop] = {
    "tool_use": "tool_use",
    "end_turn": "end",
    "stop_sequence": "end",
    "refusal": "refusal",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
}
# Anything else (e.g. `pause_turn`, which only server tools produce) is treated as a cut-off
# turn: its tool calls are never run.
FORBIDDEN_PARAMS = frozenset({"temperature", "top_p", "top_k"})


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters,
    }
    if spec.strict:
        out["strict"] = True
    return out


class AnthropicAgent:
    provider = "anthropic"
    model: str
    effort: Effort
    max_tokens: int

    def __init__(
        self,
        model: str,
        effort: Effort,
        prices: dict[str, Price],
        *,
        client: Any = None,
        max_tokens: int = 16000,
        **retry: Any,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._price = price_for(prices, model)  # unknown models fail now, not mid-run
        self._client = client
        self._retry = retry

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(max_retries=0)  # with_retries owns retries
        return self._client

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Conversation:
        return Conversation(system=system, tools=tools, history=[{"role": "user", "content": user}])

    def request(self, conv: Conversation) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": conv.system,
            "messages": list(conv.history),
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self.effort},
        }
        if conv.tools:  # no empty tools list
            params["tools"] = [tool_param(t) for t in conv.tools]
        assert not FORBIDDEN_PARAMS & params.keys()
        return params

    async def step(self, conv: Conversation) -> ModelTurn:
        params = self.request(conv)
        t0 = time.perf_counter()
        msg = await with_retries(
            lambda: quota_guard(self.client.messages.create(**params)), **self._retry
        )
        latency = int((time.perf_counter() - t0) * 1000)
        conv.history.append({"role": "assistant", "content": [_block(b) for b in msg.content]})
        return self.parse(msg, latency)

    def parse(self, msg: Any, latency_ms: int) -> ModelTurn:
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        calls = tuple(
            ToolCall(
                id=b.id,
                name=b.name,
                arguments=dict(b.input) if isinstance(b.input, dict) else None,
                parse_error=None if isinstance(b.input, dict) else "not an object",
            )
            for b in msg.content
            if getattr(b, "type", "") == "tool_use"
        )
        u = msg.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        details = getattr(u, "output_tokens_details", None)
        thinking = (getattr(details, "thinking_tokens", 0) or 0) if details else 0
        thinking = min(thinking, u.output_tokens)
        usage = Usage(
            tokens_in=u.input_tokens + cache_read + cache_write,
            tokens_out=max(u.output_tokens - thinking, 0),
            tokens_reasoning=thinking,
            tokens_cached=cache_read,
            tokens_cache_write=cache_write,
        )
        stop_reason = getattr(msg, "stop_reason", None) or "end_turn"
        stop = STOP_MAP.get(stop_reason, "max_tokens")
        if calls and stop == "end":
            stop = "tool_use"
        details_obj = getattr(msg, "stop_details", None)
        return ModelTurn(
            text=text,
            tool_calls=calls,
            stop=stop,
            usage=usage,
            cost_usd=call_cost(
                self._price,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                tokens_reasoning=usage.tokens_reasoning,
                tokens_cached=usage.tokens_cached,
                tokens_cache_write=usage.tokens_cache_write,
            ),
            latency_ms=latency_ms,
            model_snapshot=getattr(msg, "model", "") or self.model,
            request_id=getattr(msg, "_request_id", None) or getattr(msg, "id", None),
            refusal_category=getattr(details_obj, "category", None) if details_obj else None,
        )

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None:
        conv.history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )

    def add_user(self, conv: Conversation, text: str) -> None:
        last = conv.history[-1] if conv.history else None
        if last and last["role"] == "user" and isinstance(last["content"], list):
            last["content"].append({"type": "text", "text": text})
        else:
            conv.history.append({"role": "user", "content": text})


def _block(b: Any) -> dict[str, Any]:
    """An SDK content block as a plain dict to send back unchanged (thinking blocks included)."""
    if hasattr(b, "model_dump"):
        # exclude_unset echoes exactly the fields the API sent, explicit nulls included
        return b.model_dump(mode="json", exclude_unset=True)
    return dict(b)
