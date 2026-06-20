"""Tests for the shared tool-result formatter (BDP-2362).

The formatter must render identically to the two functions it
replaces:

- ``mcp._format_content_block`` (content-block path), and
- ``local_callable._stringify`` (raw-value path).

These tests pin byte-for-byte parity on representative inputs.
"""

from __future__ import annotations

import json

from mcp.types import ImageContent, TextContent

from omnigent.tools.result_formatter import (
    DEFAULT_TOOL_RESULT_FORMATTER,
    DefaultToolResultFormatter,
    ToolResultFormatter,
)


def test_default_satisfies_protocol() -> None:
    assert isinstance(DEFAULT_TOOL_RESULT_FORMATTER, ToolResultFormatter)
    assert isinstance(DefaultToolResultFormatter(), ToolResultFormatter)


# ── content-block path ──────────────────────────────────


def test_format_text_content_returns_text() -> None:
    block = TextContent(type="text", text="hello world")
    assert DEFAULT_TOOL_RESULT_FORMATTER.format_content_block(block) == "hello world"


def test_format_non_text_block_serializes_model_dump() -> None:
    block = ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")
    formatted = DEFAULT_TOOL_RESULT_FORMATTER.format_content_block(block)
    # Byte-for-byte: json.dumps(block.model_dump()) — the legacy behavior.
    assert formatted == json.dumps(block.model_dump())
    assert json.loads(formatted)["mimeType"] == "image/png"


# ── raw-value path ──────────────────────────────────────


def test_format_value_string_passthrough() -> None:
    assert DEFAULT_TOOL_RESULT_FORMATTER.format_value("plain") == "plain"


def test_format_value_none_is_empty_string() -> None:
    assert DEFAULT_TOOL_RESULT_FORMATTER.format_value(None) == ""


def test_format_value_json_object() -> None:
    value = {"a": 1, "b": [2, 3]}
    assert DEFAULT_TOOL_RESULT_FORMATTER.format_value(value) == json.dumps(value)


def test_format_value_falls_back_to_repr() -> None:
    class NotJsonable:
        def __repr__(self) -> str:
            return "<NotJsonable repr>"

    obj = NotJsonable()
    assert DEFAULT_TOOL_RESULT_FORMATTER.format_value(obj) == repr(obj)


# ── parity: text content vs raw string render identically ──


def test_text_block_and_raw_string_render_identically() -> None:
    text = "same string"
    via_block = DEFAULT_TOOL_RESULT_FORMATTER.format_content_block(
        TextContent(type="text", text=text)
    )
    via_value = DEFAULT_TOOL_RESULT_FORMATTER.format_value(text)
    assert via_block == via_value == text
