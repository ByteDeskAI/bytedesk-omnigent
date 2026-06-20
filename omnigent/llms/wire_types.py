"""
Wire-format TypedDicts for the LLM Chat-Completions adapter contract.

Every provider adapter speaks Chat Completions internally — messages,
tools, response, and streaming-chunk dicts. These were historically
annotated as opaque ``list[dict[str, Any]]`` / ``dict[str, Any]``. This
module names the actual shapes so the adapter layer, the
Responses↔Chat bridge, and the streaming accumulator share one typed
contract.

These are :class:`~typing.TypedDict` definitions: they describe the
exact keys that already flow at runtime, so existing dict construction
conforms without behavior change. Optional keys use ``total=False``.

Also defines:

- ``Provider`` — an open :func:`~typing.NewType` over ``str`` validated
  against the provider registry at the boundary (not a closed Literal,
  so the open registry is not fought).
- ``ToolChoice`` — the fixed Chat Completions tool-choice protocol set.
- ``ReasoningEffort`` / ``ReasoningConfig`` — typed reasoning controls.
- Per-provider ``connection_params`` TypedDicts used to parameterize the
  generic :class:`~omnigent.llms.adapters.base.BaseAdapter`.
"""

from __future__ import annotations

from typing import Any, Literal, NewType, TypedDict

# ── Roles, reasoning, provider, tool-choice ───────────────

# Chat Completions message roles.
ChatRole = Literal["system", "user", "assistant", "tool"]

# Reasoning effort tiers (superset across providers). Per-provider
# validation against the supported subset stays in
# ``omnigent.reasoning_effort``.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

# OpenAI Responses ``reasoning`` summary verbosity.
ReasoningSummary = Literal["auto", "concise", "detailed"]

# Provider identifier. Kept open (NewType over str) rather than a closed
# Literal — the provider set lives in the routing registry
# (``PROVIDER_CONFIGS``) and is validated there at the boundary. A frozen
# Literal would fight that open registry.
Provider = NewType("Provider", str)

# Chat Completions ``tool_choice`` protocol set. The named-function form
# is ``{"type": "function", "function": {"name": "..."}}`` and is
# represented by the dict branch of the union at call sites.
ToolChoice = Literal["none", "auto", "required"]


class ReasoningConfig(TypedDict, total=False):
    """
    Reasoning configuration block (OpenAI Responses ``reasoning``).

    :param effort: Reasoning effort tier.
    :param summary: Summary verbosity for streamed reasoning.
    """

    effort: ReasoningEffort
    summary: ReasoningSummary


# ── Per-provider connection_params shapes ─────────────────


class OpenAIConnection(TypedDict, total=False):
    """Connection overrides for OpenAI-compatible providers."""

    api_key: str
    base_url: str


# Anthropic accepts the same key set as OpenAI-compatible
# (``api_key`` + optional ``base_url``).
AnthropicConnection = OpenAIConnection

# Gemini (API-key auth) uses the same overrides shape.
GeminiConnection = OpenAIConnection

# Databricks resolves ``base_url`` + ``api_key`` (auto-resolved when absent).
DatabricksConnection = OpenAIConnection


class BedrockConnection(TypedDict, total=False):
    """Connection overrides for AWS Bedrock (Converse API)."""

    aws_region: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str


class VertexConnection(TypedDict, total=False):
    """
    Connection overrides for Google Vertex AI.

    ``project`` + ``location`` are resolved into a ``base_url`` before
    delegating to the Gemini translation layer; a full ``base_url`` may
    be supplied directly instead.
    """

    project: str
    location: str
    base_url: str


# ── Tool schemas ──────────────────────────────────────────


class ChatFunctionDef(TypedDict, total=False):
    """A Chat Completions function definition (the ``function`` block)."""

    name: str
    description: str
    # JSON Schema for the tool parameters — an open object shape.
    parameters: dict[str, Any]


class ChatTool(TypedDict):
    """A Chat Completions tool entry: ``{"type": "function", "function": {...}}``."""

    type: Literal["function"]
    function: ChatFunctionDef


class ChatFunctionCall(TypedDict):
    """The ``function`` block inside an assistant tool call."""

    name: str
    # Arguments are a JSON-encoded string per the Chat Completions wire format.
    arguments: str


class ChatToolCall(TypedDict, total=False):
    """An assistant tool call entry in a message's ``tool_calls`` array."""

    id: str
    type: Literal["function"]
    function: ChatFunctionCall


# ── Messages ──────────────────────────────────────────────

# Message content is either a plain string or a list of multimodal
# content-part dicts (text / image_url / input_file / …). The part shapes
# are provider-translated and stay open.
ChatContent = str | list[dict[str, Any]] | None


class ChatMessage(TypedDict, total=False):
    """
    A Chat Completions message.

    ``role`` is the only always-present key; the rest are role-dependent:
    ``content`` (most roles), ``tool_calls`` (assistant), ``tool_call_id``
    (tool result). ``_tool_name`` is an internal hint the Gemini adapter
    reads to label a function response.
    """

    role: ChatRole
    content: ChatContent
    tool_calls: list[ChatToolCall] | None
    tool_call_id: str
    name: str
    # Internal: function name carried on a tool message for Gemini.
    _tool_name: str


# ── Usage ─────────────────────────────────────────────────


class ChatUsage(TypedDict, total=False):
    """Chat Completions token usage block."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


# ── Non-streaming response ────────────────────────────────


class ChatResponseMessage(TypedDict, total=False):
    """The ``message`` object inside a non-streaming choice."""

    role: ChatRole
    content: str | None
    tool_calls: list[ChatToolCall] | None


class ChatChoice(TypedDict, total=False):
    """A non-streaming Chat Completions choice."""

    index: int
    message: ChatResponseMessage
    finish_reason: str | None


class ChatCompletionResponse(TypedDict, total=False):
    """A non-streaming Chat Completions response envelope."""

    id: str
    object: str
    created: int
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage


# ── Streaming chunk ───────────────────────────────────────


class ChatChunkToolCallFunction(TypedDict, total=False):
    """The ``function`` delta inside a streaming tool-call delta."""

    name: str
    arguments: str


class ChatChunkToolCall(TypedDict, total=False):
    """A streaming tool-call delta (carries an ``index`` for accumulation)."""

    index: int
    id: str
    type: Literal["function"]
    function: ChatChunkToolCallFunction


class ChatChunkDelta(TypedDict, total=False):
    """The ``delta`` object inside a streaming choice."""

    role: ChatRole
    content: str | list[Any] | None
    tool_calls: list[ChatChunkToolCall]


class ChatChunkChoice(TypedDict, total=False):
    """A streaming Chat Completions choice."""

    index: int
    delta: ChatChunkDelta
    finish_reason: str | None


class ChatCompletionChunk(TypedDict, total=False):
    """A streaming Chat Completions chunk envelope."""

    id: str
    object: str
    created: int
    model: str | None
    choices: list[ChatChunkChoice]
    usage: ChatUsage


__all__ = [
    "AnthropicConnection",
    "BedrockConnection",
    "ChatChoice",
    "ChatChunkChoice",
    "ChatChunkDelta",
    "ChatChunkToolCall",
    "ChatChunkToolCallFunction",
    "ChatCompletionChunk",
    "ChatCompletionResponse",
    "ChatContent",
    "ChatFunctionCall",
    "ChatFunctionDef",
    "ChatMessage",
    "ChatResponseMessage",
    "ChatRole",
    "ChatTool",
    "ChatToolCall",
    "ChatUsage",
    "DatabricksConnection",
    "GeminiConnection",
    "OpenAIConnection",
    "Provider",
    "ReasoningConfig",
    "ReasoningEffort",
    "ReasoningSummary",
    "ToolChoice",
    "VertexConnection",
]
