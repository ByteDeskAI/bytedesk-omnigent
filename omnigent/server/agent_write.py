"""Shared store+cache mutation for an agent bundle update.

Single home for the "store a new bundle, repoint the row, warm-swap the
cache" sequence used by every agent-write path: the session-scoped
``PUT /v1/sessions/{id}/agent`` route, the template-agent
``PUT /v1/agents/{id}/image`` route, and CLI ``--agent`` registration.
Keeping it in one place means the content-address idempotency and the
``expand_env`` provenance rule can't drift between callers.
"""

from __future__ import annotations

from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.bundles import bundle_location
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore


def apply_bundle_update(
    agent: Agent,
    bundle_bytes: bytes,
    *,
    artifact_store: ArtifactStore | None,
    agent_store: AgentStore,
    agent_cache: AgentCache | None,
    expand_env: bool,
) -> Agent:
    """
    Persist a new bundle for *agent* and warm-swap the cache.

    Content-addressed and idempotent: if the new bundle hashes to the
    agent's current ``bundle_location`` the call is a no-op and returns
    *agent* unchanged (no store write, no cache churn). Otherwise the
    bundle is stored, the agent row repointed (version bumped), and the
    cache warm-swapped so the change is live without a server restart.

    Blocking (artifact store IO + disk extraction in
    :meth:`AgentCache.replace`) — async callers should run it via
    ``asyncio.to_thread``.

    :param agent: The agent whose bundle is being replaced.
    :param bundle_bytes: Raw bytes of the new ``.tar.gz`` bundle.
    :param artifact_store: Blob store for the bundle bytes. Raises if
        ``None`` and a write is required.
    :param agent_store: Metadata store; ``update`` repoints the row.
    :param agent_cache: Two-tier cache to warm-swap; ``None`` skips the
        swap (the next ``load`` re-fetches from the store).
    :param expand_env: Whether the cache should expand ``${VAR}`` in the
        spec against the server env. MUST be ``agent.session_id is None``
        — only operator-authored template agents expand server-side; a
        tenant session-scoped bundle must not (W7-3 secret-leak guard).
    :returns: The updated :class:`Agent`, or *agent* unchanged on a
        content-identical no-op.
    :raises OmnigentError: If a write is required but *artifact_store*
        is ``None``, or the agent row vanished mid-update.
    """
    new_loc = bundle_location(agent.id, bundle_bytes)

    # Idempotency: identical content → identical address → no-op.
    if new_loc == agent.bundle_location:
        return agent

    if artifact_store is None:
        raise OmnigentError(
            "Artifact store not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    artifact_store.put(new_loc, bundle_bytes)
    updated = agent_store.update(agent.id, new_loc)
    if updated is None:
        raise OmnigentError(
            f"Agent not found: {agent.id!r}",
            code=ErrorCode.NOT_FOUND,
        )

    if agent_cache is not None:
        agent_cache.replace(agent.id, new_loc, bundle_bytes, expand_env=expand_env)

    return updated
