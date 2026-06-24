"""Batch-29 coverage for small _repl.py helper branches."""

from __future__ import annotations

import builtins
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from omnigent_client import BlockContext, ResponseEndBlock, ResponseStartBlock
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME

from omnigent.repl._repl import (
    TimedFormatter,
    _ApprovalState,
    _ApprovalVerdict,
    _StartupHeader,
    _build_startup_header,
    _display_cwd,
    _is_recoverable_sse_transport_error,
    _load_startup_theme,
    _parse_approval_input,
    _render_startup_banner_ansi,
    _summarize_description,
)


def test_is_recoverable_sse_transport_error_returns_false_when_httpx_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name in {"httpx", "httpcore"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    assert _is_recoverable_sse_transport_error(RuntimeError("boom")) is False


def test_load_startup_theme_falls_back_when_get_theme_rejects_persisted_value(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_path = tmp_path / ".omnigent" / "config.yaml"  # type: ignore[operator]
    config_path.parent.mkdir()
    config_path.write_text("tui:\n  theme: dark\n", encoding="utf-8")
    monkeypatch.setattr(
        "omnigent_ui_sdk.terminal._config.get_theme",
        MagicMock(side_effect=ValueError("invalid theme")),
    )
    assert _load_startup_theme() is LIGHT_THEME


def test_display_cwd_collapses_home_and_subpaths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.getcwd", lambda: "/srv/project")
    assert _display_cwd() == "/srv/project"

    monkeypatch.setattr("os.getcwd", lambda: "/home/tester")
    monkeypatch.setattr("os.path.expanduser", lambda _path: "/home/tester")
    assert _display_cwd() == "~"

    monkeypatch.setattr("os.getcwd", lambda: "/home/tester/repos/omnigent")
    assert _display_cwd() == "~/repos/omnigent"


def test_summarize_description_returns_none_for_whitespace_only() -> None:
    assert _summarize_description("   \n\t  ") is None


def test_render_startup_banner_shows_credential_only_row() -> None:
    header = _StartupHeader(
        folder="~/wd",
        description=None,
        model_label=None,
        credential="Subscription",
        creds_line=None,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", _render_startup_banner_ansi("agent", header=header))
    assert "Subscription" in plain
    assert "claude" not in plain.lower()


def test_render_startup_banner_shows_model_only_row() -> None:
    header = _StartupHeader(
        folder="~/wd",
        description=None,
        model_label="claude-sonnet-4-6",
        credential=None,
        creds_line=None,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", _render_startup_banner_ansi("agent", header=header))
    assert "claude-sonnet-4-6" in plain


def test_render_startup_banner_includes_remote_server_url_row() -> None:
    header = _StartupHeader(
        folder="~/wd",
        description=None,
        model_label=None,
        credential=None,
        creds_line=None,
    )
    remote = "https://omnigent.example.com"
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        _render_startup_banner_ansi("agent", server_url=remote, header=header),
    )
    assert remote in plain


def test_timed_formatter_appends_elapsed_seconds_on_response_end() -> None:
    fmt = TimedFormatter(theme=LIGHT_THEME)
    start = ResponseStartBlock(model="agent", response_id="r1", ctx=BlockContext(timestamp=10.0))
    end = ResponseEndBlock(status="completed", ctx=BlockContext(timestamp=12.5))
    fmt.format_response_start(start)
    items = fmt.format_response_end(end)
    rendered = "".join(str(item) for item in items)
    assert "2.5s" in rendered


def test_timed_formatter_skips_elapsed_when_start_missing() -> None:
    fmt = TimedFormatter(theme=LIGHT_THEME)
    end = ResponseEndBlock(status="completed", ctx=BlockContext(timestamp=12.5))
    items = fmt.format_response_end(end)
    rendered = "".join(str(item) for item in items)
    assert "s" not in rendered or "12.5s" not in rendered


def test_parse_approval_input_recognizes_always_and_once_tokens() -> None:
    assert _parse_approval_input("A") == _ApprovalVerdict.APPROVE_ALWAYS
    assert _parse_approval_input("yes") == _ApprovalVerdict.APPROVE_ONCE
    assert _parse_approval_input("nope") == _ApprovalVerdict.REFUSE


@pytest.mark.asyncio
async def test_approval_state_tracks_pending_always_and_cancel() -> None:
    state = _ApprovalState()
    assert not state.pending
    assert not state.is_pre_approved("policy", "request")

    future = state.begin("policy", "request")
    assert state.pending
    assert state.is_pre_approved("policy", "request") is False

    assert state.resolve_verdict(_ApprovalVerdict.APPROVE_ALWAYS) is True
    assert future.result() is True
    assert state.is_pre_approved("policy", "request") is True

    stale = state.begin("other", "tool_call")
    state.cancel()
    assert stale.result() is False
    assert not state.pending


@pytest.mark.asyncio
async def test_approval_state_refuses_stale_future_when_begin_replaces_pending() -> None:
    state = _ApprovalState()
    first = state.begin("policy", "request")
    second = state.begin("policy", "tool_call")
    assert first.result() is False
    assert state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE) is True
    assert second.result() is True


def test_build_startup_header_marks_unconfigured_family(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.yaml").write_text("providers: {}\n", encoding="utf-8")  # type: ignore[operator]
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.surface_default_provider",
        lambda _config, _family: None,
    )
    header = _build_startup_header(
        "databricks_supervisor",
        "Supervisor agent.",
        ["anthropic", "openai"],
    )
    assert header.creds_line is not None
    assert "not configured" in header.creds_line