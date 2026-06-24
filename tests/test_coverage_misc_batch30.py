"""Batch-30 coverage for _repl elicitation hook and event translation gaps."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from omnigent_client import ElicitationRequestCtx
from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME

from omnigent.repl._repl import (
    _ApprovalState,
    _ApprovalVerdict,
    _build_elicitation_content_from_schema,
    _load_startup_theme,
    _make_elicitation_prompt,
    _server_event_to_sdk_event,
)


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)


class _CaptureFmt:
    warning = "yellow"
    muted = "dim"
    accent = "cyan"


def _ctx(
    *,
    message: str = "",
    content_preview: str = "",
    mode: str = "form",
    url: str | None = None,
    policy_name: str = "gatekeeper",
    phase: str = "tool_call",
) -> ElicitationRequestCtx:
    return ElicitationRequestCtx(
        elicitation_id="elicit_test",
        message=message,
        requested_schema={},
        mode=mode,
        phase=phase,
        policy_name=policy_name,
        content_preview=content_preview,
        response_id="resp_test",
        url=url,
    )


def test_load_startup_theme_falls_back_when_get_theme_raises_value_error(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_path = tmp_path / ".omnigent" / "config.yaml"  # type: ignore[operator]
    config_path.parent.mkdir()
    config_path.write_text("tui:\n  theme: dark\n", encoding="utf-8")
    monkeypatch.setattr(
        "omnigent.repl._repl.get_theme",
        MagicMock(side_effect=ValueError("invalid theme")),
    )
    assert _load_startup_theme() is LIGHT_THEME


def test_build_elicitation_content_from_schema_delegates_to_tool_helper() -> None:
    schema = {
        "type": "object",
        "properties": {"approved": {"type": "boolean", "default": True}},
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result == {"approved": True}


def test_approval_state_resolve_verdict_returns_false_when_nothing_pending() -> None:
    state = _ApprovalState()
    assert state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE) is False


@pytest.mark.asyncio
async def test_elicitation_hook_auto_approves_pre_cached_pair() -> None:
    host = _CaptureHost()
    state = _ApprovalState()
    state.remember_always("cached_policy", "request")
    hook = _make_elicitation_prompt(host, _CaptureFmt(), state)
    approved = await hook(_ctx(policy_name="cached_policy", phase="request"))
    assert approved is True
    rendered = " ".join(getattr(o, "plain", str(o)) for o in host.outputs)
    assert "auto-approved" in rendered
    assert "approval required" not in rendered


@pytest.mark.asyncio
async def test_elicitation_hook_renders_reason_and_truncated_preview() -> None:
    host = _CaptureHost()
    state = _ApprovalState()
    hook = _make_elicitation_prompt(host, _CaptureFmt(), state)
    long_preview = "x" * 250
    task = asyncio.create_task(
        hook(
            _ctx(
                message="needs review",
                content_preview=long_preview,
            ),
        ),
    )
    await asyncio.sleep(0)
    rendered = " ".join(getattr(o, "plain", str(o)) for o in host.outputs)
    assert "approval required" in rendered
    assert "reason: needs review" in rendered
    assert "x" * 200 in rendered
    assert "x" * 201 not in rendered
    state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE)
    assert await task is True


@pytest.mark.asyncio
async def test_elicitation_hook_external_url_mode_blocks_keyboard_approval() -> None:
    host = _CaptureHost()
    state = _ApprovalState()
    hook = _make_elicitation_prompt(
        host,
        _CaptureFmt(),
        state,
        server_url="https://omnigent.example.com",
    )
    task = asyncio.create_task(
        hook(
            _ctx(
                mode="url",
                url="https://oauth.example.com/consent",
            ),
        ),
    )
    await asyncio.sleep(0)
    rendered = " ".join(getattr(o, "plain", str(o)) for o in host.outputs)
    assert "approve:" in rendered
    assert "y = approve once" not in rendered
    assert state.pending is True
    state.resolve_verdict(_ApprovalVerdict.REFUSE)
    assert await task is False


@pytest.mark.asyncio
async def test_elicitation_hook_internal_approve_url_uses_keyboard_prompt() -> None:
    host = _CaptureHost()
    state = _ApprovalState()
    hook = _make_elicitation_prompt(
        host,
        _CaptureFmt(),
        state,
        server_url="https://omnigent.example.com",
    )
    task = asyncio.create_task(
        hook(
            _ctx(
                mode="url",
                url="/approve/elicit_test",
            ),
        ),
    )
    await asyncio.sleep(0)
    rendered = " ".join(getattr(o, "plain", str(o)) for o in host.outputs)
    assert "y = approve once" in rendered
    state.cancel()
    assert await task is False


def test_server_event_to_sdk_event_translates_compaction_events() -> None:
    from omnigent_client._events import CompactionCompleted, CompactionInProgress
    from omnigent.server.schemas import CompactionCompletedEvent, CompactionInProgressEvent

    in_progress = CompactionInProgressEvent(type="response.compaction.in_progress")
    completed = CompactionCompletedEvent(type="response.compaction.completed")
    assert isinstance(_server_event_to_sdk_event(in_progress), CompactionInProgress)
    assert isinstance(_server_event_to_sdk_event(completed), CompactionCompleted)