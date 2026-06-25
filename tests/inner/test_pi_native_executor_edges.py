"""Edge tests for PiNativeExecutor bridge queueing."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.pi_native_executor import (
    PiNativeExecutor,
    _bridge_dir_from_env,
    _content_to_text,
    _latest_user_text,
    _request_session_id_from_env,
)
from omnigent.pi_native_bridge import (
    PI_NATIVE_BRIDGE_DIR_ENV_VAR,
    PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
)


@pytest.fixture
def bridge_dir(tmp_path: Path) -> Path:
    path = tmp_path / "pi-bridge"
    path.mkdir()
    return path


def test_bridge_dir_from_env_requires_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PI_NATIVE_BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match=PI_NATIVE_BRIDGE_DIR_ENV_VAR):
        _bridge_dir_from_env()


def test_request_session_id_from_env_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR, raising=False)
    assert _request_session_id_from_env() is None


def test_content_to_text_handles_text_blocks_and_none(bridge_dir: Path) -> None:
    assert _content_to_text("hello", bridge_dir) == "hello"
    assert _content_to_text(None, bridge_dir) == ""
    assert _content_to_text([{"type": "input_text", "text": "hi"}], bridge_dir) == "hi"
    assert _content_to_text(42, bridge_dir) == "42"


def test_latest_user_text_skips_assistant_messages(bridge_dir: Path) -> None:
    messages = [
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": "send me"},
    ]
    assert _latest_user_text(messages, bridge_dir) == "send me"


@pytest.mark.asyncio
async def test_enqueue_session_message_returns_false_for_empty_text(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = PiNativeExecutor(bridge_dir=bridge_dir)
    assert await ex.enqueue_session_message("key", "") is False


@pytest.mark.asyncio
async def test_run_turn_yields_error_without_user_text(bridge_dir: Path) -> None:
    ex = PiNativeExecutor(bridge_dir=bridge_dir)
    events = [event async for event in ex.run_turn([], [], "persona")]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_run_turn_queues_latest_user_message(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "omnigent.inner.pi_native_executor.enqueue_user_message",
        lambda _dir, text: queued.append(text),
    )
    ex = PiNativeExecutor(bridge_dir=bridge_dir)
    events = [
        event
        async for event in ex.run_turn(
            [{"role": "user", "content": "ship it"}],
            [],
            "persona",
        )
    ]
    assert queued == ["ship it"]
    assert isinstance(events[-1], TurnComplete)


def test_executor_reads_env_when_bridge_dir_not_passed(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PI_NATIVE_BRIDGE_DIR_ENV_VAR, str(bridge_dir))
    monkeypatch.setenv(PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "conv_pi")
    ex = PiNativeExecutor()
    assert ex._bridge_dir == bridge_dir
    assert ex._request_session_id == "conv_pi"
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True
