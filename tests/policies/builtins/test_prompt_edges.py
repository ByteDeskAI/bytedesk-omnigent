"""Edge-case coverage for :mod:`omnigent.policies.builtins.prompt`."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.policies.builtins.prompt import _extract_response_text, _serialize_content, prompt_policy


class _StructuredResponse:
    """LLM response without ``output_text``."""

    def __init__(self, text: str) -> None:
        block = type("Block", (), {"text": text})()
        item = type("Item", (), {"content": [block]})()
        self.output = [item]


def test_serialize_content_falls_back_to_repr_on_circular_dict() -> None:
    """Non-JSON-serializable dicts fall back to ``repr``."""
    circular: dict[str, Any] = {}
    circular["self"] = circular
    rendered = _serialize_content(circular)
    assert "self" in rendered


def test_serialize_content_repr_for_scalar_types() -> None:
    """Scalars outside str/dict/list use ``repr``."""
    assert _serialize_content(42) == "42"


def test_extract_response_text_reads_structured_output() -> None:
    """Structured response path extracts nested text."""
    response = _StructuredResponse('{"action": "allow", "reason": ""}')
    assert _extract_response_text(response) == '{"action": "allow", "reason": ""}'


@pytest.mark.asyncio
async def test_allow_with_empty_string_reason() -> None:
    """ALLOW verdict normalizes an empty-string LLM reason to ``None`` internally."""
    evaluate = prompt_policy(prompt="Allow.")
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": AsyncMock(
            **{
                "create.return_value": type(
                    "R", (), {"output_text": json.dumps({"action": "allow", "reason": ""})}
                )()
            }
        ),
    }
    assert await evaluate(event) == {"result": "ALLOW"}


@pytest.mark.asyncio
async def test_deny_with_empty_string_reason_uses_default() -> None:
    """DENY with an empty-string LLM reason falls back to the default message."""
    evaluate = prompt_policy(prompt="Deny.")
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": AsyncMock(
            **{
                "create.return_value": type(
                    "R", (), {"output_text": json.dumps({"action": "deny", "reason": ""})}
                )()
            }
        ),
    }
    result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "Denied by prompt policy."}


@pytest.mark.asyncio
async def test_includes_request_data_and_session_state_in_prompt() -> None:
    """``request_data`` and ``session_state`` are threaded into the classifier prompt."""
    evaluate = prompt_policy(prompt="Check context.")
    client = AsyncMock()
    client.create.return_value = type(
        "R", (), {"output_text": json.dumps({"action": "allow", "reason": ""})}
    )()
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "request_data": {"prior": "question"},
        "session_state": {"risk_score": 12},
        "context": {},
        "llm_client": client,
    }
    await evaluate(event)
    prompt_text = client.create.call_args.kwargs["input"][0]["content"][0]["text"]
    assert "original request" in prompt_text
    assert "session state" in prompt_text
    assert "risk_score" in prompt_text


def test_extract_response_text_returns_empty_for_empty_content_list() -> None:
    """Structured response with empty ``content`` yields empty text."""
    item = type("Item", (), {"content": []})()
    response = type("R", (), {"output": [item]})()
    assert _extract_response_text(response) == ""


@pytest.mark.asyncio
async def test_structured_response_without_output_text() -> None:
    """Classifier reads nested output when ``output_text`` is missing."""
    evaluate = prompt_policy(prompt="Deny.")
    client = AsyncMock()
    client.create.return_value = _StructuredResponse(
        json.dumps({"action": "deny", "reason": "blocked"})
    )
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": client,
    }
    result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "blocked"}