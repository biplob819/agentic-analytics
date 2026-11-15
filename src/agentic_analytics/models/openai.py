"""GPT through the Responses API with function tools (MOD-02, D-54).

Chat Completions refuses function tools together with `reasoning_effort` for GPT-6.1 Sol, so the
adapter uses `client.responses.create` with `reasoning={"effort": ...}`. Calls are stateless:
`store=False`, no `previous_response_id`, and `include=["reasoning.encrypted_content"]` so each
response's output items (encrypted reasoning items included) go back verbatim in the next
request's `input`. Reasoning tokens are reported separately and billed as output. No sampling
parameters (EvalKit D-22).
"""

from __future__ import annotations

import json
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

INCLUDE = ("reasoning.encrypted_content",)
FORBIDDEN_PARAMS = frozenset({"temperature", "top_p", "top_k", "previous_response_id"})


class ResponseFailed(Exception):
    """A 200 reply whose status is `failed` (a server-side error): retried, then `transport`."""

    status_code = 503

    def __init__(self, code: str) -> None:
        super().__init__(f"OpenAI response failed ({code})")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    """A Responses function tool (flat). `strict` is always explicit: the API defaults it on."""
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
        "strict": spec.strict,
    }


def _item(item: Any) -> dict[str, Any]:
    """An output item as a plain dict to send back unchanged (encrypted reasoning included)."""
    if hasattr(item, "model_dump"):
        # exclude_unset echoes exactly the fields the API sent; by_alias keeps wire names
        return item.model_dump(mode="json", exclude_unset=True, by_alias=True)
    return dict(item)


def _parse_args(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return None, f"arguments are not JSON: {exc.msg}"
    except (RecursionError, ValueError):
        return None, "arguments are not parseable JSON"
    if not isinstance(args, dict):
        return None, "arguments are not a JSON object"
    return args, None


class OpenAIAgent:
    provider = "openai"
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
        self._price = price_for(prices, model)
        self._client = client
        self._retry = retry

    @property
    def client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(max_retries=0)
        return self._client

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Conversation:
        return Conversation(system=system, tools=tools, history=[{"role": "user", "content": user}])

    def request(self, conv: Conversation) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "instructions": conv.system,
            "input": list(conv.history),
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.effort},
            "store": False,
            "include": list(INCLUDE),
        }
        if conv.tools:  # an empty tools list is not sent (07f69ac)
            params["tools"] = [tool_param(t) for t in conv.tools]
        assert not FORBIDDEN_PARAMS & params.keys()
        return params

    async def step(self, conv: Conversation) -> ModelTurn:
        params = self.request(conv)
        t0 = time.perf_counter()

        async def create() -> Any:
            resp = await quota_guard(self.client.responses.create(**params))
            if _get(resp, "status") == "failed":
                raise ResponseFailed(_get(_get(resp, "error"), "code") or "failed")
            return resp

        resp = await with_retries(create, **self._retry)
        latency = int((time.perf_counter() - t0) * 1000)
        conv.history.extend(_item(i) for i in (_get(resp, "output") or []))
        return self.parse(resp, latency)

    def parse(self, resp: Any, latency_ms: int) -> ModelTurn:
        texts: list[str] = []
        refusals: list[str] = []
        calls: list[ToolCall] = []
        for item in _get(resp, "output") or []:
            kind = _get(item, "type")
            if kind == "message":
                for part in _get(item, "content") or []:
                    ptype = _get(part, "type")
                    if ptype == "output_text":
                        texts.append(_get(part, "text") or "")
                    elif ptype == "refusal":
                        refusals.append(_get(part, "refusal") or "")
            elif kind == "function_call":
                args, err = _parse_args(_get(item, "arguments") or "")
                calls.append(
                    ToolCall(
                        id=_get(item, "call_id"),
                        name=_get(item, "name"),
                        arguments=args,
                        parse_error=err,
                    )
                )
        text = "".join(texts)

        status = _get(resp, "status") or "completed"
        reason = _get(_get(resp, "incomplete_details"), "reason")
        category: str | None = None
        stop: Stop
        if reason == "content_filter":
            stop, category = "refusal", "content_filter"
        elif refusals:
            stop, category = "refusal", "refusal"
        elif status != "completed":
            # max_output_tokens and anything unknown are cut-off turns: their tool calls never run
            stop = "max_tokens"
        else:
            stop = "tool_use" if calls else "end"

        usage = _get(resp, "usage")
        prompt = _get(usage, "input_tokens", 0) or 0
        output = _get(usage, "output_tokens", 0) or 0
        reasoning = _get(_get(usage, "output_tokens_details"), "reasoning_tokens", 0) or 0
        reasoning = min(reasoning, output)
        in_details = _get(usage, "input_tokens_details")
        cached = min(_get(in_details, "cached_tokens", 0) or 0, prompt)
        written = min(_get(in_details, "cache_write_tokens", 0) or 0, prompt - cached)
        u = Usage(
            tokens_in=prompt,  # cached and cache-write tokens included
            tokens_out=output - reasoning,  # visible output only
            tokens_reasoning=reasoning,
            tokens_cached=cached,
            tokens_cache_write=written,
        )
        return ModelTurn(
            text=text or "".join(refusals),
            tool_calls=tuple(calls),
            stop=stop,
            usage=u,
            cost_usd=call_cost(
                self._price,
                tokens_in=u.tokens_in,
                tokens_out=u.tokens_out,
                tokens_reasoning=u.tokens_reasoning,
                tokens_cached=u.tokens_cached,
                tokens_cache_write=u.tokens_cache_write,
            ),
            latency_ms=latency_ms,
            model_snapshot=_get(resp, "model") or self.model,
            request_id=_get(resp, "_request_id") or _get(resp, "id"),
            refusal_category=category,
        )

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            conv.history.append(
                {"type": "function_call_output", "call_id": r.call_id, "output": content}
            )

    def add_user(self, conv: Conversation, text: str) -> None:
        conv.history.append({"role": "user", "content": text})
