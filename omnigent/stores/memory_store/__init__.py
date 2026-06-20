"""Omnigent-native agent memory store (FU1, ADR-0132).

Durable, compartmented, weighted, decaying agent memory. Omnigent is the sole
writer of record; recall is lexical on SQLite (FTS5) and lexical-now /
semantic-ready on PostgreSQL (tsvector GIN + a ``vector(384)`` column the
migration adds). See :mod:`omnigent.stores.memory_store.sqlalchemy_store`.
"""

from omnigent.stores.memory_store.provider import (
    AgentMemoryProvider,
    ComposedAgentMemoryProvider,
    Memory,
    RecallMode,
    build_embedder_registry,
    build_memory_provider_registry,
    create_agent_memory_provider,
    select_default_embedder,
)
from omnigent.stores.memory_store.reinforcement import (
    ReinforcementBuffer,
    get_reinforcement_buffer,
)
from omnigent.stores.memory_store.sqlalchemy_store import (
    MemoryHit,
    SqlAlchemyMemoryStore,
)

__all__ = [
    "AgentMemoryProvider",
    "ComposedAgentMemoryProvider",
    "Memory",
    "MemoryHit",
    "RecallMode",
    "ReinforcementBuffer",
    "SqlAlchemyMemoryStore",
    "build_embedder_registry",
    "build_memory_provider_registry",
    "create_agent_memory_provider",
    "get_reinforcement_buffer",
    "select_default_embedder",
]
