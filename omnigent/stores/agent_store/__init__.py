"""Agent store — manages registered agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from omnigent.entities import Automation, PagedList


class AgentStore(ABC):
    """
    Abstract base for agent persistence.

    Manages the lifecycle of registered template agents: creation
    with template-name uniqueness enforcement, lookup by ID or name,
    paginated listing, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the agent store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///agents.db"`` for SQLAlchemy or a
            filesystem path for file-backed stores.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
    ) -> Automation:
        """
        Register a new template agent. Name must be unique among
        template agents and raises if a template with that name
        already exists.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``. Caller generates this so
            the bundle location can be computed before persisting.
        :param name: Human-readable agent name. Must be unique
            among template agents, e.g. ``"code-assistant"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description of the
            agent's purpose.
        :returns: The newly created :class:`Agent`.
        """
        ...

    @abstractmethod
    def get(self, agent_id: str) -> Automation | None:
        """
        Return the agent, or ``None`` if it does not exist.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def get_by_name(self, name: str) -> Automation | None:
        """
        Look up a registered template agent by its unique name.

        :param name: The template agent's unique name,
            e.g. ``"code-assistant"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        category: str | None = None,
    ) -> PagedList[Automation]:
        """
        List registered template agents with cursor-based pagination.

        ``order`` controls the sort direction on ``created_at``
        (``"desc"`` = newest-first, ``"asc"`` = oldest-first).

        :param limit: Maximum number of agents to return.
        :param after: Cursor agent ID; only return agents appearing
            *after* this agent in the sort order,
            e.g. ``"agent_abc123"``.
        :param before: Cursor agent ID; only return agents appearing
            *before* this agent in the sort order.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param category: When set, restrict to one tier (``"system"`` |
            ``"employee"`` | ``"workflow"``); ``None`` returns all tiers.
        :returns: A :class:`PagedList` of :class:`Automation` objects.
        """
        ...

    @abstractmethod
    def get_names(self, agent_ids: list[str]) -> dict[str, str]:
        """
        Batch-fetch agent names for a list of IDs.

        Returns a mapping from agent ID to agent name. IDs that do not
        exist in the store are silently omitted from the result.

        :param agent_ids: List of agent identifiers to look up,
            e.g. ``["ag_abc123", "ag_def456"]``.
        :returns: Mapping of ``{agent_id: agent_name}`` for found
            agents.
        """
        ...

    @abstractmethod
    def update(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expected_version: int | None = None,
    ) -> Automation | None:
        """
        Update an agent's bundle location, bump its version, and
        set ``updated_at``. Returns the updated agent, or ``None``
        if no agent with the given ID exists.

        Optimistic concurrency (BDP-2412, ADR-0150): when
        *expected_version* is given, the update is a guarded
        compare-and-swap on ``version`` — it only succeeds if the row
        is still at *expected_version*, closing the last-writer-wins
        clobber on concurrent edits. When omitted (``None``) the update
        is unconditional, so every existing caller is unchanged.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param expected_version: The version the caller last read (the
            ``If-Match`` ETag). ``None`` skips the precondition.
        :returns: The updated :class:`Agent`, or ``None`` if not
            found.
        :raises StaleWriteError: If *expected_version* is given but the
            row's version has since moved (concurrent write).
        """
        ...

    def set_sot_tier(self, agent_id: str, tier: str | None) -> bool:  # noqa: ARG002
        """
        Set the per-agent migration tier marker (ADR-0133/0136).

        ``None`` / ``"openclaw-resident"`` = OpenClaw is SoT (default);
        ``"migrated"`` = omnigent is SoT for this agent's config (e.g.
        after an org-chart edit through ``PUT /v1/agents/{id}/image``),
        which immunizes the agent against the startup wheel re-seed.

        The flip-able marker lives on the row, not in the immutable
        bundle params. Backends that don't persist it stay a no-op (it
        simply reads back as ``None``); the SQLAlchemy store overrides
        with the real implementation.

        :param agent_id: The registered agent id.
        :param tier: The tier marker, or ``None`` to clear it.
        :returns: ``True`` if the agent exists and was updated, else
            ``False``.
        """
        return False

    def get_sot_tier(self, agent_id: str) -> str | None:  # noqa: ARG002
        """
        Return the agent's migration tier marker, or ``None``.

        Default ``None`` for stores that don't track it; the SQLAlchemy
        store overrides.

        :param agent_id: The registered agent id.
        :returns: The tier string (e.g. ``"migrated"``), or ``None``.
        """
        return None

    def set_capabilities(
        self, agent_id: str, capabilities: Sequence[str] | None
    ) -> bool:  # noqa: ARG002
        """
        Persist the agent's declared capability slugs (BDP-2334, ADR-0142).

        Materializes the capability surface parsed from the agent spec onto
        the row so the assignment resolver / admin surfaces can read it back
        and filter agents by declared capability. Stored as JSON-in-Text.

        Backends that don't persist it stay a no-op (it simply reads back as
        ``None``); the SQLAlchemy store overrides with the real implementation.

        :param agent_id: The registered agent id.
        :param capabilities: The capability slugs, or ``None`` to clear them.
        :returns: ``True`` if the agent exists and was updated, else ``False``.
        """
        return False

    def get_capabilities(self, agent_id: str) -> tuple[str, ...]:  # noqa: ARG002
        """
        Return the agent's persisted capability slugs (empty if none/unset).

        Default empty for stores that don't track it; the SQLAlchemy store
        overrides.

        :param agent_id: The registered agent id.
        :returns: The declared capability slugs as a tuple (possibly empty).
        """
        return ()

    def set_category(self, agent_id: str, category: str | None) -> bool:  # noqa: ARG002
        """
        Persist the agent's tier classification (agent-tiering step 1).

        ``"system"`` | ``"employee"`` | ``"workflow"``, or ``None`` to clear.
        Written by the post-seed backfill so the column is authoritative for
        ``/v1/agents?category=``. Privilege axis, orthogonal to ``sot_tier``.

        Backends that don't persist it stay a no-op (it reads back as ``None``,
        and the converter falls back to name-only inference); the SQLAlchemy
        store overrides with the real implementation.

        :param agent_id: The registered agent id.
        :param category: The tier, or ``None`` to clear it.
        :returns: ``True`` if the agent exists and was updated, else ``False``.
        """
        return False

    def get_category(self, agent_id: str) -> str | None:  # noqa: ARG002
        """
        Return the agent's persisted tier, or ``None`` (unclassified/unset).

        Default ``None`` for stores that don't track it; the SQLAlchemy store
        overrides.

        :param agent_id: The registered agent id.
        :returns: The tier string, or ``None``.
        """
        return None

    @abstractmethod
    def delete(self, agent_id: str) -> bool:
        """
        Delete an agent. Returns ``True`` if the agent existed,
        ``False`` otherwise. Caller is responsible for cancelling
        in-flight tasks before calling this.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: ``True`` if the agent was deleted, ``False`` if
            it did not exist.
        """
        ...
