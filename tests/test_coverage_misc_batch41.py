"""Batch-41 coverage for remaining pure _repl.py helper branches."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest
from omnigent_ui_sdk import UserConfigError
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME
from prompt_toolkit.document import Document
from omnigent.spec.types import SkillSpec

from omnigent.repl._repl import (
    COMMANDS,
    _ApprovalVerdict,
    _SlashCommandCompleter,
    _StartupHeader,
    _extract_message_text,
    _header_glyph,
    _humanize_agent_name,
    _is_remote_server_url,
    _load_startup_theme,
    _parse_approval_input,
    _parse_terminal_tool_output,
    _render_history_item,
    _render_startup_banner_ansi,
    register_skill_commands,
    unregister_skill_commands,
)


class _StubHost:
    def __init__(self) -> None:
        self.output_calls: list[object] = []

    def output(self, item: object) -> None:
        self.output_calls.append(item)


def _completions_for(text: str) -> list[tuple[str, str, int]]:
    doc = Document(text=text, cursor_position=len(text))
    completer = _SlashCommandCompleter()
    return [
        (c.text, c.display_meta_text, c.start_position)
        for c in completer.get_completions(doc, complete_event=None)  # type: ignore[arg-type]
    ]


@pytest.mark.parametrize(
    ("wire", "display"),
    [
        ("resume_test", "resume test"),
        ("my-coding-agent", "my coding agent"),
        ("plain", "plain"),
    ],
)
def test_humanize_agent_name_replaces_separators(wire: str, display: str) -> None:
    assert _humanize_agent_name(wire) == display


def test_header_glyph_suppresses_subscription_ticket() -> None:
    from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND

    assert _header_glyph(SUBSCRIPTION_KIND) == ""
    assert _header_glyph("key") != ""


@pytest.mark.parametrize(
    ("url", "remote"),
    [
        (None, False),
        ("", False),
        ("http://127.0.0.1:6767", False),
        ("http://localhost:6767", False),
        ("http://[::1]:6767", False),
        ("https://omnigent.example.com", True),
        ("http://8.8.8.8", True),
    ],
)
def test_is_remote_server_url(url: str | None, remote: bool) -> None:
    assert _is_remote_server_url(url) is remote


def test_extract_message_text_returns_empty_for_non_list_content() -> None:
    assert _extract_message_text({"content": "not-a-list"}) == ""


def test_render_history_item_slash_command_includes_output_line() -> None:
    host = _StubHost()
    _render_history_item(
        {
            "type": "slash_command",
            "name": "grill-me",
            "arguments": "review plan",
            "output": "Loaded skill instructions.",
        },
        host,  # type: ignore[arg-type]
    )
    rendered = "\n".join(str(item) for item in host.output_calls)
    assert "/grill-me review plan" in rendered
    assert "Loaded skill instructions." in rendered


def test_parse_terminal_tool_output_decodes_mcp_content_parts_wrapper() -> None:
    inner = {"terminal": "bash", "session": "s1", "status": "launched"}
    wrapped = json.dumps([{"type": "text", "text": json.dumps(inner)}])
    assert _parse_terminal_tool_output(wrapped) == inner


def test_register_skill_commands_skips_builtin_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    skill = SkillSpec(name="cancel", description="collides with /cancel", content="x")
    registered = register_skill_commands([skill])
    try:
        assert registered == []
        assert any("collides" in record.message for record in caplog.records)
    finally:
        unregister_skill_commands(registered)


def test_completer_ignores_path_like_and_post_space_tokens() -> None:
    assert _completions_for("/Users/me/project") == []
    assert _completions_for("/help extra") == []


def test_load_startup_theme_falls_back_on_user_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl.load_user_config",
        MagicMock(side_effect=UserConfigError("corrupt config")),
    )
    assert _load_startup_theme() is LIGHT_THEME


def test_render_startup_banner_appends_multi_vendor_creds_line() -> None:
    header = _StartupHeader(
        folder="~/wd",
        description="Orchestrator",
        model_label="claude-sonnet-4-6",
        credential="Subscription",
        creds_line="Claude → Subscription   ·   Codex → 🔑 OpenAI API Key",
    )
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        _render_startup_banner_ansi("polly", header=header),
    )
    assert "spawn the following sub-agents" in plain
    assert "Codex" in plain


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("approve always", _ApprovalVerdict.APPROVE_ALWAYS),
        ("YES ALWAYS", _ApprovalVerdict.APPROVE_ALWAYS),
        ("ok", _ApprovalVerdict.APPROVE_ONCE),
        ("  approve  ", _ApprovalVerdict.APPROVE_ONCE),
        ("nah", _ApprovalVerdict.REFUSE),
    ],
)
def test_parse_approval_input_token_sets(text: str, verdict: _ApprovalVerdict) -> None:
    assert _parse_approval_input(text) == verdict


def test_completer_hides_alias_commands_from_popup() -> None:
    names = {name for name, _, _ in _completions_for("/")}
    assert "/help" in names
    assert "/?" not in names
    assert "/exit" not in names