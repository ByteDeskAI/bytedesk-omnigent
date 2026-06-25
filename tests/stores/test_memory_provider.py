"""Tests for the first-class pluggable agent-memory port (BDP-2369, ADR-0132).

Covers:

* a write→recall **conformance suite** parametrized over every provider impl
  (the in-tree composed provider + an in-memory FAKE), proving the Protocol is
  genuinely swappable;
* the embedder registry default selection (sub-seam #8);
* the recall-mode strategy both branches (sub-seam #30);
* the provider registry default + ``OMNIGENT_USE_AGENT_MEMORY`` override.

The in-memory fake never touches a database — that it satisfies the same
conformance suite is the swappability proof.
"""

from __future__ import annotations

import pytest

from omnigent.kernel.pluggable import PluggableRegistry
from omnigent.stores.memory_store import (
    AgentMemoryProvider,
    ComposedAgentMemoryProvider,
    Memory,
    RecallMode,
    SqlAlchemyMemoryStore,
    build_embedder_registry,
    build_memory_provider_registry,
    create_agent_memory_provider,
)
from omnigent.stores.memory_store.embeddings import EMBEDDING_DIM, FastEmbedEmbedder


# ── an in-memory FAKE provider (the swappability proof) ──────────────────────


class _FakeMemoryProvider:
    """A fully in-memory :class:`AgentMemoryProvider` — no DB, no store, no embedder.

    Lexical-substring recall over an in-process list. Exists to prove the Protocol
    is swappable: a backend that ignores the store/embedder/recall sub-seams still
    satisfies the same conformance suite.
    """

    def __init__(self) -> None:
        self._rows: list[Memory] = []
        self._noted: list[str] = []
        self._seq = 0

    def write(self, *, scope, owner, name, content, weight=1.0,
              source_conversation_id=None, **kwargs) -> str:
        self._seq += 1
        mid = f"fake_{self._seq}"
        self._rows.append(
            Memory(
                id=mid,
                compartment_id=f"{scope}:{owner}:{name}",
                content=content,
                weight=weight,
                effective_weight=weight,
                created_at=0,
                last_accessed_at=0,
                source_conversation_id=source_conversation_id,
                source_compaction_id=None,
            )
        )
        return mid

    def recall(self, *, scope, owner, name, query, k=10, mode=RecallMode.AUTO):
        cid = f"{scope}:{owner}:{name}"
        hits = [
            r for r in self._rows
            if r.compartment_id == cid and query.lower() in r.content.lower()
        ]
        return hits[:k]

    def list_compartments(self, *, scope=None, owner=None):
        out = []
        seen = set()
        for r in self._rows:
            s, o, n = r.compartment_id.split(":", 2)
            if scope is not None and s != scope:
                continue
            if owner is not None and o != owner:
                continue
            if (s, o, n) not in seen:
                seen.add((s, o, n))
                out.append({"scope": s, "owner": o, "name": n})
        return out

    def note_recalled(self, hits) -> None:
        self._noted.extend(h.id for h in hits)

    def health(self):
        return {"provider": "fake", "rows": len(self._rows)}


# ── provider factories under conformance ─────────────────────────────────────


def _composed(tmp_path) -> ComposedAgentMemoryProvider:
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'prov.db'}")
    return ComposedAgentMemoryProvider(store)


def _fake(_tmp_path) -> _FakeMemoryProvider:
    return _FakeMemoryProvider()


PROVIDER_FACTORIES = [_composed, _fake]


@pytest.fixture(params=PROVIDER_FACTORIES, ids=["composed", "fake"])
def provider(request, tmp_path) -> AgentMemoryProvider:
    return request.param(tmp_path)


# ── conformance suite (every provider passes) ────────────────────────────────


def test_provider_satisfies_protocol(provider) -> None:
    assert isinstance(provider, AgentMemoryProvider)


def test_write_then_recall_roundtrip(provider) -> None:
    mid = provider.write(scope="agent", owner="ag_m", name="notes",
                         content="Ryan prefers fastembed")
    assert isinstance(mid, str) and mid
    hits = provider.recall(scope="agent", owner="ag_m", name="notes", query="fastembed")
    assert len(hits) == 1
    assert "fastembed" in hits[0].content


def test_recall_empty_compartment_is_empty(provider) -> None:
    assert provider.recall(scope="agent", owner="nobody", name="notes", query="x") == []


def test_recall_respects_k(provider) -> None:
    for i in range(5):
        provider.write(scope="topic", owner="shared", name="t", content=f"alpha {i}")
    assert len(provider.recall(scope="topic", owner="shared", name="t",
                               query="alpha", k=3)) == 3


def test_list_compartments_round_trips(provider) -> None:
    provider.write(scope="agent", owner="ag_m", name="notes", content="x")
    comps = provider.list_compartments(scope="agent", owner="ag_m")
    assert any(c["name"] == "notes" for c in comps)


def test_note_recalled_is_accepted(provider) -> None:
    provider.write(scope="agent", owner="ag_m", name="notes", content="alpha")
    hits = provider.recall(scope="agent", owner="ag_m", name="notes", query="alpha")
    provider.note_recalled(hits)  # must not raise on any provider


def test_health_returns_dict(provider) -> None:
    assert isinstance(provider.health(), dict)


# ── embedder registry (sub-seam #8) ──────────────────────────────────────────


def test_embedder_registry_default_is_fastembed() -> None:
    reg = build_embedder_registry()
    assert isinstance(reg, PluggableRegistry)
    assert reg.describe()["default"] == "fastembed"
    assert reg.describe()["active"] == "fastembed"


def test_embedder_registry_resolves_fastembed_without_loading_model() -> None:
    # resolve_default constructs the embedder but FastEmbedEmbedder is lazy — the
    # ONNX model only downloads on first .embed(), so construction stays cheap and
    # the dim is the unchanged 384 (a dim change would need a vector(N) migration).
    embedder = build_embedder_registry().resolve_default()
    assert isinstance(embedder, FastEmbedEmbedder)
    assert embedder.dim == EMBEDDING_DIM == 384


def test_embedder_registry_override_selects_named(monkeypatch) -> None:
    class _Stub:
        dim = 384
        model_version = "stub"

        def embed(self, texts):
            return [[0.0] * 384 for _ in texts]

    reg = build_embedder_registry()
    reg.register("stub", _Stub)
    monkeypatch.setenv("OMNIGENT_USE_MEMORY_EMBEDDER", "stub")
    assert isinstance(reg.resolve_default(), _Stub)


# ── recall-mode strategy (sub-seam #30) ──────────────────────────────────────


def test_recall_mode_lexical_branch_on_sqlite(tmp_path) -> None:
    """On SQLite both AUTO and LEXICAL take the lexical branch (no embedder)."""
    p = _composed(tmp_path)
    p.write(scope="agent", owner="ag_m", name="notes", content="alpha beta")
    for mode in (RecallMode.AUTO, RecallMode.LEXICAL):
        hits = p.recall(scope="agent", owner="ag_m", name="notes",
                        query="alpha", mode=mode)
        assert len(hits) == 1


def test_recall_mode_semantic_degrades_to_lexical_without_substrate(tmp_path) -> None:
    """SEMANTIC requested on SQLite (no embedder/pg) must not crash — it falls
    through to lexical rather than casting TEXT to vector."""
    p = _composed(tmp_path)
    p.write(scope="agent", owner="ag_m", name="notes", content="alpha beta")
    hits = p.recall(scope="agent", owner="ag_m", name="notes",
                    query="alpha", mode=RecallMode.SEMANTIC)
    assert len(hits) == 1


def test_recall_mode_override_is_restored(tmp_path) -> None:
    """The recall_mode context manager restores the prior override on exit."""
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'rm.db'}")
    assert store._recall_mode_override is None
    with store.recall_mode("lexical"):
        assert store._recall_mode_override == "lexical"
        with store.recall_mode("semantic"):
            assert store._recall_mode_override == "semantic"
        assert store._recall_mode_override == "lexical"
    assert store._recall_mode_override is None


def test_store_use_semantic_gate(tmp_path) -> None:
    """The store's semantic gate: lexical on SQLite even with an embedder forced."""
    class _Emb:
        dim = 384
        model_version = "x"

        def embed(self, texts):
            return [[0.0] * 384 for _ in texts]

    sqlite_store = SqlAlchemyMemoryStore(
        f"sqlite:///{tmp_path / 'g.db'}", embedder=_Emb()
    )
    # SQLite never goes semantic even with an embedder attached.
    assert sqlite_store._use_semantic() is False
    with sqlite_store.recall_mode("semantic"):
        assert sqlite_store._use_semantic() is False  # substrate missing → degrade
    with sqlite_store.recall_mode("lexical"):
        assert sqlite_store._use_semantic() is False


# ── provider registry (sub-seam #24, the coarse swap unit) ───────────────────


def test_provider_registry_default_is_composed(tmp_path) -> None:
    reg = build_memory_provider_registry(f"sqlite:///{tmp_path / 'p.db'}")
    assert reg.describe()["default"] == "composed"
    assert isinstance(reg.resolve_default(), ComposedAgentMemoryProvider)


def test_create_agent_memory_provider_returns_composed(tmp_path) -> None:
    p = create_agent_memory_provider(f"sqlite:///{tmp_path / 'p.db'}")
    assert isinstance(p, ComposedAgentMemoryProvider)


def test_provider_registry_override_selects_external(tmp_path, monkeypatch) -> None:
    """OMNIGENT_USE_AGENT_MEMORY swaps the whole backend to a registered external."""
    reg = build_memory_provider_registry(f"sqlite:///{tmp_path / 'p.db'}")
    reg.register("fake", _FakeMemoryProvider)
    monkeypatch.setenv("OMNIGENT_USE_AGENT_MEMORY", "fake")
    assert isinstance(reg.resolve_default(), _FakeMemoryProvider)
