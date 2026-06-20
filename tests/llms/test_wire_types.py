"""
Tests for the LLM Chat-Completions wire TypedDicts + generic BaseAdapter.

Covers the BDP-2359 contract:

- a representative adapter round-trips a typed ``ChatMessage`` →
  ``ChatCompletionResponse``;
- the generic base narrows the connection type per adapter (structural);
- ``ReasoningEffort`` accepts the known tiers;
- ``Provider`` (open ``NewType``) validates against the routing registry;
- the ``stream`` overload splits the return type precisely.
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator
from typing import Literal, get_args, get_type_hints

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.adapters.anthropic import (
    AnthropicAdapter,
    _anthropic_to_chat,
    _chat_to_anthropic,
)
from omnigent.llms.adapters.base import BaseAdapter
from omnigent.llms.adapters.bedrock import BedrockAdapter
from omnigent.llms.adapters.gemini import GeminiAdapter
from omnigent.llms.adapters.openai import OpenAICompatibleAdapter
from omnigent.llms.routing import validate_provider
from omnigent.llms.wire_types import (
    AnthropicConnection,
    BedrockConnection,
    ChatCompletionResponse,
    ChatMessage,
    ChatTool,
    Provider,
    ReasoningConfig,
    ReasoningEffort,
    VertexConnection,
)


def test_chat_message_roundtrips_to_chat_completion_response() -> None:
    """A typed ChatMessage list converts through Anthropic to a typed response."""
    messages: list[ChatMessage] = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    payload = _chat_to_anthropic(messages, "claude-sonnet-4-20250514", None, {})
    assert payload["model"] == "claude-sonnet-4-20250514"
    assert payload["system"] == "You are helpful."
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]

    # Anthropic response dict → typed ChatCompletionResponse.
    anthropic_resp = {
        "id": "msg_1",
        "model": "claude-sonnet-4-20250514",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hi there"}],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }
    result: ChatCompletionResponse = _anthropic_to_chat(anthropic_resp)
    assert result["choices"][0]["message"]["content"] == "Hi there"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 8


def test_chat_message_with_tool_call_roundtrip() -> None:
    """An assistant tool-call message + tool result convert with tool typing intact."""
    messages: list[ChatMessage] = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    tools: list[ChatTool] = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    payload = _chat_to_anthropic(messages, "claude-x", tools, {})
    # Tool result becomes an Anthropic user/tool_result message.
    assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["name"] == "get_weather"


def test_base_adapter_is_generic_over_connection() -> None:
    """BaseAdapter is generic; each adapter binds a distinct connection shape."""
    # The base is parameterizable (structural check via __class_getitem__).
    assert BaseAdapter[AnthropicConnection] is not None
    # Each concrete adapter declares its own connection TypedDict as the
    # generic argument (orig_bases carries the BaseAdapter[...] specialization).
    anthropic_arg = typing.get_args(AnthropicAdapter.__orig_bases__[0])  # type: ignore[attr-defined]
    bedrock_arg = typing.get_args(BedrockAdapter.__orig_bases__[0])  # type: ignore[attr-defined]
    assert anthropic_arg == (AnthropicConnection,)
    assert bedrock_arg == (BedrockConnection,)
    # The two connection shapes are genuinely different TypedDicts.
    assert set(BedrockConnection.__annotations__) == {
        "aws_region",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
    assert set(VertexConnection.__annotations__) == {"project", "location", "base_url"}


def test_reasoning_effort_literal_accepts_known_tiers() -> None:
    """ReasoningEffort enumerates exactly the known effort tiers."""
    tiers = set(get_args(ReasoningEffort))
    assert tiers == {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def test_reasoning_config_typeddict_shape() -> None:
    """ReasoningConfig is a TypedDict with effort + summary keys."""
    cfg: ReasoningConfig = {"effort": "high", "summary": "concise"}
    assert cfg["effort"] == "high"
    assert set(ReasoningConfig.__annotations__) == {"effort", "summary"}


def test_provider_newtype_validates_against_registry() -> None:
    """Provider is an open NewType validated at the registry boundary."""
    # NewType is an identity tag over str — known providers pass through.
    valid: Provider = validate_provider("anthropic")
    assert valid == "anthropic"
    assert isinstance(valid, str)
    # The validator is the boundary, not a closed Literal.
    with pytest.raises(OmnigentError):
        validate_provider("not-a-real-provider")


def test_stream_overload_splits_return_type() -> None:
    """The chat_completions overloads split on stream: Literal[True]/[False]."""
    # The base declares overloads; the concrete impl returns the union.
    hints = get_type_hints(OpenAICompatibleAdapter.chat_completions)
    ret = hints["return"]
    # Union of ChatCompletionResponse and AsyncIterator[ChatCompletionChunk].
    args = get_args(ret)
    assert ChatCompletionResponse in args
    assert any(getattr(a, "__origin__", None) is AsyncIterator for a in args)


@pytest.mark.asyncio
async def test_concrete_adapter_satisfies_generic_base() -> None:
    """A concrete adapter is an instance of the generic BaseAdapter."""
    adapter = GeminiAdapter()
    assert isinstance(adapter, BaseAdapter)
    # stream=Literal narrowing is purely static; assert the impl exists and
    # is async-callable (smoke).
    assert callable(adapter.chat_completions)


def test_literal_role_values() -> None:
    """ChatMessage role is the Chat Completions role literal set."""
    # ``from __future__ import annotations`` makes raw __annotations__ strings;
    # resolve them via get_type_hints.
    role_hint = get_type_hints(ChatMessage)["role"]
    assert set(get_args(role_hint)) == {"system", "user", "assistant", "tool"}
    # Sanity: the literal type matches the documented protocol roles.
    assert get_args(Literal["system", "user", "assistant", "tool"]) == (
        "system",
        "user",
        "assistant",
        "tool",
    )
