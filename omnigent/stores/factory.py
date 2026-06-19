"""Store bootstrapper — one place that wires the persistence stores.

Part of the omnigent core-refactor spine (BDP-2327, Phase 1). The
``omnigent server`` command in :mod:`omnigent.cli` instantiates the
``SqlAlchemy*`` stores and the artifact store inline before calling
:func:`omnigent.server.create_app`. :class:`StoreBootstrapper` lifts that
exact construction into a single, testable factory so the composition
root has one seam for "build all the stores from a DB URI + artifact
location".

This is a strangler-fig sidecar: it is used by ``cli.server`` only when
``OMNIGENT_USE_STORE_BOOTSTRAPPER`` is on. With the flag off the existing
inline instantiation in ``cli.py`` stays the default path, so behavior is
unchanged. The factory must therefore construct **the same stores, the
same way** — including the ``dbfs:/Volumes/...`` Databricks-vs-Local
artifact-store branch — so flipping the flag is byte-identical wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


def _create_artifact_store(location: str) -> Any:  # type: ignore[explicit-any]  # ArtifactStore protocol (optional deps)
    """Create an artifact store based on the location URI scheme.

    Mirrors ``omnigent.cli._create_artifact_store``: ``dbfs:/Volumes/...``
    URIs use :class:`DatabricksVolumesArtifactStore` (requires
    ``databricks-sdk``); all other locations use
    :class:`LocalArtifactStore`. Imports are deferred so the Databricks
    backend's optional dependency is only required when actually used.

    :param location: Artifact storage location, e.g. ``"./artifacts"`` or
        ``"dbfs:/Volumes/cat/schema/vol"``.
    :returns: An :class:`~omnigent.stores.ArtifactStore` instance.
    """
    if location.startswith("dbfs:/Volumes/"):
        from omnigent.stores.artifact_store.databricks_volumes import (
            DatabricksVolumesArtifactStore,
        )

        return DatabricksVolumesArtifactStore(location)

    from omnigent.stores.artifact_store.local import LocalArtifactStore

    return LocalArtifactStore(location)


@dataclass(frozen=True)
class BootstrappedStores:
    """The set of persistence stores a server needs, wired together.

    Field names match the keyword arguments
    :func:`omnigent.server.create_app` accepts, so a caller can splat or
    forward them directly.

    :param agent_store: Store for agent CRUD.
    :param file_store: Store for uploaded-file metadata.
    :param conversation_store: Store for conversations + items.
    :param comment_store: Store for per-conversation review comments.
    :param policy_store: Store for server-persisted policies.
    :param permission_store: Store for session-level access grants.
    :param artifact_store: Store for binary blobs (bundles, file content).
    :param host_store: Store for host registrations.
    """

    agent_store: Any
    file_store: Any
    conversation_store: Any
    comment_store: Any
    policy_store: Any
    permission_store: Any
    artifact_store: Any
    host_store: Any

    def all_stores(self) -> tuple[Any, ...]:  # type: ignore[explicit-any]  # heterogeneous stores
        """The wired stores as a tuple, in construction order.

        Convenience accessor for :meth:`run_lifecycle` and any caller that
        wants to iterate the store set uniformly.

        :returns: Every store in this bundle.
        """
        return (
            self.agent_store,
            self.file_store,
            self.conversation_store,
            self.comment_store,
            self.policy_store,
            self.permission_store,
            self.artifact_store,
            self.host_store,
        )

    async def run_lifecycle(self, phase: str) -> dict[int, bool | None]:
        """Drive one lifecycle phase across every wired store (gated, no-op default).

        Delegates to :func:`omnigent.stores.lifecycle.run_store_lifecycle`,
        which invokes a store's hook **only when the store defines one** and
        is itself gated behind ``OMNIGENT_STORE_LIFECYCLE_HOOKS`` (default
        OFF). With the flag unset this is an immediate no-op, so it is safe
        to call from a boot/shutdown path without changing behavior.

        :param phase: ``"startup"``, ``"shutdown"``, or ``"health_check"``.
        :returns: Mapping of ``id(store) -> result`` for stores that define
            the hook; empty when the flag is off.
        """
        from omnigent.stores.lifecycle import LifecyclePhase, run_store_lifecycle

        return await run_store_lifecycle(self.all_stores(), cast(LifecyclePhase, phase))


class StoreBootstrapper:
    """Construct the persistence stores from a DB URI + artifact location.

    Factory for the exact store set ``cli.server`` wires inline today.
    Stateless — :meth:`create` is the single entry point.
    """

    @staticmethod
    def create(db_uri: str, artifact_location: str) -> BootstrappedStores:
        """Instantiate every persistence store for a server.

        Imports are deferred (matching ``cli.server``) so optional store
        backends are only loaded when this runs, and to avoid import
        cycles. The artifact store honors the same
        ``dbfs:/Volumes/...`` Databricks-vs-Local branch as the inline
        path via :func:`_create_artifact_store`.

        :param db_uri: SQLAlchemy database URI shared by every SQL store,
            e.g. ``"sqlite:////home/me/.omnigent/omnigent.db"``.
        :param artifact_location: Artifact storage location, e.g.
            ``"./artifacts"`` or ``"dbfs:/Volumes/cat/schema/vol"``.
        :returns: A :class:`BootstrappedStores` with every store wired.
        """
        from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
        from omnigent.stores.comment_store.sqlalchemy_store import (
            SqlAlchemyCommentStore,
        )
        from omnigent.stores.conversation_store.sqlalchemy_store import (
            SqlAlchemyConversationStore,
        )
        from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
        from omnigent.stores.host_store import HostStore
        from omnigent.stores.permission_store.sqlalchemy_store import (
            SqlAlchemyPermissionStore,
        )
        from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

        return BootstrappedStores(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            comment_store=SqlAlchemyCommentStore(db_uri),
            policy_store=SqlAlchemyPolicyStore(db_uri),
            permission_store=SqlAlchemyPermissionStore(db_uri),
            artifact_store=_create_artifact_store(artifact_location),
            host_store=HostStore(db_uri),
        )


__all__ = ["StoreBootstrapper", "BootstrappedStores"]
