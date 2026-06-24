"""Batch-42 coverage for model readout and terminal decode edge branches."""

from __future__ import annotations

import json

from omnigent.repl._repl import (
    _build_model_readout_lines,
    _decode_terminal_target_key,
    _parse_terminal_tool_output,
    _terminal_target_key,
)
from omnigent.repl._repl import _TerminalInfo


def test_build_model_readout_lines_shows_no_model_pinned_for_key_provider() -> None:
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key_ref": "env:ANTHROPIC_API_KEY",
                },
            }
        }
    }
    lines = _build_model_readout_lines(config, "claude-sdk", None)
    assert any("no model pinned" in line for line in lines)


def test_decode_terminal_target_key_rejects_malformed_keys() -> None:
    info = _TerminalInfo(
        name="bash",
        session="s1",
        socket="/tmp/sock",
        target="main",
        conv_id="conv_a",
    )
    valid = _terminal_target_key(info)
    assert _decode_terminal_target_key(valid) == ("conv_a", "bash", "s1")
    assert _decode_terminal_target_key("terminal::only-two") is None
    assert _decode_terminal_target_key("main") is None


def test_parse_terminal_tool_output_skips_invalid_mcp_parts() -> None:
    wrapped = json.dumps(
        [
            {"type": "image"},
            {"type": "text", "text": "not-json"},
            {"type": "text", "text": json.dumps({"status": "closed"})},
        ]
    )
    assert _parse_terminal_tool_output(wrapped) == {"status": "closed"}
    assert _parse_terminal_tool_output(json.dumps([{"type": "text", "text": "{bad"}])) is None