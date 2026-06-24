"""Batch-35 coverage for slash-command helpers and model readout paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omnigent_ui_sdk.terminal import RichBlockFormatter

from omnigent.repl._repl import (
    COMMANDS,
    _build_model_readout_lines,
    _match_configured_provider,
    _model_readout_harness,
    _model_validation_warning,
    _resolve_provider_default_model,
    _session_readout_harness,
    handle_slash_command,
)


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)


class _StubFmt:
    muted = "dim"
    accent = "cyan"

    def welcome(self, name: str, hints: object) -> str:
        return f"<welcome:{name}>"


@dataclass
class _ModelSession:
    model: str = "test-agent"
    harness: str | None = "claude-sdk"
    model_override: str | None = None
    llm_model: str | None = "anthropic/claude-sonnet-4-6"
    is_streaming: bool = False

    async def set_model_override(self, model: str | None) -> None:
        self.model_override = model


def _rendered(host: _CaptureHost) -> str:
    return "\n".join(str(item) for item in host.outputs)


def _anthropic_key_provider(*, default: bool = True) -> dict[str, object]:
    entry: dict[str, object] = {
        "kind": "key",
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "api_key_ref": "env:ANTHROPIC_API_KEY",
            "models": {"default": "claude-sonnet-4-6"},
        },
    }
    if default:
        entry["default"] = True
    return entry


def _openai_key_provider(*, default: bool = False) -> dict[str, object]:
    entry: dict[str, object] = {
        "kind": "key",
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "models": {"default": "gpt-5.5"},
        },
    }
    if default:
        entry["default"] = True
    return entry


def _openrouter_gateway_provider() -> dict[str, object]:
    return {
        "kind": "gateway",
        "anthropic": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_ref": "env:OPENROUTER_API_KEY",
        },
    }


@pytest.mark.asyncio
async def test_help_command_lists_registered_commands() -> None:
    host = _CaptureHost()
    _, handler = COMMANDS["/help"]
    await handler("", _ModelSession(), MagicMock(), host, _StubFmt())  # type: ignore[arg-type]

    rendered = _rendered(host)
    assert "/model" in rendered
    assert "/theme" in rendered
    assert "/?" not in rendered
    assert "/exit" not in rendered


def test_model_readout_harness_infers_from_model_string() -> None:
    assert _model_readout_harness("openai/gpt-4.1") == "openai-agents"


def test_model_readout_harness_falls_back_to_claude_sdk() -> None:
    assert _model_readout_harness(None) == "claude-sdk"
    assert _model_readout_harness("not-a-known-model") == "claude-sdk"


def test_session_readout_harness_prefers_bound_harness() -> None:
    session = _ModelSession(harness="codex", llm_model="openai/gpt-4.1")
    assert _session_readout_harness(session) == "codex"  # type: ignore[arg-type]


def test_session_readout_harness_infers_when_harness_missing() -> None:
    session = _ModelSession(harness=None, llm_model="openai/gpt-4.1")
    assert _session_readout_harness(session) == "openai-agents"  # type: ignore[arg-type]


def test_build_model_readout_lines_without_configured_credential() -> None:
    lines = _build_model_readout_lines({}, "claude-sdk", None)
    assert lines[0].startswith("Active:  None")
    assert "omnigent setup" in lines[1]


def test_build_model_readout_lines_with_unresolved_override() -> None:
    lines = _build_model_readout_lines({}, "claude-sdk", "ghost-model")
    assert "ghost-model" in lines[0]
    assert "provider unresolved" in lines[0]


def test_build_model_readout_lines_labels_databricks_and_subscription() -> None:
    databricks_config = {
        "providers": {
            "workspace": {
                "kind": "databricks",
                "profile": "my-ws",
                "default": "anthropic",
            }
        }
    }
    databricks_lines = _build_model_readout_lines(databricks_config, "claude-sdk", None)
    assert "Databricks profile picks the model" in databricks_lines[0]

    subscription_config = {
        "providers": {
            "claude-subscription": {
                "kind": "subscription",
                "cli": "claude",
                "default": "anthropic",
            }
        }
    }
    subscription_lines = _build_model_readout_lines(subscription_config, "claude-sdk", None)
    assert "CLI login picks the model" in subscription_lines[0]


def test_build_model_readout_lines_lists_alternate_providers() -> None:
    config = {
        "providers": {
            "anthropic": _anthropic_key_provider(),
            "openrouter": _openrouter_gateway_provider(),
        }
    }
    lines = _build_model_readout_lines(config, "claude-sdk", None)
    assert any("Also configured:" in line for line in lines)
    assert any("omnigent setup --no-internal-beta" in line for line in lines)


def test_match_configured_provider_resolves_friendly_name() -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    assert _match_configured_provider(config, "Anthropic") == "anthropic"
    assert _match_configured_provider(config, "claude-opus-4-1") is None


def test_resolve_provider_default_model_returns_family_default() -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    resolved = _resolve_provider_default_model(config, "anthropic")
    assert resolved is not None
    assert "claude" in resolved


def test_model_validation_warning_for_unknown_prefix_and_catalog_miss() -> None:
    warning = _model_validation_warning("qwen/qwen3.7-plus")
    assert warning is not None
    assert "gateway" in warning

    catalog_warning = _model_validation_warning("anthropic/not-a-real-model-ever")
    assert catalog_warning is not None
    assert "catalog" in catalog_warning

    assert _model_validation_warning("anthropic/claude-sonnet-4-6") is None


@pytest.mark.asyncio
async def test_handle_slash_command_unknown_command_shows_help_hint() -> None:
    host = _CaptureHost()
    await handle_slash_command(
        "/not-a-real-command",
        _ModelSession(),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "Unknown command" in _rendered(host)
    assert "/help" in _rendered(host)


@pytest.mark.asyncio
async def test_model_command_show_readout(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    host = _CaptureHost()
    await handle_slash_command(
        "/model",
        _ModelSession(),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        RichBlockFormatter(),
    )
    assert "Active:" in _rendered(host)


@pytest.mark.asyncio
async def test_model_command_clear_resets_override() -> None:
    session = _ModelSession(model_override="claude-opus-4-7")
    host = _CaptureHost()
    await handle_slash_command(
        "/model default",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert session.model_override is None
    assert "model reset" in _rendered(host)


@pytest.mark.asyncio
async def test_model_command_rejects_cross_provider_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "providers": {
            "anthropic": _anthropic_key_provider(default=True),
            "openai": _openai_key_provider(default=False),
        }
    }
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    host = _CaptureHost()
    await handle_slash_command(
        "/model openai",
        _ModelSession(harness="claude-sdk"),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    rendered = _rendered(host)
    assert "Switching the active provider isn't supported" in rendered


@pytest.mark.asyncio
async def test_model_command_sets_override_with_catalog_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    session = _ModelSession(is_streaming=True)
    host = _CaptureHost()
    await handle_slash_command(
        "/model qwen/qwen3.7-plus",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    rendered = _rendered(host)
    assert session.model_override == "qwen/qwen3.7-plus"
    assert "note:" in rendered
    assert "current response unchanged" in rendered


@pytest.mark.asyncio
async def test_switch_command_lists_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    listed = MagicMock(
        id="conv_listed",
        title="Demo",
        status="idle",
        created_at=1_700_000_000,
    )
    client.sessions.list = AsyncMock(return_value=[listed])

    host = _CaptureHost()
    await handle_slash_command(
        "/switch",
        _ModelSession(),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    from rich.console import Console
    from rich.table import Table

    client.sessions.list.assert_awaited_once_with(limit=20)
    assert len(host.outputs) == 2
    table = host.outputs[0]
    assert isinstance(table, Table)
    console = Console(width=200, record=True)
    console.print(table)
    assert "conv_listed" in console.export_text()
    assert "/switch <#>" in _rendered(host)


@pytest.mark.asyncio
async def test_switch_command_resumes_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    listed = MagicMock(
        id="conv_target",
        title="Target",
        status="idle",
        created_at=1_700_000_000,
    )
    client.sessions.list = AsyncMock(return_value=[listed])

    session = _ModelSession()
    session.switch_to_session = AsyncMock()  # type: ignore[attr-defined]

    attach = AsyncMock()
    monkeypatch.setattr("omnigent.repl._repl._attach_to_conversation", attach)

    host = _CaptureHost()
    await handle_slash_command(
        "/switch 1",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    session.switch_to_session.assert_awaited_once_with("conv_target")  # type: ignore[attr-defined]
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_command_rejects_out_of_range_index() -> None:
    client = MagicMock()
    client.sessions.list = AsyncMock(return_value=[])

    host = _CaptureHost()
    await handle_slash_command(
        "/switch 9",
        _ModelSession(),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "No session #9" in _rendered(host)