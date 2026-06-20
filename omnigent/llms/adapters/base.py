"""
Base adapter interface for LLM provider adapters.

Each adapter translates between Chat Completions format and the
provider's native API, and handles HTTP communication. All methods
are async — adapters use ``httpx.AsyncClient`` for non-blocking I/O.

:class:`BaseAdapter` is generic over the connection-params shape
(``TConn``, bound to a ``Mapping[str, str]``) so each adapter narrows
``connection_params`` to its own provider-specific TypedDict instead of a
base union.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Generic, Literal, TypeVar, overload

from omnigent.llms.wire_types import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    ChatMessage,
    ChatTool,
    ReasoningEffort,  # noqa: F401  (re-exported for adapter convenience)
)

# Connection-params shape for a given adapter, e.g. ``AnthropicConnection``
# or ``BedrockConnection``. Conceptually a ``Mapping[str, str]``; left
# unbounded because mypy does not treat a (``total=False``) TypedDict as a
# subtype of ``Mapping[str, str]``, so a hard bound would reject the very
# per-provider connection TypedDicts this generic exists to narrow to.
TConn = TypeVar("TConn")

# The ``extra`` provider-kwargs bag stays open (temperature, top_p,
# reasoning_effort, response_format, …) — value types are genuinely
# heterogeneous (str/float/int/dict), so a TypedDict would be lossy here.
ChatExtra = dict[str, Any]


class BaseAdapter(ABC, Generic[TConn]):
    """
    Abstract base class for provider adapters.

    Subclasses implement :meth:`chat_completions` to send a request
    in the provider's native format and return a
    :class:`~omnigent.llms.wire_types.ChatCompletionResponse` (or async
    iterator of :class:`~omnigent.llms.wire_types.ChatCompletionChunk`
    for streaming).

    The class is generic over ``TConn``, the per-adapter
    ``connection_params`` shape, e.g.
    ``class AnthropicAdapter(BaseAdapter[AnthropicConnection])``.
    """

    @overload
    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: Literal[False],
        extra: ChatExtra,
        *,
        connection_params: TConn | None = ...,
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
        connection_params: TConn | None = ...,
        timeout: int | None = ...,
    ) -> AsyncIterator[ChatCompletionChunk]: ...

    @abstractmethod
    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: bool,
        extra: ChatExtra,
        *,
        connection_params: TConn | None = None,
        timeout: int | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]:
        """
        Send a chat completions request to the provider.

        :param messages: Chat Completions format messages, e.g.
            ``[{"role": "user", "content": "Hello"}]``.
        :param model: The model name (without provider prefix),
            e.g. ``"claude-sonnet-4-20250514"``.
        :param tools: Chat Completions tool schemas, or ``None``.
        :param stream: If ``True``, return an async iterator of
            chunks. If ``False``, return a single response.
        :param extra: Additional provider-specific kwargs, e.g.
            ``{"temperature": 0.7, "reasoning_effort": "high"}``.
        :param connection_params: Per-call connection overrides,
            narrowed to this adapter's ``TConn`` shape. ``None`` means
            use the adapter's default credentials (env vars, etc.).
            Common keys by provider:

            - OpenAI-compatible: ``{"api_key": "...",
              "base_url": "..."}``
            - Anthropic: ``{"api_key": "..."}``
            - Databricks: ``{"api_key": "...",
              "base_url": "..."}``
            - Bedrock: ``{"aws_region": "...",
              "aws_access_key_id": "...",
              "aws_secret_access_key": "..."}``
            - Vertex: ``{"project": "...", "location": "..."}``
        :param timeout: Request timeout in seconds. ``None`` uses
            the adapter's default (120s non-streaming, 300s
            streaming).
        :returns: A :class:`ChatCompletionResponse` when
            ``stream=False``, or an async iterator of
            :class:`ChatCompletionChunk` when ``stream=True``.
        """
        ...
