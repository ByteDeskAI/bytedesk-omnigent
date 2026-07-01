from __future__ import annotations

import pytest

from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.server.agent_refs import require_agent_ref, resolve_agent_ref


class _Store:
    def __init__(self, agents: list[Agent]) -> None:
        self._by_id = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> Agent | None:
        return self._by_id.get(agent_id)

    def get_by_name(self, name: str) -> Agent | None:
        for agent in self._by_id.values():
            if agent.name == name and agent.session_id is None:
                return agent
        return None


def _agent(agent_id: str, name: str, *, session_id: str | None = None) -> Agent:
    return Agent(
        id=agent_id,
        created_at=1,
        name=name,
        bundle_location=f"{agent_id}/bundle",
        session_id=session_id,
    )


def test_resolves_durable_agent_id() -> None:
    agent = _agent("ag_maya", "chief-of-staff")
    store = _Store([agent])

    assert resolve_agent_ref(store, "ag_maya") is agent


def test_resolves_template_agent_name() -> None:
    agent = _agent("ag_maya", "chief-of-staff")
    store = _Store([agent])

    assert resolve_agent_ref(store, "chief-of-staff") is agent


def test_template_only_rejects_session_scoped_agent() -> None:
    agent = _agent("ag_session", "worker", session_id="conv_1")
    store = _Store([agent])

    assert resolve_agent_ref(store, "ag_session", template_only=True) is None


def test_require_agent_ref_raises_404_for_missing_name() -> None:
    store = _Store([])

    with pytest.raises(OmnigentError) as exc:
        require_agent_ref(store, "missing-agent")

    assert exc.value.code == "not_found"
