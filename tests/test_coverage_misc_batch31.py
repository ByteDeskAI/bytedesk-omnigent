"""Batch-31 coverage for _SessionsChatReplAdapter helper paths."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent_client import OmnigentError


@dataclass
class _StubSession:
    id: str
    agent_id: str
    runner_id: str | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    agent_name: str | None = "test-agent"
    llm_model: str | None = "anthropic/claude-sonnet"
    context_window: int | None = 200_000
    last_total_tokens: int | None = 42
    harness: str | None = "claude-sdk"


def _adapter(**kwargs: object) -> _SessionsChatReplAdapter:
    client = MagicMock()
    defaults = {
        "client": client,
        "agent_name": "test-agent",
        "session_bundle": b"bundle",
        "runner_id": "runner_1",
    }
    defaults.update(kwargs)
    return _SessionsChatReplAdapter(**defaults)  # type: ignore[arg-type]


def test_adapter_property_defaults_before_first_send() -> None:
    adapter = _adapter(session_id="conv_existing", harness="claude-sdk")
    assert adapter.session_id == "conv_existing"
    assert adapter.model == "test-agent"
    assert adapter.current_response_id is None
    assert adapter.is_streaming is False
    assert adapter.reasoning_effort is None
    assert adapter.model_override is None
    assert adapter.llm_model is None
    assert adapter.harness == "claude-sdk"
    assert adapter.context_window is None


@pytest.mark.asyncio
async def test_set_model_override_rejects_blank_string() -> None:
    adapter = _adapter()
    with pytest.raises(ValueError, match="non-empty"):
        await adapter.set_model_override("   ")


@pytest.mark.asyncio
async def test_set_model_override_caches_before_session_exists() -> None:
    adapter = _adapter()
    await adapter.set_model_override("claude-opus-4-7")
    assert adapter.model_override == "claude-opus-4-7"
    adapter._client.sessions.set_model_override.assert_not_called()


@pytest.mark.asyncio
async def test_set_model_override_patches_existing_session() -> None:
    adapter = _adapter()
    adapter._session_id = "conv_1"
    adapter._client.sessions.set_model_override = AsyncMock(
        return_value=_StubSession(
            id="conv_1",
            agent_id="ag_1",
            model_override="claude-opus-4-7",
        ),
    )
    await adapter.set_model_override("claude-opus-4-7")
    adapter._client.sessions.set_model_override.assert_awaited_once_with(
        "conv_1",
        model_override="claude-opus-4-7",
    )
    assert adapter.model_override == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_set_reasoning_effort_caches_and_patches_session() -> None:
    adapter = _adapter()
    await adapter.set_reasoning_effort("high")
    assert adapter.reasoning_effort == "high"

    adapter._session_id = "conv_1"
    adapter._client.sessions.set_reasoning_effort = AsyncMock(
        return_value=_StubSession(
            id="conv_1",
            agent_id="ag_1",
            reasoning_effort="low",
        ),
    )
    await adapter.set_reasoning_effort("low")
    assert adapter.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_compact_raises_when_no_active_session() -> None:
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="No active conversation"):
        await adapter.compact()


@pytest.mark.asyncio
async def test_compact_posts_to_sessions_api() -> None:
    adapter = _adapter()
    adapter._session_id = "conv_1"
    adapter._client.sessions.compact = AsyncMock(return_value=None)
    await adapter.compact()
    adapter._client.sessions.compact.assert_awaited_once_with("conv_1")


@pytest.mark.asyncio
async def test_recover_runner_if_needed_updates_runner_id() -> None:
    adapter = _adapter(runner_recover=lambda: "runner_new")
    adapter._runner_id = "runner_old"
    await adapter._recover_runner_if_needed()
    assert adapter._runner_id == "runner_new"
    assert adapter._bound_runner_id is None


@pytest.mark.asyncio
async def test_notify_session_start_invokes_callback_once() -> None:
    seen: list[str] = []
    adapter = _adapter(on_session_start=seen.append)
    adapter._session_id = "conv_1"
    adapter._notify_session_start_once()
    adapter._notify_session_start_once()
    assert seen == ["conv_1"]


@pytest.mark.asyncio
async def test_bind_runner_if_needed_skips_attach_only_clients() -> None:
    adapter = _adapter(attach_only=True)
    adapter._session_id = "conv_1"
    adapter._client.sessions.bind_runner = AsyncMock()
    await adapter._bind_runner_if_needed()
    adapter._client.sessions.bind_runner.assert_not_called()


@pytest.mark.asyncio
async def test_bind_runner_if_needed_skips_when_already_bound() -> None:
    adapter = _adapter()
    adapter._session_id = "conv_1"
    adapter._bound_runner_id = "runner_1"
    adapter._client.sessions.bind_runner = AsyncMock()
    await adapter._bind_runner_if_needed()
    adapter._client.sessions.bind_runner.assert_not_called()


@pytest.mark.asyncio
async def test_bind_runner_if_needed_patches_runner_and_hydrates() -> None:
    adapter = _adapter()
    adapter._session_id = "conv_1"
    adapter._client.sessions.bind_runner = AsyncMock(
        return_value=_StubSession(
            id="conv_1",
            agent_id="ag_1",
            runner_id="runner_1",
            harness="codex",
        ),
    )
    await adapter._bind_runner_if_needed()
    assert adapter._bound_runner_id == "runner_1"
    assert adapter.harness == "codex"


def test_runner_recovery_error_message_terminal_vs_transient() -> None:
    adapter = _adapter()
    terminal = OmnigentError("runner gone", code="runner_unavailable", status_code=409)
    transient = RuntimeError("connection reset")
    assert "Runner recovery failed" in adapter._runner_recovery_error_message(terminal)
    assert "transient error" in adapter._runner_recovery_error_message(transient)


def test_emit_runner_recovery_error_once_dedupes_repeated_failures() -> None:
    adapter = _adapter()
    events: list[object] = []
    adapter._on_event = events.append
    exc = OmnigentError("bad", code="conflict", status_code=409)
    adapter._emit_runner_recovery_error_once(exc)
    adapter._emit_runner_recovery_error_once(exc)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_legacy_hooks_reset_resume_and_switch_session() -> None:
    adapter = _adapter(session_id="conv_old")
    stream_task = asyncio.create_task(asyncio.sleep(60))
    adapter._stream_task = stream_task
    adapter.reset()
    adapter.resume_from_response("resp_ignored")
    adapter.switch_session("conv_new")
    assert adapter.session_id == "conv_new"
    assert adapter._stream_task is None
    assert adapter._bound_runner_id is None
    with pytest.raises(asyncio.CancelledError):
        await stream_task


@pytest.mark.asyncio
async def test_start_new_conversation_clears_local_state() -> None:
    adapter = _adapter(session_id="conv_old")
    adapter._current_response_id = "resp_1"
    adapter._is_streaming = True
    adapter._bound_runner_id = "runner_1"
    adapter._client.sessions.unbind_runner = AsyncMock()
    adapter._stream_task = asyncio.create_task(asyncio.sleep(60))
    await adapter.start_new_conversation()
    assert adapter.session_id is None
    assert adapter.current_response_id is None
    assert adapter.is_streaming is False
    assert adapter._bound_runner_id is None


@pytest.mark.asyncio
async def test_unbind_runner_soft_ignores_legacy_server_rejection() -> None:
    adapter = _adapter()
    adapter._client.sessions.unbind_runner = AsyncMock(
        side_effect=OmnigentError(
            "runner_id must not be empty",
            code="invalid_input",
            status_code=400,
        ),
    )
    await adapter._unbind_runner_soft("conv_old")


@pytest.mark.asyncio
async def test_switch_to_session_rebinds_and_restarts_pump() -> None:
    adapter = _adapter()
    adapter._session_id = "conv_old"
    adapter._stream_task = asyncio.create_task(asyncio.sleep(60))
    adapter._client.sessions.get = AsyncMock(
        return_value=_StubSession(id="conv_new", agent_id="ag_new", runner_id=None),
    )
    adapter._client.sessions.unbind_runner = AsyncMock()
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock()  # type: ignore[method-assign]

    result = await adapter.switch_to_session("conv_new")
    assert result == "conv_new"
    assert adapter.session_id == "conv_new"
    adapter._bind_runner_if_needed.assert_awaited_once()
    assert adapter._stream_task is not None


@pytest.mark.asyncio
async def test_aclose_cancels_stream_recover_and_local_tasks() -> None:
    adapter = _adapter()
    adapter._stream_task = asyncio.create_task(asyncio.sleep(60))
    adapter._recover_task = asyncio.create_task(asyncio.sleep(60))
    local_task = asyncio.create_task(asyncio.sleep(60))
    adapter._pending_local_tasks["call_1"] = local_task
    await adapter.aclose()
    assert adapter._stream_task is None
    assert adapter._recover_task is None
    assert adapter._pending_local_tasks == {}