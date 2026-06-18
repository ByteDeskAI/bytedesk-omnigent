"""Unit tests for the native signal-bus tools (BDP-2248 α1 integration, ADR-0142)."""
from __future__ import annotations

import json

import pytest

from omnigent.bus import SqlAlchemySignalBus
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.signal_tools import (
    SignalAwaitTool,
    SignalCheckTool,
    SignalDeliverTool,
)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    b = SqlAlchemySignalBus(f"sqlite:///{tmp_path / 'bus.db'}")
    monkeypatch.setattr("omnigent.runtime.get_signal_bus", lambda: b)
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
