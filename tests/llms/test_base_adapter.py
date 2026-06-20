"""Tests for llms.adapters.base — ABC enforcement."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.llms.adapters.base import BaseAdapter


def test_cannot_instantiate_base_adapter() -> None:
    """BaseAdapter is abstract and cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        BaseAdapter()  # type: ignore[abstract]


def test_subclass_must_implement_chat_completions() -> None:
    """A subclass that does not implement chat_completions cannot be instantiated."""

    class IncompleteAdapter(BaseAdapter):
        pass

    with pytest.raises(TypeError, match="abstract"):
        IncompleteAdapter()  # type: ignore[abstract]


def test_concrete_subclass_can_be_instantiated() -> None:
    """A complete subclass that implements chat_completions can be instantiated."""

    class ConcreteAdapter(BaseAdapter):
        async def chat_completions(
            self,
            messages: list[dict[str, Any]],
            model: str,
            tools: list[dict[str, Any]] | None,
            stream: bool,
            extra: dict[str, Any],
            *,
            connection_params: dict[str, str] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
            return {"choices": []}

    adapter = ConcreteAdapter()
    assert isinstance(adapter, BaseAdapter)


def test_base_adapter_defaults_native_responses_off() -> None:
    """``supports_native_responses_api`` defaults to ``False`` so a new adapter
    routes through chat-completions unless it opts in (BDP-2352)."""

    class ConcreteAdapter(BaseAdapter):
        async def chat_completions(self, *a: Any, **k: Any):  # type: ignore[override]
            return {"choices": []}

    assert ConcreteAdapter().supports_native_responses_api is False


def test_openai_adapter_opts_into_native_responses() -> None:
    """``OpenAIAdapter`` (native Responses API) sets the flag ``True`` (BDP-2352)."""
    from omnigent.llms.adapters.openai import OpenAIAdapter

    assert OpenAIAdapter().supports_native_responses_api is True
