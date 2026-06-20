"""
Shared tool-result-to-string formatter.

Tool results reach the model as a single string, but they arrive in
two shapes that historically rendered through two separate, drifting
code paths:

- the **MCP content-block path** — a ``CallToolResult`` whose
  ``content`` is a list of ``ContentBlock`` (``TextContent``,
  ``ImageContent``, ``AudioContent``, ``EmbeddedResource``,
  ``ResourceLink``), formatted in :mod:`omnigent.tools.mcp`; and
- the **raw-value path** — the in-process return value of a local
  callable (str / None / arbitrary JSON-able object), formatted in
  :mod:`omnigent.tools.local_callable`.

Keeping image/audio/resource rendering in one place removes the
MCP-vs-in-process drift: both call sites delegate here so a content
block and an equivalent raw value stringify identically.

The :class:`ToolResultFormatter` Protocol is the swappable seam
(ADR-0008 Strategy) — callers depend on the Protocol, the default
:class:`DefaultToolResultFormatter` reproduces the prior byte-for-byte
behavior, and an alternate renderer (e.g. truncating, redacting) can be
substituted without touching the call sites.

This module imports only ``json`` + ``mcp.types`` — it must stay free
of heavy/runtime imports so it is cheap on every tool-dispatch path.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from mcp.types import ContentBlock, TextContent


@runtime_checkable
class ToolResultFormatter(Protocol):
    """Strategy for coercing tool results into model-facing strings.

    Covers both the MCP content-block shape and the raw return value
    of an in-process callable, so the two dispatch paths render
    identically.
    """

    def format_content_block(self, block: ContentBlock) -> str:
        """Render a single MCP content block as a string."""
        ...

    def format_value(self, value: Any) -> str:
        """Render a raw (non-MCP) tool return value as a string."""
        ...


class DefaultToolResultFormatter:
    """Default :class:`ToolResultFormatter` — byte-for-byte legacy behavior.

    Reproduces the two original functions exactly:

    - :meth:`format_content_block` mirrors the old
      ``omnigent.tools.mcp._format_content_block``: ``.text`` for
      ``TextContent``, otherwise ``json.dumps(block.model_dump())``.
    - :meth:`format_value` mirrors the old
      ``omnigent.tools.local_callable._stringify``: ``str`` passes
      through, ``None`` becomes ``""``, everything else routes through
      ``json.dumps`` with a ``repr`` fallback.
    """

    def format_content_block(self, block: ContentBlock) -> str:
        """
        Convert a single MCP content block to a string.

        Returns ``.text`` for ``TextContent`` (the most common case).
        For non-text types (``ImageContent``, ``AudioContent``,
        ``EmbeddedResource``, ``ResourceLink``), serializes the full
        Pydantic model to JSON.

        :param block: A content block from ``CallToolResult.content``,
            e.g. ``TextContent(type="text", text="hello")``.
        :returns: A string representation of the block.
        """
        if isinstance(block, TextContent):
            return block.text
        # All ContentBlock variants are Pydantic BaseModels.
        return json.dumps(block.model_dump())

    def format_value(self, value: Any) -> str:
        """
        Coerce a tool's return value into a string for the workflow.

        Strings pass through; ``None`` becomes the empty string;
        everything else routes through :func:`json.dumps` with a
        fallback to :func:`repr` for objects JSON cannot encode.

        :param value: The wrapped callable's return value.
        :returns: A string suitable for the
            ``function_call_output.output`` field.
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return repr(value)


#: Shared default formatter instance both dispatch paths delegate to.
DEFAULT_TOOL_RESULT_FORMATTER: ToolResultFormatter = DefaultToolResultFormatter()
