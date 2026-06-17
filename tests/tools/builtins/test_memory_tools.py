"""Unit tests for the FU1 memory builtins (BDP-2147, ADR-0132).

Verifies the append/query/list round-trip and the server-side owner-stamping
anti-spoofing invariant: an agent operates only on its own ``agent``-scope
memory; ``team`` / ``topic`` are shared; a forged ``owner`` argument is ignored.
"""

from __future__ import annotations

import json

import pytest

from omnigent.stores.memory_store import SqlAlchemyMemoryStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.memory import (
    MemoryAppendTool,
    MemoryCompartmentsListTool,
    MemoryQueryTool,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temp-sqlite memory store wired in as the runtime's memory store."""
    s = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'mem.db'}")
    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: s)
    return s


def _ctx(agent_id: str = "ag_maya") -> ToolContext:
    return ToolContext(task_id="task_t", agent_id=agent_id, conversation_id="conv_1")


def _append(args: dict, agent_id: str = "ag_maya") -> dict:
    return json.loads(MemoryAppendTool().invoke(json.dumps(args), _ctx(agent_id)))


def _query(args: dict, agent_id: str = "ag_maya") -> dict:
    return json.loads(MemoryQueryTool().invoke(json.dumps(args), _ctx(agent_id)))


def test_append_then_query_roundtrip(store) -> None:
    out = _append({"content": "Ryan prefers fastembed", "scope": "agent", "name": "notes"})
    assert out["memory_id"].startswith("mem_")
    res = _query({"query": "fastembed"})
    assert len(res["results"]) == 1
    assert "fastembed" in res["results"][0]["content"]


def test_agent_scope_is_owner_isolated(store) -> None:
    _append({"content": "maya secret alpha"}, agent_id="ag_maya")
    # A different agent cannot recall maya's private agent-scope memory.
    assert _query({"query": "alpha"}, agent_id="ag_nolan")["results"] == []
    # Maya can.
    assert len(_query({"query": "alpha"}, agent_id="ag_maya")["results"]) == 1


def test_forged_owner_arg_is_ignored(store) -> None:
    # The tool exposes no 'owner' param; a forged one is ignored and the memory
    # is stored under the server-stamped ctx.agent_id (anti-spoofing).
    _append({"content": "sneaky note", "owner": "ag_victim", "scope": "agent"}, agent_id="ag_attacker")
    assert _query({"query": "sneaky"}, agent_id="ag_victim")["results"] == []
    assert len(_query({"query": "sneaky"}, agent_id="ag_attacker")["results"]) == 1


def test_shared_topic_scope(store) -> None:
    _append({"content": "build uses pgvector", "scope": "topic", "name": "omnigent-arch"}, agent_id="ag_maya")
    # A different agent CAN recall a shared topic compartment.
    res = _query(
        {"query": "pgvector", "scope": "topic", "name": "omnigent-arch"}, agent_id="ag_nolan"
    )
    assert len(res["results"]) == 1


def test_compartments_list(store) -> None:
    _append({"content": "x", "scope": "agent", "name": "notes"}, agent_id="ag_maya")
    _append({"content": "y", "scope": "topic", "name": "arch"}, agent_id="ag_maya")
    out = json.loads(MemoryCompartmentsListTool().invoke("{}", _ctx("ag_maya")))
    names = {(c["scope"], c["name"]) for c in out["compartments"]}
    assert ("agent", "notes") in names
    assert ("topic", "arch") in names


def test_invalid_scope_errors(store) -> None:
    assert "error" in _append({"content": "x", "scope": "tenant"})


def test_missing_content_errors(store) -> None:
    assert "error" in _append({"scope": "agent"})
