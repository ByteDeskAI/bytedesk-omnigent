"""Unit tests for the FU1 memory builtins (BDP-2147, ADR-0132).

Verifies the append/query/list round-trip and the server-side owner-stamping
anti-spoofing invariant: an agent operates only on its own ``agent``-scope
memory; ``team`` / ``topic`` are shared; a forged ``owner`` argument is ignored.
"""

from __future__ import annotations

import json

import pytest

from omnigent.stores.memory_store import (
    ComposedAgentMemoryProvider,
    SqlAlchemyMemoryStore,
)
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.memory import (
    MemoryAppendTool,
    MemoryCompartmentsListTool,
    MemoryQueryTool,
    _resolve_owner,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temp-sqlite memory store wired in behind the runtime's memory provider.

    The tools go through ``get_memory_provider`` (BDP-2369), so wire the temp store
    behind the in-tree composed provider rather than monkeypatching the legacy
    store getter directly.
    """
    s = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'mem.db'}")
    provider = ComposedAgentMemoryProvider(s)
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)
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


def test_query_routes_reinforcement_through_port(tmp_path, monkeypatch) -> None:
    """The query tool reinforces via the provider port (provider.note_recalled),
    NOT a reach-through to db.utils / the reinforcement-buffer module (BDP-2369)."""
    from omnigent.stores.memory_store import ComposedAgentMemoryProvider

    s = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'port.db'}")
    provider = ComposedAgentMemoryProvider(s)
    noted: list[list[str]] = []
    monkeypatch.setattr(provider, "note_recalled",
                        lambda hits: noted.append([h.id for h in hits]))
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    _append({"content": "alpha through the port"})
    res = _query({"query": "alpha"})
    assert len(res["results"]) == 1
    assert noted and len(noted[0]) == 1, "recall must reinforce via the port"


def test_invalid_scope_errors(store) -> None:
    assert "error" in _append({"content": "x", "scope": "tenant"})
    assert "error" in _query({"query": "x", "scope": "tenant"})


def test_missing_content_errors(store) -> None:
    assert "error" in _append({"scope": "agent"})


def test_memory_tool_identity_helpers() -> None:
    """Registration helpers expose stable metadata for all memory tools."""
    assert MemoryAppendTool.name() == "memory_append"
    assert "recall" in MemoryAppendTool.description().lower()
    assert MemoryAppendTool().get_schema()["function"]["name"] == "memory_append"

    assert MemoryQueryTool.name() == "memory_query"
    assert "salience" in MemoryQueryTool.description().lower()
    query_params = MemoryQueryTool().get_schema()["function"]["parameters"]
    assert query_params["required"] == ["query"]

    assert MemoryCompartmentsListTool.name() == "memory_compartments_list"
    assert "compartments" in MemoryCompartmentsListTool.description().lower()
    assert (
        MemoryCompartmentsListTool().get_schema()["function"]["parameters"]["required"]
        == []
    )


def test_resolve_owner_team_and_agent_requirements() -> None:
    """Owner resolution is server-derived per scope."""
    ctx = _ctx("ag_maya")
    assert _resolve_owner("team", ctx) == "team"
    assert _resolve_owner("topic", ctx) == "shared"
    assert _resolve_owner("agent", ctx) == "ag_maya"

    no_agent = ToolContext(task_id="t", agent_id=None, conversation_id="c")
    with pytest.raises(ValueError, match="agent identity"):
        _resolve_owner("agent", no_agent)


def test_team_scope_append_and_query_roundtrip(store) -> None:
    """Team-scope memories are shared across agents."""
    _append({"content": "standup at nine", "scope": "team", "name": "rituals"}, agent_id="ag_a")
    res = _query(
        {"query": "standup", "scope": "team", "name": "rituals"},
        agent_id="ag_b",
    )
    assert len(res["results"]) == 1


def test_append_without_agent_identity_errors(store) -> None:
    """Agent-scope writes require a server-stamped agent id."""
    ctx = ToolContext(task_id="t", agent_id=None, conversation_id="c")
    result = json.loads(MemoryAppendTool().invoke('{"content": "x"}', ctx))
    assert "agent identity" in result["error"]


def test_query_missing_query_argument(store) -> None:
    """Query tool rejects empty query strings."""
    result = json.loads(MemoryQueryTool().invoke("{}", _ctx()))
    assert result["error"] == "missing required 'query' argument"


def test_query_empty_results_include_message(store) -> None:
    """No hits return an explicit empty-results message."""
    result = _query({"query": "definitely-not-stored"})
    assert result["results"] == []
    assert result["message"] == "No matching memories."


def test_append_provider_value_error_surfaces(store, monkeypatch) -> None:
    """Provider write failures round-trip as structured tool errors."""
    from omnigent.runtime import get_memory_provider

    provider = get_memory_provider()

    def _boom(**_kwargs: object) -> str:
        raise ValueError("weight out of range")

    monkeypatch.setattr(provider, "write", _boom)
    result = _append({"content": "bad weight", "weight": 99})
    assert result["error"] == "weight out of range"


def test_query_provider_value_error_surfaces(store, monkeypatch) -> None:
    """Provider recall failures round-trip as structured tool errors."""
    from omnigent.runtime import get_memory_provider

    provider = get_memory_provider()

    def _boom(**_kwargs: object) -> list[object]:
        raise ValueError("invalid limit")

    monkeypatch.setattr(provider, "recall", _boom)
    result = _query({"query": "anything"})
    assert result["error"] == "invalid limit"


def test_compartments_list_without_agent_id_lists_shared_only(store) -> None:
    """Without an agent id only team/topic compartments are listed."""
    _append({"content": "shared", "scope": "topic", "name": "arch"}, agent_id="ag_maya")
    ctx = ToolContext(task_id="t", agent_id=None, conversation_id="c")
    out = json.loads(MemoryCompartmentsListTool().invoke("{}", ctx))
    scopes = {c["scope"] for c in out["compartments"]}
    assert "agent" not in scopes
    assert "topic" in scopes or "team" in scopes
