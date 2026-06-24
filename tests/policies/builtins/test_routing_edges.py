"""Edge-case coverage for :mod:`omnigent.policies.builtins.routing`."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.policies.builtins.routing import _extract_response_text, deny_trivial_to_expensive_model

from .helpers import llm_request_event
from .test_routing import _EXPENSIVE, _FakePolicyLLMClient, _llm_request_with_client


class _StructuredResponse:
    """Response shape without ``output_text`` — uses nested ``output``."""

    def __init__(self, text: str) -> None:
        block = type("Block", (), {"text": text})()
        item = type("Item", (), {"content": [block]})()
        self.output = [item]


def test_extract_response_text_reads_structured_output() -> None:
    """Fallback path reads ``output[0].content[0].text`` when ``output_text`` is absent."""
    response = _StructuredResponse('{"difficulty": "COMPLEX"}')
    assert _extract_response_text(response) == '{"difficulty": "COMPLEX"}'


def test_extract_response_text_returns_empty_for_missing_output() -> None:
    """Unrecognized response shapes yield an empty string."""
    assert _extract_response_text(object()) == ""


def test_extract_response_text_returns_empty_for_empty_content_list() -> None:
    """Structured response with an empty ``content`` list yields empty text."""
    item = type("Item", (), {"content": []})()
    response = type("R", (), {"output": [item]})()
    assert _extract_response_text(response) == ""


@pytest.mark.asyncio
async def test_non_dict_llm_request_data_abstains() -> None:
    """Non-dict ``data`` on ``llm_request`` abstains without classifying."""
    client = _FakePolicyLLMClient(_StructuredResponse(json.dumps({"difficulty": "TRIVIAL"})))
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)
    event = _llm_request_with_client(client)
    event["data"] = "not-a-dict"
    assert await policy(event) is None
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_structured_response_abstains() -> None:
    """Classifier response with no extractable text abstains (fail open)."""
    client = AsyncMock()
    client.create.return_value = _StructuredResponse("")
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)
    event = llm_request_event(
        model="databricks-claude-opus-4-6",
        last_user_message="What is 2+2?",
    )
    event["llm_client"] = client
    assert await policy(event) is None