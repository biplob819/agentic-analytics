"""GPT through Chat Completions with function tools (MOD-02).

Chat Completions is the endpoint EvalKit's real runs used with GPT-6.1 Sol. Reasoning tokens are
reported separately and billed as output. No sampling parameters (EvalKit D-22).
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

STOP_MAP: dict[str, Stop] = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    fn: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }
    if spec.strict:
        fn["strict"] = True
    return {"type": "function", "function": fn}


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
        return Conversation(
            system=system,
            tools=tools,
            history=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )

    def request(self, conv: Conversation) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": list(conv.history),
            "max_completion_tokens": self.max_tokens,
            "reasoning_effort": self.effort,
        }
        if conv.tools:  # an empty tools list is not sent (the API may reject it)
            params["tools"] = [tool_param(t) for t in conv.tools]
        return params

    async def step(self, conv: Conversation) -> ModelTurn:
        params = self.request(conv)
        t0 = time.perf_counter()
        resp = await with_retries(
            lambda: quota_guard(self.client.chat.completions.create(**params)), **self._retry
        )
        latency = int((time.perf_counter() - t0) * 1000)
        turn, message = self._parse(resp, latency)
        conv.history.append(message)
        return turn

    def parse(self, resp: Any, latency_ms: int) -> ModelTurn:
        return self._parse(resp, latency_ms)[0]

    def _parse(self, resp: Any, latency_ms: int) -> tuple[ModelTurn, dict[str, Any]]:
        choice = _get(resp, "choices")[0]
        msg = _get(choice, "message")
        text = _get(msg, "content") or ""
        raw_calls = _get(msg, "tool_calls") or []
        calls: list[ToolCall] = []
        sent_calls: list[dict[str, Any]] = []
        for c in raw_calls:
            fn = _get(c, "function")
            name, raw_args = _get(fn, "name"), _get(fn, "arguments") or ""
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                err = None if isinstance(args, dict) else "arguments are not a JSON object"
            except json.JSONDecodeError as exc:
                args, err = None, f"arguments are not JSON: {exc.msg}"
            except (RecursionError, ValueError):
                args, err = None, "arguments are not parseable JSON"
            calls.append(
                ToolCall(
                    id=_get(c, "id"),
                    name=name,
                    arguments=args if isinstance(args, dict) else None,
                    parse_error=err,
                )
            )
            sent_calls.append(
                {
                    "id": _get(c, "id"),
                    "type": "function",
                    "function": {"name": name, "arguments": raw_args},
                }
            )
        usage = _get(resp, "usage")
        prompt = _get(usage, "prompt_tokens", 0) or 0
        completion = _get(usage, "completion_tokens", 0) or 0
        reasoning = _get(_get(usage, "completion_tokens_details") or {}, "reasoning_tokens", 0)
        reasoning = min(reasoning or 0, completion)
        cached = _get(_get(usage, "prompt_tokens_details") or {}, "cached_tokens", 0) or 0
        u = Usage(
            tokens_in=prompt,
            tokens_out=max(completion - reasoning, 0),
            tokens_reasoning=reasoning,
            tokens_cached=min(cached, prompt),
        )
        refusal = _get(msg, "refusal")
        finish = _get(choice, "finish_reason") or "stop"
        # unknown finish reasons are cut-off turns, as for Claude: their tool calls never run
        stop: Stop = "refusal" if refusal else STOP_MAP.get(finish, "max_tokens")
        if calls and stop == "end":
            stop = "tool_use"
        message: dict[str, Any] = {
            "role": "assistant",
            "content": text or (None if sent_calls else (refusal or "")),
        }
        if sent_calls:
            message["tool_calls"] = sent_calls
        turn = ModelTurn(
            text=text or (refusal or ""),
            tool_calls=tuple(calls),
            stop=stop,
            usage=u,
            cost_usd=call_cost(
                self._price,
                tokens_in=u.tokens_in,
                tokens_out=u.tokens_out,
                tokens_reasoning=u.tokens_reasoning,
                tokens_cached=u.tokens_cached,
            ),
            latency_ms=latency_ms,
            model_snapshot=_get(resp, "model") or self.model,
            request_id=_get(resp, "_request_id") or _get(resp, "id"),
        )
        return turn, message

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            conv.history.append({"role": "tool", "tool_call_id": r.call_id, "content": content})

    def add_user(self, conv: Conversation, text: str) -> None:
        conv.history.append({"role": "user", "content": text})
