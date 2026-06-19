"""``office:agents`` channel + delta contract (BDP-2301).

The platform C# ``RealtimeTopicRegistry`` resolves the SignalR topic
``office:agents`` to the Redis channel ``office:agents:{tenant}`` with the
tenant as a DASHED guid (BDP-1397). This module is the omnigent-side half of
that hand-mirrored contract; ``test_channel.py`` pins the exact strings, and the
platform's RealtimeTopicRegistryTests pins the C# half. Keep them in lockstep
(BDP-2302 will make the registry a fetched SoT and retire this duplication).

Delta envelopes are intentionally TINY — the plugin never re-projects Office's
models. ``roster.changed`` tells the org chart to refetch the snapshot (cached
reader → omnigent SoT); ``presence.changed`` carries just an agent's new
activity status.
"""

from __future__ import annotations

from typing import Any


def office_agents_channel(tenant: str) -> str:
    """The Redis channel ByteDesk.Realtime fans out to the ``office:agents`` topic."""
    return f"office:agents:{tenant}"


def roster_changed(action: str, agent_id: str) -> dict[str, Any]:
    """An agent was created/updated/deleted (incl. live config edits)."""
    return {"type": "roster.changed", "action": action, "agentId": agent_id}


def presence_changed(agent_id: str, status: str) -> dict[str, Any]:
    """An agent's live activity status changed (active when working, idle otherwise)."""
    return {"type": "presence.changed", "agentId": agent_id, "status": status}
