"""Batch-32 coverage for sessions adapter stream pump and session lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent.server.schemas import SessionStatusEvent
from omnigent_client import OmnigentError


@dataclass
class _StubSession:
    id: str
    agent_id: str
    runner_id: str | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    agent_name: str | None = "test-agent"
    llm_model: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    harness: str | None = "claude-sdk"
    status: str = "idle"


def _adapter(**kwargs: object) -> _SessionsChatReplAdapter:
    client = MagicMock()
    defaults: dict[str, object] = {
        "client": client,
        "agent_name": "test-agent",
        "session_bundle": b"bundle",
        "runner_id": "runner_1",
    }
    defaults.update(kwargs)
    return _SessionsChatReplAdapter(**defaults)  # type: ignore[arg-type]


def _status_event(status: str = "idle") -> SessionStatusEvent:
    return SessionStatusEvent(
        type="session.status",
        conversation_id="conv_1",
        status=status,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_recover_runner_if_needed_noop_without_callback() -> None:
    adapter = _adapter(runner_recover=None)
    adapter._runner_id = "runner_old"
    await adapter._recover_runner_if_needed()
    assert adapter._runner_id == "runner_old"


@pytest.mark.asyncio
async def test_runner_recover_watch_binds_after_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(runner_recover=lambda: "runner_1")
    adapter._session_id = "conv_1"
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]

    calls = 0

    async def _sleep_then_cancel(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await adapter._runner_recover_watch()

    adapter._recover_runner_if_needed.assert_awaited()
    adapter._bind_runner_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_recover_watch_emits_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter(runner_recover=lambda: "runner_1")
    adapter._session_id = "conv_1"
    events: list[object] = []
    adapter._on_event = events.append
    adapter._recover_runner_if_needed = AsyncMock(  # type: ignore[method-assign]
        side_effect=OmnigentError("bind failed", code="conflict", status_code=409),
    )

    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    calls = 0

    async def _sleep_then_cancel(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await adapter._runner_recover_watch()

    assert len(events) == 1
    captured = capsys.readouterr()
    assert "runner recovery watchdog failed" in captured.err


@pytest.mark.asyncio
async def test_ensure_session_returns_immediately_when_pump_running() -> None:
    adapter = _adapter(session_id="conv_existing")
    adapter._stream_task = asyncio.create_task(asyncio.sleep(60))
    adapter._client.sessions.create = AsyncMock()

    result = await adapter._ensure_session()

    assert result == "conv_existing"
    adapter._client.sessions.create.assert_not_called()
    adapter._stream_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_task


@pytest.mark.asyncio
async def test_ensure_session_requires_bundle_for_fresh_session() -> None:
    adapter = _adapter(session_id=None, session_bundle=None)
    with pytest.raises(RuntimeError, match="local agent bundle"):
        await adapter._ensure_session()


@pytest.mark.asyncio
async def test_ensure_session_resume_path_hydrates_existing_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter(session_id="conv_resume")
    resumed = _StubSession(
        id="conv_resume",
        agent_id="ag_resume",
        model_override="claude-sonnet",
        harness="codex",
    )
    adapter._client.sessions.get = AsyncMock(return_value=resumed)
    adapter._client.sessions.create = AsyncMock()
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]

    real_pump = adapter._stream_pump

    async def _pump_once() -> None:
        await asyncio.sleep(3600)

    adapter._stream_pump = _pump_once  # type: ignore[method-assign]

    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")
    session_id = await adapter._ensure_session()

    assert session_id == "conv_resume"
    adapter._client.sessions.create.assert_not_called()
    adapter._client.sessions.get.assert_awaited_once_with("conv_resume")
    assert adapter.model_override == "claude-sonnet"
    assert adapter.harness == "codex"
    assert adapter._stream_task is not None
    captured = capsys.readouterr()
    assert "resuming existing session" in captured.err

    adapter._stream_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_task
    # Restore for type checkers; not used again in this test.
    adapter._stream_pump = real_pump  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ensure_session_spawns_recover_watch_when_callback_present() -> None:
    adapter = _adapter(runner_recover=lambda: "runner_1")
    adapter._client.sessions.create = AsyncMock(
        return_value=_StubSession(id="conv_new", agent_id="ag_new"),
    )
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock()  # type: ignore[method-assign]

    await adapter._ensure_session()

    assert adapter._recover_task is not None
    adapter._recover_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await adapter._recover_task


@pytest.mark.asyncio
async def test_ensure_session_debug_logs_create_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter()
    adapter._client.sessions.create = AsyncMock(
        return_value=_StubSession(id="conv_dbg", agent_id="ag_dbg"),
    )
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock()  # type: ignore[method-assign]

    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")
    await adapter._ensure_session()

    captured = capsys.readouterr()
    assert "POST /v1/sessions multipart bundle" in captured.err
    assert "session created id='conv_dbg'" in captured.err


@pytest.mark.asyncio
async def test_bind_runner_if_needed_raises_without_session() -> None:
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="before a session exists"):
        await adapter._bind_runner_if_needed()


@pytest.mark.asyncio
async def test_bind_runner_if_needed_raises_without_runner_id() -> None:
    adapter = _adapter(runner_id=None)
    adapter._session_id = "conv_1"
    with pytest.raises(RuntimeError, match="registered runner id"):
        await adapter._bind_runner_if_needed()


@pytest.mark.asyncio
async def test_bind_runner_if_needed_debug_logs_patch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter()
    adapter._session_id = "conv_1"
    adapter._client.sessions.bind_runner = AsyncMock(
        return_value=_StubSession(id="conv_1", agent_id="ag_1", runner_id="runner_1"),
    )

    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")
    await adapter._bind_runner_if_needed()

    captured = capsys.readouterr()
    assert "PATCH /v1/sessions/conv_1" in captured.err
    assert "runner bound id='runner_1'" in captured.err


def test_emit_runner_recovery_error_noop_without_callback() -> None:
    adapter = _adapter()
    adapter._on_event = None
    adapter._emit_runner_recovery_error_once(RuntimeError("ignored"))


@pytest.mark.asyncio
async def test_stream_pump_forwards_events_and_sets_turn_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(session_id="conv_1")
    received: list[object] = []
    adapter._on_event = received.append
    adapter._turn_done = asyncio.Event()

    async def _stream(_session_id: str):
        yield _status_event("idle")

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_pump()

    assert adapter._turn_done.is_set()
    assert len(received) == 1


@pytest.mark.asyncio
async def test_stream_pump_reconnects_after_clean_close(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter(session_id="conv_1")
    subscriptions = 0

    async def _stream(_session_id: str):
        nonlocal subscriptions
        subscriptions += 1
        if subscriptions == 1:
            return
        yield _status_event("running")

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_pump()

    assert subscriptions >= 2
    captured = capsys.readouterr()
    assert "subscribing /stream conv_1" in captured.err


@pytest.mark.asyncio
async def test_stream_pump_demotes_recoverable_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(session_id="conv_1")
    attempts = 0

    async def _stream(_session_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError("peer closed connection")
        await asyncio.sleep(3600)
        yield _status_event()

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with patch("omnigent.repl._repl._log") as mock_log:
        with pytest.raises(asyncio.CancelledError):
            await adapter._stream_pump()

    mock_log.info.assert_called()
    mock_log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_stream_pump_warns_on_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(session_id="conv_1")
    attempts = 0

    async def _stream(_session_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("bad json")
        await asyncio.sleep(3600)
        yield _status_event()

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with patch("omnigent.repl._repl._log") as mock_log:
        with pytest.raises(asyncio.CancelledError):
            await adapter._stream_pump()

    mock_log.warning.assert_called()


@pytest.mark.asyncio
async def test_stream_pump_recovers_runner_after_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter(session_id="conv_1", runner_recover=lambda: "runner_new")
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    attempts = 0

    async def _stream(_session_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("socket reset")
        await asyncio.sleep(3600)
        yield _status_event()

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_pump()

    adapter._bind_runner_if_needed.assert_awaited()
    assert adapter._runner_id == "runner_new"


@pytest.mark.asyncio
async def test_stream_pump_emits_recovery_error_when_rebind_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter(session_id="conv_1", runner_recover=lambda: "runner_1")
    events: list[object] = []
    adapter._on_event = events.append
    adapter._bind_runner_if_needed = AsyncMock(  # type: ignore[method-assign]
        side_effect=OmnigentError("runner gone", code="runner_unavailable", status_code=409),
    )

    async def _stream(_session_id: str):
        raise RuntimeError("disconnect")
        yield  # pragma: no cover

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_pump()

    assert len(events) == 1
    captured = capsys.readouterr()
    assert "runner recover after stream error failed" in captured.err


@pytest.mark.asyncio
async def test_unbind_runner_soft_debug_logs_legacy_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter()
    adapter._client.sessions.unbind_runner = AsyncMock(
        side_effect=OmnigentError(
            "runner_id must not be empty",
            code="invalid_input",
            status_code=400,
        ),
    )
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    await adapter._unbind_runner_soft("conv_old")

    captured = capsys.readouterr()
    assert "unbind_runner not supported by server" in captured.err


@pytest.mark.asyncio
async def test_unbind_runner_soft_reraises_other_errors() -> None:
    adapter = _adapter()
    adapter._client.sessions.unbind_runner = AsyncMock(
        side_effect=OmnigentError("server exploded", code="internal", status_code=500),
    )
    with pytest.raises(OmnigentError, match="server exploded"):
        await adapter._unbind_runner_soft("conv_old")


@pytest.mark.asyncio
async def test_start_new_conversation_cancels_pending_local_tasks() -> None:
    adapter = _adapter(session_id="conv_old")
    adapter._client.sessions.unbind_runner = AsyncMock()
    adapter._stream_task = asyncio.create_task(asyncio.sleep(60))
    local_task = asyncio.create_task(asyncio.sleep(60))
    adapter._pending_local_tasks["call_1"] = local_task

    await adapter.start_new_conversation()

    assert adapter._pending_local_tasks == {}
    with pytest.raises(asyncio.CancelledError):
        await local_task