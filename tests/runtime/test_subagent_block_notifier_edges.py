"""Edge-case coverage for omnigent.runtime.subagent_block_notifier."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from omnigent.runtime import pending_elicitations, subagent_block_notifier
from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier, _WakeOutcome
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.runtime.test_subagent_block_notifier import (
    _CapturedWake,
    _RecordingDispatch,
    _instant_sleep,
    _request_event,
    _resolved_event,
    elicitation_armed,
)


@pytest_asyncio.fixture
async def conv_store(tmp_path: Path) -> AsyncIterator[SqlAlchemyConversationStore]:
    store = SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'test.db'}")
    yield store


@pytest.fixture(autouse=True)
def _reset_pending_elicitations() -> None:
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.mark.asyncio
async def test_sleep_helpers_delegate_to_asyncio_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await subagent_block_notifier._sleep(0.5)
    await subagent_block_notifier._escalation_sleep(120.0)
    assert slept == [0.5, 120.0]


@pytest.mark.asyncio
async def test_observe_ignores_elicitation_id_on_non_request_types(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events with an id but neither request nor resolved type are ignored."""
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:other", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    notifier.observe(
        child.id,
        {"type": "response.completed", "elicitation_id": "elicit_other"},
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_observe_releases_arm_when_event_loop_is_closed(
    conv_store: SqlAlchemyConversationStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduling onto a closed loop drops the wake and releases the debounce arm."""
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:closed-loop", parent_conversation_id=parent.id
    )
    loop = asyncio.new_event_loop()
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=loop,
    )
    loop.close()
    with caplog.at_level(logging.DEBUG, logger="omnigent.runtime.subagent_block_notifier"):
        notifier.observe(child.id, _request_event("elicit_closed"))
    assert elicitation_armed(notifier, "elicit_closed") is False
    assert any("loop unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_close_cancels_inflight_wake_futures(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan teardown cancels scheduled wake handlers without waiting."""
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:close", parent_conversation_id=parent.id
    )
    gate = asyncio.Event()

    async def _gated_escalation(_seconds: float) -> None:
        await gate.wait()

    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _gated_escalation)
    notifier.observe(child.id, _request_event("elicit_close"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert notifier._inflight  # noqa: SLF001 — handler parked in grace
    notifier.close()
    assert not notifier._inflight  # noqa: SLF001
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_discard_inflight_logs_unexpected_handler_exception(
    conv_store: SqlAlchemyConversationStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception inside the scheduled handler is logged, not propagated."""
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:boom", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    def _boom(_child: Any, _event: dict[str, Any]) -> str:
        raise ValueError("formatting failed")

    monkeypatch.setattr(subagent_block_notifier, "_format_block_notice", _boom)
    with caplog.at_level(logging.WARNING, logger="omnigent.runtime.subagent_block_notifier"):
        notifier.observe(child.id, _request_event("elicit_boom"))
        deadline = asyncio.get_event_loop().time() + 1.0
        while not any("wake handling raised unexpectedly" in r.getMessage() for r in caplog.records):
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0)
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_handle_request_no_op_for_invalid_elicitation_id(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct handle_request calls with malformed ids exit before any dispatch."""
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:bad", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    await notifier.handle_request(
        child.id,
        {"type": "response.elicitation_request", "elicitation_id": ""},
    )
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_handle_request_returns_when_arm_cleared_after_parent_lookup(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolve during parent lookup suppresses wake registration."""
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:lookup-race", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    with notifier._lock:
        notifier._notified.add("elicit_lookup")

    async def _clear_arm_during_lookup(fn: Any, *args: Any) -> Any:
        with notifier._lock:
            notifier._notified.discard("elicit_lookup")
        return fn(*args)

    monkeypatch.setattr(asyncio, "to_thread", _clear_arm_during_lookup)
    await notifier.handle_request(child.id, _request_event("elicit_lookup"))
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_deliver_with_retry_returns_moot_when_arm_cleared(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """Retry delivery reports MOOT once the block arm has been cleared."""
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:moot", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    outcome = await notifier._deliver_with_retry(
        parent.id,
        child,
        "notice",
        armed_id="elicit_moot",
    )
    assert outcome is _WakeOutcome.MOOT
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_handle_request_skips_resolution_notice_on_moot_delivery(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MOOT block delivery exits before waiting for a resolution notice."""
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:moot-handler", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    with notifier._lock:
        notifier._notified.add("elicit_moot_handler")

    async def _always_moot(
        self: SubagentBlockNotifier,
        parent_id: str,
        child: Any,
        notice: str,
        *,
        armed_id: str | None,
    ) -> _WakeOutcome:
        return _WakeOutcome.MOOT

    monkeypatch.setattr(SubagentBlockNotifier, "_deliver_with_retry", _always_moot)
    await notifier.handle_request(child.id, _request_event("elicit_moot_handler"))
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_handle_request_top_level_returns_after_escalation(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level sessions skip parent wake even after the escalation delay."""
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)
    top = conv_store.create_conversation(kind="default", title="root")
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    with notifier._lock:
        notifier._notified.add("elicit_top_escalation")

    await notifier.handle_request(top.id, _request_event("elicit_top_escalation"))

    assert dispatch.calls == []