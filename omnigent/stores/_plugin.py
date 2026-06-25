"""CORE first-party plugin — the ``stores`` subpackage (BDP-2509).

Dogfoods the kernel plugin contract for the persistence layer: this plugin
registers the storage subpackage's *existing* default providers into the three
storage kernel seams it owns, using the same :class:`~omnigent.sdk` decorator and
the same :class:`~omnigent.kernel.extensions.OmnigentExtension` Protocol a third party
would use. Per the three-tier picture (Section 9.1 "stores" row, Section 10 line
776, dogfooding argument Section 9.2):

  * ``artifact_store``  — hook ``artifact_store_providers``
  * ``agent_memory``    — hook ``agent_memory_providers``
  * ``memory_embedder`` — hook ``memory_embedder_providers``

No provider is moved or rewritten. The concrete default classes already live in
``omnigent/stores/{artifact_store,memory_store}/`` and are reached through the
existing registry-builders in :mod:`omnigent.stores.factory` and
:mod:`omnigent.stores.memory_store.provider`; this plugin only re-expresses the
*registrations* through the seam hooks, so when the Integration phase wires it
into boot (and drops the registries' own hard-wired defaults), the swap is clean.

**Names mirror the canonical defaults** the seam registries register today
(``local``/``dbfs``/``nats``, ``composed``, ``fastembed``) so the rest of core,
which resolves by those names + the ``OMNIGENT_USE_<SEAM>`` strangler flag, keeps
working byte-for-byte once this plugin is the sole registrant.

**Not boot-wired yet.** This module is import-clean and exposes correct hook
returns; the Integration phase adds the ``pyproject.toml`` entry-point /
``default_extensions()`` wiring. Until then it never double-registers (no seam
calls its hook), so no :class:`~omnigent.kernel.pluggable.errors.RegistryConflict` fires.

**Circular-import / hot-path safety (kernel rule 4):** every domain import lives
*inside* a hook method (and inside the per-factory closure where the optional
backend dependency matters), so importing this module pulls only the SDK facade —
the FastAPI/SQLAlchemy/fastembed stacks stay off the import path. The factories
the hooks return are the zero-arg ``() -> provider`` shape the
:class:`~omnigent.kernel.pluggable.PluggableRegistry` stores; locations/URIs are resolved
lazily at factory-call time (the same source the composition root uses), never at
hook-call time, so describe/discover stay side-effect-free.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omnigent.sdk import extension


@extension(name="omnigent.stores")
class StoresExtension:
    """First-party storage plugin: registers the default store providers.

    Hand-written seam hooks (the three storage seams have no dedicated SDK
    member-decorator, so they are written directly rather than synthesised). The
    ``@extension`` decorator leaves these methods untouched — it only fills in the
    Protocol members the author did *not* define — and supplies the no-op
    ``routers()`` / empty optional hooks that make ``isinstance(self,
    OmnigentExtension)`` hold.
    """

    # ── artifact_store seam (hook: artifact_store_providers) ─────────────────
    def artifact_store_providers(self) -> dict[str, Callable[[], Any]]:
        """The three built-in artifact-store backends, keyed by URI scheme.

        Mirrors the defaults :func:`omnigent.stores.factory._build_artifact_store_registry`
        registers (``local`` default, ``dbfs``, ``nats``). Each factory is the
        zero-arg ``() -> ArtifactStore`` shape the seam registry stores; the
        optional backend dependency (``databricks-sdk`` / ``nats-py``) is imported
        only inside the factory that needs it, so this hook stays cheap and the
        unused backends never load.

        The storage location is resolved lazily at factory-call time from the same
        default the CLI/composition root uses, so a constructed backend points at
        the real artifact dir without this hook needing a boot-time location.
        """

        def _location() -> str:
            # Deferred: the CLI module pulls the broader server surface; keep it
            # off this module's import path. The Integration phase may instead
            # inject the resolved location, but the default here matches today's
            # ``omnigent server`` behaviour (``<data_dir>/artifacts``).
            from omnigent.cli import _default_artifact_location

            return _default_artifact_location()

        def _local() -> Any:
            from omnigent.stores.artifact_store.local import LocalArtifactStore

            return LocalArtifactStore(_location())

        def _databricks() -> Any:
            from omnigent.stores.artifact_store.databricks_volumes import (
                DatabricksVolumesArtifactStore,
            )

            return DatabricksVolumesArtifactStore(_location())

        def _nats() -> Any:
            from omnigent.stores.artifact_store.nats_object_store import (
                NatsObjectStoreArtifactStore,
            )

            return NatsObjectStoreArtifactStore(_location())

        return {"local": _local, "dbfs": _databricks, "nats": _nats}

    # ── memory_embedder seam (hook: memory_embedder_providers) ───────────────
    def memory_embedder_providers(self) -> dict[str, Callable[[], Any]]:
        """The default recall embedder, keyed ``fastembed``.

        Mirrors :func:`omnigent.stores.memory_store.provider.build_embedder_registry`'s
        default. The optional ``fastembed`` dependency is imported only when the
        factory runs (parity with the registry-builder), so this hook is import-light.
        """

        def _fastembed() -> Any:
            from omnigent.stores.memory_store.embeddings import FastEmbedEmbedder

            return FastEmbedEmbedder()

        return {"fastembed": _fastembed}

    # ── agent_memory seam (hook: agent_memory_providers) ─────────────────────
    def agent_memory_providers(self) -> dict[str, Callable[[], Any]]:
        """The default composed agent-memory provider, keyed ``composed``.

        Mirrors :func:`omnigent.stores.memory_store.provider.build_memory_provider_registry`'s
        default (store + embedder + recall mode). The storage URI is resolved
        lazily at factory-call time from the live conversation store — exactly as
        :func:`omnigent.runtime.get_memory_provider` does — so the provider is built
        for the active database without this hook closing over a boot-time URI.
        """

        def _composed() -> Any:
            from omnigent.runtime import get_conversation_store
            from omnigent.stores.memory_store.provider import (
                ComposedAgentMemoryProvider,
            )

            location = get_conversation_store().storage_location
            return ComposedAgentMemoryProvider.from_location(location)

        return {"composed": _composed}


__all__ = ["StoresExtension"]
