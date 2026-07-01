"""Agent reference normalization for API/tool boundaries."""

from __future__ import annotations

from omnigent.entities import Automation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores import AgentStore


def resolve_agent_ref(
    agent_store: AgentStore,
    agent_ref: str | None,
    *,
    template_only: bool = False,
) -> Automation | None:
    """Resolve a durable agent id or template-agent name to an agent row."""
    ref = (agent_ref or "").strip()
    if not ref:
        return None
    agent = agent_store.get(ref)
    if agent is None:
        get_by_name = getattr(agent_store, "get_by_name", None)
        if get_by_name is not None:
            agent = get_by_name(ref)
    if agent is None:
        return None
    if template_only and agent.session_id is not None:
        return None
    return agent


def require_agent_ref(
    agent_store: AgentStore,
    agent_ref: str | None,
    *,
    template_only: bool = False,
    not_found: str | None = None,
) -> Automation:
    """Resolve an agent reference or raise a route-friendly 404."""
    agent = resolve_agent_ref(agent_store, agent_ref, template_only=template_only)
    if agent is None:
        ref = (agent_ref or "").strip()
        message = not_found or f"Agent not found: {ref!r}"
        raise OmnigentError(message, code=ErrorCode.NOT_FOUND)
    return agent


def resolve_agent_ref_id(
    agent_store: AgentStore,
    agent_ref: str | None,
    *,
    template_only: bool = False,
) -> str | None:
    """Return the durable id for an optional id/name reference."""
    if agent_ref is None:
        return None
    return require_agent_ref(
        agent_store,
        agent_ref,
        template_only=template_only,
    ).id


def resolve_agent_ref_ids(
    agent_store: AgentStore,
    agent_refs: list[str],
    *,
    template_only: bool = False,
) -> list[str]:
    """Normalize many id/name references while preserving order."""
    return [
        require_agent_ref(agent_store, ref, template_only=template_only).id
        for ref in agent_refs
    ]
