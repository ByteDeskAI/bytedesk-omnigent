"""Embedding provider for semantic memory recall (FU1 T5, ADR-0132).

Self-contained, in-process embeddings — no external API, no credential, no
outbound dependency (the hard requirement that all agent memory lives in the
omnigent ecosystem). The default :class:`FastEmbedEmbedder` runs
``BAAI/bge-small-en-v1.5`` (384-dim) on CPU via ``fastembed`` (ONNX, no torch).
The model downloads once on first use and is cached; ``fastembed`` is an
optional dependency (the ``memory`` extra), imported lazily so this module
loads even where it is not installed (e.g. SQLite dev/tests use a fake
embedder, or no embedder at all).

The :class:`Embedder` protocol is the pluggable seam: lexical-now / semantic
behind one surface, and the backend (fastembed today, a different model or a
hosted API later) can be swapped without touching the store or the agents.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODEL_VERSION = "bge-small-en-v1.5"
EMBEDDING_DIM = 384


@runtime_checkable
class Embedder(Protocol):
    """Pluggable text-embedding backend for the memory plane."""

    dim: int
    model_version: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text into a ``dim``-length float vector."""
        ...


def format_vector(vector: list[float]) -> str:
    """Render a float vector as a pgvector literal, e.g. ``"[0.1,0.2,0.3]"``.

    Stored verbatim in the ``embedding`` column — a ``vector(384)`` column on
    PostgreSQL (the literal casts directly) and a portable ``TEXT`` column on
    SQLite.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class FastEmbedEmbedder:
    """``BAAI/bge-small-en-v1.5`` (384-dim, CPU) via ``fastembed`` (lazy load)."""

    dim = EMBEDDING_DIM
    model_version = EMBEDDING_MODEL_VERSION

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model_name = model_name
        self._model = None  # lazily constructed on first embed (downloads once)

    def _ensure_model(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - exercised in deploy
                raise RuntimeError(
                    "fastembed is required for semantic memory recall; install "
                    "the 'memory' extra (uv sync --extra memory)."
                ) from exc
            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*; returns one ``dim``-length vector per input."""
        model = self._ensure_model()
        return [list(map(float, vec)) for vec in model.embed(texts)]
