"""Fake provider clients that return real SDK response objects (EK L-17)."""

from __future__ import annotations

from typing import Any

from anthropic.types import Message
from openai.types.responses import Response


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


class FakeOpenAI:
    """`client.responses.create` (the Responses API, D-54); `rec` is the recorder."""

    def __init__(self, responses: list[Any]) -> None:
        self.rec = _Recorder(responses)
        self.responses = self.rec


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


ENCRYPTED = "gAAAA-encrypted-reasoning-blob"
GPT_USAGE: dict[str, Any] = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 0},
    "output_tokens": 300,
    "output_tokens_details": {"reasoning_tokens": 200},
    "total_tokens": 1300,
}


def gpt_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """A Responses `function_call` output item (arguments as the raw JSON string)."""
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def gpt_response(
    *,
    text: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    refusal: str | None = None,
    reasoning: bool = True,
    status: str = "completed",
    incomplete: str | None = None,
    usage: dict[str, Any] | None = None,
) -> Response:
    """A real SDK `Response`: an encrypted reasoning item, then a message, then function calls."""
    output: list[dict[str, Any]] = []
    if reasoning:
        output.append(
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": ENCRYPTED,
            }
        )
    parts: list[dict[str, Any]] = []
    if text is not None:
        parts.append({"type": "output_text", "text": text, "annotations": []})
    if refusal is not None:
        parts.append({"type": "refusal", "refusal": refusal})
    if parts:
        output.append(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": parts,
            }
        )
    output.extend(tool_calls or [])
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1,
            "model": "gpt-6.1-sol-2026-10-01",
            "status": status,
            "incomplete_details": {"reason": incomplete} if incomplete else None,
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": usage or GPT_USAGE,
        }
    )


class Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


async def no_sleep(_: float) -> None:
    return None
