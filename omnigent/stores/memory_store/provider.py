"""First-class pluggable agent-memory port (BDP-2369, ADR-0132).

The agent-memory plane is a **domain capability** with a real second-impl market
(pgvector default → Pinecone/Qdrant/Weaviate, or a hosted memory service like
mem0/zep). This module makes the whole backend swappable behind one coarse-grained
:class:`AgentMemoryProvider` Protocol, registered in a
:class:`~omnigent.pluggable.PluggableRegistry` (the artifact-store seam is the
worked reference; see :mod:`omnigent.stores.factory`).

TWO-LEVEL composition — do **not** add a fourth parallel memory thing:

* The default in-tree provider (:class:`ComposedAgentMemoryProvider`) COMPOSES the
  three fine-grained sub-seams so they stay mix-and-match:

  - **#24 store backend** — all access routes through the
    :class:`~omnigent.stores.memory_store.SqlAlchemyMemoryStore` (its ABC surface),
    never a reach-through to ``stores.memory_store`` / ``db.utils``.
  - **#8 recall embedder** — selected from :func:`build_embedder_registry` (a
    ``PluggableRegistry`` over the existing
    :class:`~omnigent.stores.memory_store.embeddings.Embedder` Protocol) instead of
    the inline ``runtime._select_memory_embedder`` switch.
  - **#30 recall mode** — semantic pgvector vs lexical FTS as a
    :class:`RecallMode`; the default provider auto-selects exactly as the store did
    (semantic only when an embedder is attached **and** the dialect is PostgreSQL).

* A fully-external provider (Pinecone/mem0/zep) would IGNORE those sub-seams and
  talk to its own service — the Protocol seam is left here so one can be registered
  later via the ``agent_memory_providers`` extension hook. We do not build one now
  (YAGNI).

Behavior is byte-identical to the pre-port path: the default embedder selection
mirrors the old ``_select_memory_embedder`` gate (no model loaded on SQLite or a
Postgres without pgvector), the embedder dimension is unchanged (a dim change would
need a ``vector(N)`` migration), and recall ranking is the store's existing
weighted-decay composite.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from omnigent.pluggable import PluggableRegistry
from omnigent.stores.memory_store.embeddings import Embedder
from omnigent.stores.memory_store.sqlalchemy_store import (
    MemoryHit,
    SqlAlchemyMemoryStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ── recall mode (#30) ────────────────────────────────────────────────────────


class RecallMode(str, enum.Enum):
    """How a recall ranks candidates within a compartment.

    * ``AUTO`` — let the provider pick (semantic when available, else lexical);
      this is the historical default behavior.
    * ``SEMANTIC`` — pgvector cosine retrieval (requires an embedder + PostgreSQL).
    * ``LEXICAL`` — FTS5 / tsvector keyword match.
    """

    AUTO = "auto"
    SEMANTIC = "semantic"
    LEXICAL = "lexical"


# ── recall record ────────────────────────────────────────────────────────────

# A recalled memory. The store already returns a frozen ``MemoryHit`` with exactly
# the fields a recall surfaces (id, content, weight, effective_weight, …), so the
# provider's recall record IS ``MemoryHit`` — re-exported as ``Memory`` so the port
# vocabulary reads cleanly and an external provider can construct the same shape.
Memory = MemoryHit


# ── the port (the one coarse-grained swap unit) ──────────────────────────────


@runtime_checkable
class AgentMemoryProvider(Protocol):
    """Swappable backend for the whole agent-memory plane.

    One coarse-grained seam: the default composes store + embedder + recall mode;
    an external provider (Pinecone/mem0/zep) ignores those and uses its own service.
    """

    def write(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        content: str,
        weight: float = 1.0,
        source_conversation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Persist a memory; returns its id."""
        ...

    def recall(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        query: str,
        k: int = 10,
        mode: RecallMode = RecallMode.AUTO,
        kind: str = "ambient",
    ) -> list[Memory]:
        """Recall up to ``k`` memories ranked by relevance × decayed weight.

        *kind* (BDP-2459): ``"ambient"`` (default) / ``"addressable"`` / ``"all"``
        selects whether keyed slots are included in the search candidate set.
        """
        ...

    def list_compartments(
        self, *, scope: str | None = None, owner: str | None = None
    ) -> list[dict]:
        """Enumerate compartments reachable by the caller."""
        ...

    def note_recalled(self, hits: list[Memory]) -> None:
        """Record recalled hits for out-of-band reinforcement (off the recall path)."""
        ...

    def health(self) -> dict[str, Any]:
        """Lifecycle/health hook: a small status dict for capability manifests."""
        ...


# ── embedder seam (#8) ───────────────────────────────────────────────────────

EMBEDDER_SEAM = "memory_embedder"


def build_embedder_registry() -> PluggableRegistry[Embedder]:
    """Registry over the :class:`Embedder` Protocol (sub-seam #8).

    Default = the in-tree ``fastembed`` ``BAAI/bge-small-en-v1.5`` (384-dim)
    embedder, lazily imported so the optional ``fastembed`` dependency only loads
    when the embedder is actually constructed (parity with the old inline import in
    ``_select_memory_embedder``). Extensions contribute alternatives via the
    ``memory_embedder_providers`` hook; ``OMNIGENT_USE_MEMORY_EMBEDDER`` pins one.
    """

    def _fastembed() -> Embedder:
        from omnigent.stores.memory_store.embeddings import FastEmbedEmbedder

        return FastEmbedEmbedder()

    registry: PluggableRegistry[Embedder] = PluggableRegistry(
        EMBEDDER_SEAM, default=("fastembed", _fastembed)
    )
    # Extension discovery deferred to server startup (Wave-2 composition root):
    # it loads FastAPI-heavy entry-point extensions; keep off the import hot path.
    # Hook: 'memory_embedder_providers'.
    return registry


def select_default_embedder(engine: Any) -> Embedder | None:
    """Select the recall embedder for *engine*, or ``None`` for lexical recall.

    Byte-identical to the historical ``runtime._select_memory_embedder`` gate:
    semantic recall needs PostgreSQL **with the pgvector extension installed** (the
    migration only builds the ``vector`` column when pgvector is available). SQLite,
    and a Postgres without the extension, stay lexical — the embedding model is never
    loaded there and recall never casts a ``TEXT`` column to ``vector``. The only
    change is *where the concrete embedder comes from*: the embedder registry's
    active provider (default ``fastembed``) instead of a hard-coded constructor.
    """
    from omnigent.stores.memory_store.pgvector import pgvector_installed

    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as conn:
        if not pgvector_installed(conn):
            return None
    return build_embedder_registry().resolve_default()


# ── the default composed provider ────────────────────────────────────────────


class ComposedAgentMemoryProvider:
    """Default :class:`AgentMemoryProvider` — composes store + embedder + recall mode.

    Routes ALL access through the :class:`SqlAlchemyMemoryStore` (sub-seam #24 ABC),
    embeds via the embedder selected at construction (sub-seam #8), and exposes the
    semantic-vs-lexical recall mode (sub-seam #30). The store decides semantic vs
    lexical from whether an embedder is attached and the dialect is PostgreSQL, so a
    ``RecallMode.AUTO`` recall behaves exactly as the pre-port store did.
    """

    def __init__(self, store: SqlAlchemyMemoryStore) -> None:
        """:param store: The wired memory store (already carries its embedder)."""
        self._store = store

    @classmethod
    def from_location(
        cls,
        storage_location: str,
        *,
        embedder: Embedder | None = None,
    ) -> ComposedAgentMemoryProvider:
        """Build the default provider for a database URI.

        :param storage_location: SQLAlchemy URI (shared with the conversation store).
        :param embedder: Pre-selected embedder; when omitted, selected from the
            embedder registry via :func:`select_default_embedder` (lexical on
            SQLite / pgvector-less Postgres — byte-identical to the old gate).
        """
        from omnigent.db.utils import get_or_create_engine

        if embedder is None:
            embedder = select_default_embedder(get_or_create_engine(storage_location))
        return cls(SqlAlchemyMemoryStore(storage_location, embedder=embedder))

    @property
    def store(self) -> SqlAlchemyMemoryStore:
        """The underlying store (advisory-lock coordination, maintenance loop)."""
        return self._store

    def write(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        content: str,
        weight: float = 1.0,
        source_conversation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        return self._store.append(
            scope=scope,
            owner=owner,
            name=name,
            content=content,
            weight=weight,
            source_conversation_id=source_conversation_id,
            **kwargs,
        )

    def recall(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        query: str,
        k: int = 10,
        mode: RecallMode = RecallMode.AUTO,
        kind: str = "ambient",
    ) -> list[Memory]:
        # The store auto-selects semantic vs lexical from its embedder + dialect
        # (the historical AUTO behavior). An explicit LEXICAL forces keyword recall
        # even when an embedder is attached; SEMANTIC is only reachable when the
        # store actually has the embedder+pg substrate, otherwise it falls through
        # to the store's lexical path — never a crash. *kind* (BDP-2459) selects
        # ambient / addressable / all candidates.
        with self._store.recall_mode(_store_mode(mode)):
            return self._store.query(
                scope=scope, owner=owner, name=name, query=query, limit=k, kind=kind
            )

    def list_compartments(
        self, *, scope: str | None = None, owner: str | None = None
    ) -> list[dict]:
        return self._store.list_compartments(scope=scope, owner=owner)

    def note_recalled(self, hits: list[Memory]) -> None:
        self._store.note_recalled(hits)

    def health(self) -> dict[str, Any]:
        """Status dict for capability manifests (no I/O beyond cheap introspection)."""
        return {
            "provider": "composed",
            "dialect": self._store.engine.dialect.name,
            "embedder": (
                self._store.embedder.model_version
                if self._store.embedder is not None
                else None
            ),
            "recall_modes": [m.value for m in RecallMode],
        }


def _store_mode(mode: RecallMode) -> str | None:
    """Map a port :class:`RecallMode` to the store's override token.

    ``AUTO`` returns ``None`` (the store keeps its own embedder+dialect decision);
    ``SEMANTIC`` / ``LEXICAL`` force that branch.
    """
    if mode is RecallMode.AUTO:
        return None
    return mode.value


# ── the provider seam (#24, the coarse swap unit) ────────────────────────────

PROVIDER_SEAM = "agent_memory"


def build_memory_provider_registry(
    storage_location: str,
    *,
    embedder: Embedder | None = None,
) -> PluggableRegistry[AgentMemoryProvider]:
    """Registry over :class:`AgentMemoryProvider` for *storage_location*.

    Default = the in-tree :class:`ComposedAgentMemoryProvider`. Extensions
    contribute external providers (Pinecone/mem0/zep) via the
    ``agent_memory_providers`` hook; ``OMNIGENT_USE_AGENT_MEMORY`` pins one per env.
    The factory closes over *storage_location* so the selected provider is built for
    exactly this database.
    """

    def _composed() -> AgentMemoryProvider:
        return ComposedAgentMemoryProvider.from_location(
            storage_location, embedder=embedder
        )

    registry: PluggableRegistry[AgentMemoryProvider] = PluggableRegistry(
        PROVIDER_SEAM, default=("composed", _composed)
    )
    # Extension discovery deferred to server startup (Wave-2 composition root):
    # it loads FastAPI-heavy entry-point extensions; keep off the import hot path.
    # Hook: 'agent_memory_providers'.
    return registry


def create_agent_memory_provider(
    storage_location: str,
    *,
    embedder: Embedder | None = None,
) -> AgentMemoryProvider:
    """Resolve the active :class:`AgentMemoryProvider` for *storage_location*.

    The single composition-root entry point: builds the registry and resolves the
    active provider (``OMNIGENT_USE_AGENT_MEMORY`` override, else ``composed``).
    """
    return build_memory_provider_registry(
        storage_location, embedder=embedder
    ).resolve_default()


# Typing-only alias so callers can name a factory in their own signatures.
if TYPE_CHECKING:
    ProviderFactory = Callable[[], AgentMemoryProvider]


__all__ = [
    "AgentMemoryProvider",
    "ComposedAgentMemoryProvider",
    "Memory",
    "RecallMode",
    "EMBEDDER_SEAM",
    "PROVIDER_SEAM",
    "build_embedder_registry",
    "build_memory_provider_registry",
    "create_agent_memory_provider",
    "select_default_embedder",
]
