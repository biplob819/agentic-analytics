"""Fake provider clients that return real SDK response objects (EK L-17)."""

from __future__ import annotations

from typing import Any

from anthropic.types import Message
from openai.types.chat import ChatCompletion


class _Recorder:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Any:
        self.calls.append(params)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _Recorder(responses)


class _Chat:
    def __init__(self, rec: _Recorder) -> None:
        self.completions = rec


class FakeOpenAI:
    def __init__(self, responses: list[Any]) -> None:
        self.rec = _Recorder(responses)
        self.chat = _Chat(self.rec)


def claude_message(
    content: list[dict[str, Any]],
    *,
    stop: str = "tool_use",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> Message:
    data: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }
    if stop_details is not None:
        data["stop_details"] = stop_details
    return Message.model_validate(data)


def gpt_completion(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str = "tool_calls",
    refusal: str | None = None,
    usage: dict[str, Any] | None = None,
) -> ChatCompletion:
    msg: dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-6.1-sol",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish, "logprobs": None}],
            "usage": usage
            or {
                "prompt_tokens": 1000,
                "completion_tokens": 300,
                "total_tokens": 1300,
                "prompt_tokens_details": {"cached_tokens": 600},
                "completion_tokens_details": {"reasoning_tokens": 200},
            },
        }
    )


class Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


async def no_sleep(_: float) -> None:
    return None
