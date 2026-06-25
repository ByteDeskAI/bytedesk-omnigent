"""Edge tests for signal tool metadata and validation branches."""

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


def _ctx(conversation_id: str | None = "conv_1") -> ToolContext:
    return ToolContext(task_id="t", agent_id="ag_a", conversation_id=conversation_id)


def test_signal_tool_metadata_and_schemas() -> None:
    await_tool = SignalAwaitTool()
    deliver_tool = SignalDeliverTool()
    check_tool = SignalCheckTool()

    assert SignalAwaitTool.name() == "signal_await"
    assert "durable wait" in SignalAwaitTool.description().lower()
    assert await_tool.get_schema()["function"]["name"] == "signal_await"
    assert "signal_id" in await_tool.get_schema()["function"]["parameters"]["properties"]

    assert SignalDeliverTool.name() == "signal_deliver"
    assert "idempotent" in SignalDeliverTool.description().lower()
    assert deliver_tool.get_schema()["function"]["name"] == "signal_deliver"

    assert SignalCheckTool.name() == "signal_check"
    assert "inbox" in SignalCheckTool.description().lower()
    assert check_tool.get_schema()["function"]["parameters"]["required"] == []


def test_signal_await_rejects_missing_fields_and_requires_session(bus) -> None:
    tool = SignalAwaitTool()
    assert json.loads(tool.invoke('{"signal_id": "s"}', _ctx()))["error"]
    assert json.loads(tool.invoke('{"key": "k"}', _ctx()))["error"]
    assert (
        json.loads(tool.invoke('{"signal_id": "s", "key": "k"}', _ctx(None)))["error"]
        == "signal_await requires a session"
    )


def test_signal_deliver_rejects_missing_id_and_requires_session(bus) -> None:
    tool = SignalDeliverTool()
    assert json.loads(tool.invoke("{}", _ctx()))["error"] == "missing required 'signal_id'"
    assert (
        json.loads(tool.invoke('{"signal_id": "s"}', _ctx(None)))["error"]
        == "signal_deliver requires a session"
    )


def test_signal_check_requires_session(bus) -> None:
    tool = SignalCheckTool()
    assert json.loads(tool.invoke("{}", _ctx(None)))["error"] == "signal_check requires a session"
