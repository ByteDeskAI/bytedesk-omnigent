"""Omnigent-native agent memory store (FU1, ADR-0132).

Durable, compartmented, weighted, decaying agent memory. Omnigent is the sole
writer of record; recall is lexical on SQLite (FTS5) and lexical-now /
semantic-ready on PostgreSQL (tsvector GIN + a ``vector(384)`` column the
migration adds). See :mod:`omnigent.stores.memory_store.sqlalchemy_store`.
"""

from omnigent.stores.memory_store.sqlalchemy_store import (
    MemoryHit,
    SqlAlchemyMemoryStore,
)

__all__ = ["MemoryHit", "SqlAlchemyMemoryStore"]
