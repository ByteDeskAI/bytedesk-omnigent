"""Edge tests for runner relay lifecycle helpers."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from omnigent.server.routes import sessions as sessions_mod


@pytest.fixture(autouse=True)
def _clear_relay_tasks() -> None:
    sessions_mod._runner_relay_tasks.clear()
    yield
    sessions_mod._runner_relay_tasks.clear()


def test_ensure_runner_relay_returns_none_without_runner_binding() -> None:
    client = MagicMock()
    assert sessions_mod._ensure_runner_relay("conv_skip", None, client) is None
    assert sessions_mod._ensure_runner_relay("conv_skip", "runner_1", None) is None
    assert sessions_mod._runner_relay_tasks == {}


def test_ensure_runner_relay_reuses_healthy_existing_handle() -> None:
    fake_task = MagicMock()
    fake_task.done.return_value = False
    existing = sessions_mod._RelayHandle(
        runner_id="runner_a",
        task=fake_task,
        ready=asyncio.Event(),
    )
    sessions_mod._runner_relay_tasks["conv_reuse"] = existing

    class _NoStreamClient:
        stream_calls = 0

        def stream(self, *_args, **_kwargs):
            type(self).stream_calls += 1
            raise AssertionError("stream should not be called when relay is reused")

    client = _NoStreamClient()
    handle = sessions_mod._ensure_runner_relay(
        "conv_reuse",
        "runner_a",
        client,  # type: ignore[arg-type]
    )

    assert handle is existing
    assert handle.task is fake_task
    assert client.stream_calls == 0
    fake_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_runner_relay_replaces_stale_runner_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_task = MagicMock()
    old_task.done.return_value = False

    async def _noop_relay(
        _session_id: str,
        _client: object,
        _store: object | None,
        ready: asyncio.Event,
    ) -> None:
        ready.set()

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _noop_relay)
    sessions_mod._runner_relay_tasks["conv_replace"] = sessions_mod._RelayHandle(
        runner_id="runner_old",
        task=old_task,
        ready=asyncio.Event(),
    )

    client = MagicMock()
    new_handle = sessions_mod._ensure_runner_relay(
        "conv_replace",
        "runner_new",
        client,
    )

    assert new_handle is not None
    assert new_handle.runner_id == "runner_new"
    assert new_handle.task is not old_task
    old_task.cancel.assert_called_once()
    await asyncio.wait_for(new_handle.ready.wait(), timeout=1.0)
    await asyncio.wait_for(new_handle.task, timeout=1.0)


@pytest.mark.asyncio
async def test_ensure_runner_relay_creates_new_handle_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop_relay(
        _session_id: str,
        _client: object,
        _store: object | None,
        ready: asyncio.Event,
    ) -> None:
        ready.set()

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _noop_relay)
    client = MagicMock()
    handle = sessions_mod._ensure_runner_relay("conv_new", "runner_new", client)

    assert handle is not None
    assert handle.runner_id == "runner_new"
    assert sessions_mod._runner_relay_tasks["conv_new"] is handle
    await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
    await asyncio.wait_for(handle.task, timeout=1.0)


@pytest.mark.asyncio
async def test_ensure_runner_relay_replaces_done_task_without_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_task = MagicMock()
    old_task.done.return_value = True

    async def _noop_relay(
        _session_id: str,
        _client: object,
        _store: object | None,
        ready: asyncio.Event,
    ) -> None:
        ready.set()

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _noop_relay)
    sessions_mod._runner_relay_tasks["conv_done_task"] = sessions_mod._RelayHandle(
        runner_id="runner_old",
        task=old_task,
        ready=asyncio.Event(),
    )

    new_handle = sessions_mod._ensure_runner_relay(
        "conv_done_task",
        "runner_new",
        MagicMock(),
    )

    assert new_handle is not None
    assert new_handle.runner_id == "runner_new"
    old_task.cancel.assert_not_called()
    await asyncio.wait_for(new_handle.task, timeout=1.0)


@pytest.mark.asyncio
async def test_ensure_runner_relay_done_callback_clears_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop_relay(
        _session_id: str,
        _client: object,
        _store: object | None,
        ready: asyncio.Event,
    ) -> None:
        ready.set()

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _noop_relay)
    handle = sessions_mod._ensure_runner_relay(
        "conv_clear",
        "runner_clear",
        MagicMock(),
    )
    assert handle is not None
    await asyncio.wait_for(handle.task, timeout=1.0)
    assert "conv_clear" not in sessions_mod._runner_relay_tasks


@pytest.mark.asyncio
async def test_ensure_runner_relay_ready_returns_none_without_runner() -> None:
    result = await sessions_mod._ensure_runner_relay_ready(
        "conv_none",
        None,
        None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_ensure_runner_relay_ready_returns_when_ready_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    ready.set()
    fake_handle = sessions_mod._RelayHandle(
        runner_id="runner_ready",
        task=MagicMock(),
        ready=ready,
    )
    monkeypatch.setattr(
        sessions_mod,
        "_ensure_runner_relay",
        lambda *_args, **_kwargs: fake_handle,
    )

    result = await sessions_mod._ensure_runner_relay_ready(
        "conv_ready",
        "runner_ready",
        MagicMock(),
    )

    assert result is fake_handle


@pytest.mark.asyncio
async def test_ensure_runner_relay_ready_raises_when_task_exits_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError

    async def _exit_without_ready(
        _session_id: str,
        _client: object,
        _store: object | None,
        _ready: asyncio.Event,
    ) -> None:
        return

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _exit_without_ready)
    monkeypatch.setattr(sessions_mod, "_RUNNER_RELAY_READY_TIMEOUT_S", 0.05)

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._ensure_runner_relay_ready(
            "conv_exit",
            "runner_exit",
            MagicMock(),
        )

    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "exited before becoming ready" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ensure_runner_relay_ready_raises_on_ready_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError

    async def _hang_without_ready(
        _session_id: str,
        _client: object,
        _store: object | None,
        _ready: asyncio.Event,
    ) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(sessions_mod, "_relay_runner_stream", _hang_without_ready)
    monkeypatch.setattr(sessions_mod, "_RUNNER_RELAY_READY_TIMEOUT_S", 0.05)

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._ensure_runner_relay_ready(
            "conv_timeout",
            "runner_timeout",
            MagicMock(),
        )

    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "Timed out waiting for runner stream relay" in str(exc_info.value)
    handle = sessions_mod._runner_relay_tasks.pop("conv_timeout", None)
    if handle is not None:
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await handle.task
