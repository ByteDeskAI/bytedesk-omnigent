"""
Google Vertex AI adapter.

Uses the same Gemini payload format but with GCP auth (Application
Default Credentials or service account) and Vertex AI endpoints.
Ported from MLflow AI Gateway's VertexAIProvider.

Connection config (``project``, ``location``) must be provided via
``connection_params`` at call time — typically from the
``connection:`` block in the agent spec's ``llm:`` config.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, cast, overload

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import ChatExtra
from omnigent.llms.adapters.gemini import GeminiAdapter
from omnigent.llms.wire_types import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    ChatMessage,
    ChatTool,
    GeminiConnection,
    VertexConnection,
)

# The union-returning ``chat_completions`` implementation type, used to
# call ``super().chat_completions`` with a runtime ``bool`` ``stream``
# (the parent's ``@overload``s only resolve for ``Literal`` ``stream``).
_ChatCompletionsImpl = Callable[
    ..., Awaitable[ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]]
]

_DEFAULT_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Timeout for the GCP OAuth token refresh HTTP request.
# Tighter than the SDK default (120s) so a hanging metadata
# server doesn't block the LLM call too long.
_AUTH_REFRESH_TIMEOUT = 30


class VertexAdapter(GeminiAdapter):
    """
    Adapter for Google Vertex AI.

    Inherits Gemini translation logic but uses Vertex AI endpoints
    and GCP OAuth authentication.

    Requires ``connection_params`` with:
    - ``"project"``: GCP project ID, e.g. ``"my-gcp-project"``.
    - ``"location"``: GCP region, e.g. ``"us-central1"``.

    Or alternatively a full ``"base_url"`` override.

    These come from the ``connection:`` block in the agent spec's
    ``llm:`` config — not from environment variables.
    """

    def __init__(self) -> None:
        self._cached_credentials: Any = None

    def _get_credentials(self) -> Any:
        """
        Get GCP credentials, refreshing if needed.

        :returns: A ``google.auth.credentials.Credentials`` object
            with a valid access token.
        """
        if self._cached_credentials is not None and self._cached_credentials.valid:
            return self._cached_credentials

        import functools

        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(scopes=_DEFAULT_SCOPES)
        request = google.auth.transport.requests.Request()
        # 30s timeout for the OAuth token refresh HTTP request.
        # The SDK default is 120s; we tighten it so a hanging
        # metadata server doesn't block the LLM call too long.
        credentials.refresh(  # type: ignore[no-untyped-call]
            functools.partial(request, timeout=_AUTH_REFRESH_TIMEOUT)
        )
        self._cached_credentials = credentials
        return credentials

    async def _get_headers(
        self,
        api_key_override: str | None = None,
    ) -> dict[str, str]:
        """
        Build Vertex AI headers with OAuth bearer token.

        Offloads ``_get_credentials()`` to a thread because the
        Google auth token refresh does a blocking HTTP request
        to the metadata server / OAuth endpoint (100-500ms).

        :param api_key_override: Not used by Vertex AI (uses GCP
            OAuth). Accepted for interface compatibility.
        :returns: Headers dict with Authorization.
        """
        import asyncio

        credentials = await asyncio.to_thread(
            self._get_credentials,
        )
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credentials.token}",
        }

    def _get_base_url(self) -> str:
        """
        Not used — Vertex AI requires ``connection_params``.

        :returns: Never returns.
        :raises OmnigentError: Always — Vertex requires connection_params
            with ``"project"`` and ``"location"``.
        """
        raise OmnigentError(
            "Vertex AI requires 'project' and 'location' in"
            " connection_params (from llm.connection config)",
            code=ErrorCode.INVALID_INPUT,
        )

    @overload  # type: ignore[override]  # Vertex connection shape (project/location) intentionally differs from the GeminiConnection base param.
    async def chat_completions(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ChatTool] | None,
        stream: Literal[False],
        extra: ChatExtra,
        *,
        connection_params: VertexConnection | None = ...,
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
        connection_params: VertexConnection | None = ...,
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
        connection_params: VertexConnection | None = None,
        timeout: int | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]:
        """
        Send a request to Vertex AI.

        :param messages: Chat Completions format messages.
        :param model: Model name, e.g. ``"gemini-2.5-pro"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs.
        :param connection_params: Required. Must contain
            ``"project"`` + ``"location"`` or ``"base_url"``.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Chat Completions response dict or async chunk
            iterator.
        :raises OmnigentError: If ``connection_params`` is missing
            or lacks required keys.
        """
        resolved_params = _resolve_vertex_params(connection_params)
        # ``stream`` is a runtime bool, so call the union-returning
        # implementation rather than the parent's Literal-split overloads.
        impl = cast("_ChatCompletionsImpl", super().chat_completions)
        return await impl(
            messages,
            model,
            tools,
            stream,
            extra,
            connection_params=resolved_params,
            timeout=timeout,
        )


def _resolve_vertex_params(
    connection_params: VertexConnection | None,
) -> GeminiConnection:
    """
    Convert Vertex-specific ``"project"``/``"location"`` keys into
    a ``"base_url"`` that the parent Gemini adapter understands.

    All connection info must come from ``connection_params`` — no
    environment variable fallbacks.

    :param connection_params: Raw connection params from the caller.
        Must contain ``"project"`` + ``"location"`` or ``"base_url"``.
    :returns: Params with ``"base_url"`` resolved, in the Gemini shape.
    :raises OmnigentError: If params are missing or incomplete.
    """
    if not connection_params:
        raise OmnigentError(
            "Vertex AI requires connection_params with"
            " 'project' and 'location' (from llm.connection config)",
            code=ErrorCode.INVALID_INPUT,
        )

    # If caller provided a full base_url, pass through as-is.
    if "base_url" in connection_params:
        return cast("GeminiConnection", connection_params)

    project = connection_params.get("project")
    location = connection_params.get("location")
    if not project:
        raise OmnigentError(
            "Vertex AI requires 'project' in connection_params (from llm.connection config)",
            code=ErrorCode.INVALID_INPUT,
        )
    if not location:
        raise OmnigentError(
            "Vertex AI requires 'location' in connection_params (from llm.connection config)",
            code=ErrorCode.INVALID_INPUT,
        )
    return cast(
        "GeminiConnection",
        {
            **connection_params,
            "base_url": _build_vertex_url(project, location),
        },
    )


def _build_vertex_url(project: str, location: str) -> str:
    """
    Build the Vertex AI endpoint URL from project and location.

    :param project: GCP project ID.
    :param location: GCP region, e.g. ``"us-central1"``.
    :returns: The Vertex AI base URL.
    """
    return (
        f"https://{location}-aiplatform.googleapis.com"
        f"/v1/projects/{project}"
        f"/locations/{location}"
        f"/publishers/google/models"
    )
