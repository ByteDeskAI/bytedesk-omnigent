"""Tests for FU1 semantic embed-on-write + the pluggable embedder (BDP-2147 T5).

The PostgreSQL pgvector ``<=>`` recall path is verified by the opt-in Postgres
integration suite + the in-cluster slice proof (T14); here on SQLite we verify
the dialect-agnostic parts: embed-on-write storage, the embedder seam, and that
an attached embedder still falls back to lexical recall on SQLite.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory
from omnigent.stores.memory_store import SqlAlchemyMemoryStore
from omnigent.stores.memory_store.embeddings import (
    EMBEDDING_DIM,
    FastEmbedEmbedder,
    format_vector,
)


class _FakeEmbedder:
    """Deterministic offline embedder (no model download)."""

    dim = EMBEDDING_DIM
    model_version = "fake-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            seed = sum(ord(c) for c in t)
            out.append([float((seed + i) % 7) / 7.0 for i in range(self.dim)])
        return out


def _engine(tmp_path):
    return sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")


def test_format_vector_pgvector_literal() -> None:
    assert format_vector([0.5, 1.0]) == "[0.5,1.0]"


def test_fastembed_embedder_metadata_without_download() -> None:
    # Constructing must not download the model; dim/version are static.
    fe = FastEmbedEmbedder()
    assert fe.dim == EMBEDDING_DIM == 384
    assert fe.model_version == "bge-small-en-v1.5"


def test_embed_on_write_populates_embedding(tmp_path) -> None:
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}", embedder=_FakeEmbedder())
    mid = store.append(scope="agent", owner="m", name="n", content="hello world")
    with Session(_engine(tmp_path)) as s:
        row = s.get(SqlMemory, mid)
        assert row.embedding is not None and row.embedding.startswith("[")
        assert row.embedding.count(",") == EMBEDDING_DIM - 1  # 384 components
        assert row.embedding_model_version == "fake-v1"


def test_no_embedder_leaves_embedding_null(tmp_path) -> None:
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")
    mid = store.append(scope="agent", owner="m", name="n", content="hello world")
    with Session(_engine(tmp_path)) as s:
        assert s.get(SqlMemory, mid).embedding is None


def test_embedder_on_sqlite_still_recalls_lexically(tmp_path) -> None:
    # An attached embedder must not break SQLite: the semantic path is gated to
    # Postgres, so recall falls back to lexical FTS5 and still works.
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}", embedder=_FakeEmbedder())
    store.append(scope="agent", owner="m", name="n", content="alpha beta gamma")
    hits = store.query(scope="agent", owner="m", name="n", query="alpha")
    assert len(hits) == 1
