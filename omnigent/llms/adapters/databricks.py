"""
Databricks Model Serving adapter.

Extends the OpenAI-compatible adapter with Databricks-specific
authentication. When ``connection_params`` omits ``base_url``, the
adapter auto-resolves credentials via
:func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`,
which honors ``DATABRICKS_CONFIG_PROFILE`` for profile selection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, cast, overload

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import ChatExtra
from omnigent.llms.adapters.openai import OpenAICompatibleAdapter
from omnigent.llms.wire_types import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    ChatMessage,
    ChatTool,
    DatabricksConnection,
)
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

# The union-returning ``chat_completions`` implementation type, used to
# call ``super().chat_completions`` with a runtime ``bool`` ``stream``
# (the parent's ``@overload``s only resolve for ``Literal`` ``stream``).
_ChatCompletionsImpl = Callable[
    ..., Awaitable[ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]]
]


class DatabricksAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Databricks Model Serving.

    Credentials are resolved in the following order:

    1. ``connection_params`` passed at call time (from the ``connection:``
       block in the agent spec's ``llm:`` config) — used when present.
    2. Auto-resolved via
       :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`,
       which tries the databricks-sdk (all auth types) then falls back to
       the raw ``~/.databrickscfg`` configparser, honoring
       ``DATABRICKS_CONFIG_PROFILE`` for profile selection.

    An :class:`~omnigent.errors.OmnigentError` is raised only when
    both paths fail.
    """

    def __init__(self) -> None:
        super().__init__()

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: bool,
        extra: ChatExtra,
    ) -> dict[str, Any]:
        """
        Build the Chat Completions payload without ``stream_options``.

        Databricks model serving rejects ``stream_options`` with a 400 error
        (the field is an OpenAI extension that Databricks does not support).
        This override builds the standard payload and removes the key.

        :param messages: Chat Completions messages.
        :param model: Model name, e.g. ``"databricks-kimi-k2-6"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Whether to enable streaming.
        :param extra: Additional kwargs (temperature, etc.).
        :returns: The request payload dict without ``stream_options``.
        """
        payload = super()._build_payload(messages, model, tools, stream, extra)
        payload.pop("stream_options", None)
        return payload

    @overload
    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: Literal[False],
        extra: ChatExtra,
        *,
        connection_params: DatabricksConnection | None = ...,
        timeout: int | None = ...,
    ) -> ChatCompletionResponse: ...

    @overload
    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: Literal[True],
        extra: ChatExtra,
        *,
        connection_params: DatabricksConnection | None = ...,
        timeout: int | None = ...,
    ) -> AsyncIterator[ChatCompletionChunk]: ...

    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: bool,
        extra: ChatExtra,
        *,
        connection_params: DatabricksConnection | None = None,
        timeout: int | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]:
        """
        Send a Chat Completions request to Databricks Model Serving.

        :param messages: Chat Completions format messages.
        :param model: Model name, e.g. ``"databricks-gpt-5-4"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs.
        :param connection_params: Optional. When provided, must contain
            ``"base_url"``; ``"api_key"`` is also expected. When absent
            or missing ``"base_url"``, credentials are auto-resolved via
            :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Response dict or async iterator of chunk dicts.
        :raises OmnigentError: If ``connection_params`` lacks
            ``"base_url"`` and auto-resolution from ``~/.databrickscfg``
            also fails.
        """
        if not connection_params or "base_url" not in connection_params:
            try:
                creds = resolve_databricks_workspace(None)
            except OSError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            resolved: DatabricksConnection = {
                "base_url": creds.host + "/serving-endpoints",
                "api_key": creds.token,
            }
            connection_params = {**resolved, **(connection_params or {})}
        # ``stream`` is a runtime bool, so call the union-returning
        # implementation rather than the Literal-split overloads.
        impl = cast("_ChatCompletionsImpl", super().chat_completions)
        return await impl(
            messages,
            model,
            tools,
            stream,
            extra,
            connection_params=connection_params,
            timeout=timeout,
        )
