"""Unit tests for the native signal-bus tools (BDP-2248 α1 integration, ADR-0142)."""
from __future__ import annotations

import json

import pytest

from bytedesk_omnigent.bus import SqlAlchemySignalBus
from bytedesk_omnigent.tools.signal_tools import (
    SignalAwaitTool,
    SignalCheckTool,
    SignalDeliverTool,
)
from omnigent.tools.base import ToolContext


@pytest.fixture
def bus(tmp_path, monkeypatch):
    b = SqlAlchemySignalBus(f"sqlite:///{tmp_path / 'bus.db'}")
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: b)
    return b


def _ctx(conversation_id: str = "conv_1") -> ToolContext:
    return ToolContext(task_id="t", agent_id="ag_a", conversation_id=conversation_id)


def _call(tool, args: dict, conversation_id: str = "conv_1") -> dict:
    return json.loads(tool.invoke(json.dumps(args), _ctx(conversation_id)))


def test_await_deliver_then_check_roundtrip(bus) -> None:
    out = _call(SignalAwaitTool(), {"signal_id": "release:1.2.3", "key": "release"})
    assert out == {"signal_id": "release:1.2.3", "status": "pending"}

    delivered = _call(
        SignalDeliverTool(),
        {"signal_id": "release:1.2.3", "payload": {"build": "green"}},
    )
    assert delivered["status"] == "delivered"

    checked = _call(SignalCheckTool(), {})
    assert len(checked["signals"]) == 1
    assert checked["signals"][0]["payload"] == {"build": "green"}
    # Drained — a second check is empty.
    assert _call(SignalCheckTool(), {})["signals"] == []


def test_deliver_unmatched_signal_dead_letters(bus) -> None:
    result = _call(SignalDeliverTool(), {"signal_id": "nope"})
    assert result["status"] == "dead_lettered"


def test_duplicate_deliver_is_idempotent(bus) -> None:
    _call(SignalAwaitTool(), {"signal_id": "s1", "key": "k"})
    assert _call(SignalDeliverTool(), {"signal_id": "s1"})["status"] == "delivered"
    # A replayed deliver of the same id resolves idempotently.
    assert _call(SignalDeliverTool(), {"signal_id": "s1"})["status"] == "already_resolved"


def test_agent_cannot_forge_another_sessions_signal(bus) -> None:
    """An agent (namespaced to its own session) cannot deliver against another
    session's parked wait — the forge dead-letters and the victim stays un-woken
    (BDP-2288 #1: signal_deliver no longer del-ctx + delivers any raw id)."""
    # Victim session awaits 'release:1.2.3'.
    _call(
        SignalAwaitTool(),
        {"signal_id": "release:1.2.3", "key": "release"},
        conversation_id="conv_victim",
    )
    # Attacker session tries to forge that external signal.
    forged = _call(
        SignalDeliverTool(),
        {"signal_id": "release:1.2.3", "payload": {"build": "green"}},
        conversation_id="conv_attacker",
    )
    assert forged["status"] == "dead_lettered"  # namespaced away → no such wait
    # The victim is NOT woken.
    assert _call(SignalCheckTool(), {}, conversation_id="conv_victim")["signals"] == []


def test_agent_signal_id_is_session_scoped(bus) -> None:
    """Two sessions awaiting the SAME signal_id don't collide — each is namespaced
    to its own session, so neither can squat or starve the other (BDP-2288 #2)."""
    _call(SignalAwaitTool(), {"signal_id": "dup", "key": "k"}, conversation_id="conv_1")
    out = _call(SignalAwaitTool(), {"signal_id": "dup", "key": "k"}, conversation_id="conv_2")
    assert out["status"] == "pending"  # not a silent collision — conv_2 has its own wait

    # conv_2 resolves ITS own 'dup'; conv_1's is untouched.
    out2 = _call(SignalDeliverTool(), {"signal_id": "dup"}, conversation_id="conv_2")
    assert out2["status"] == "delivered"
    assert len(_call(SignalCheckTool(), {}, conversation_id="conv_2")["signals"]) == 1
    assert _call(SignalCheckTool(), {}, conversation_id="conv_1")["signals"] == []
