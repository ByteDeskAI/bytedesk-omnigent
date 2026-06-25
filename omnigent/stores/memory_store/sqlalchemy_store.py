"""SQLAlchemy-backed omnigent-native agent memory store (FU1, ADR-0132).

Omnigent is the **sole writer of record** for durable agent memory. Memories
live in named, directly-queryable/appendable **compartments** scoped
``agent`` / ``team`` / ``topic`` (``tenant`` is deferred — omnigent has no
tenant identity). Each memory carries a float ``weight`` (salience) that
**decays** so stale memories fall off recall:

    effective_weight = weight * exp(-(now - last_accessed_at) / half_life_seconds)

``query`` is a **PURE READ**: it computes ``effective_weight`` for the lexical
candidate set, **drops** any below the compartment's ``read_floor`` (real
fall-off even when the sweep is down), and ranks by ``effective_weight``. It
performs no writes — reinforcement (touching ``last_accessed_at`` /
``access_count``) is applied out-of-band off the recall path (BDP-2147 T8).
``sweep`` archives memories whose effective weight is below ``archive_floor``
past a grace window (eviction, not mere re-ranking).

Recall is lexical here: SQLite FTS5 (``memories_fts``) and Postgres tsvector
(the GIN index the migration adds). The Postgres ``vector(384)`` semantic blend
(``cosine x weight x decay``) layers on top additively behind this same surface
(BDP-2147 T5).
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select, text, update

from omnigent.db.db_models import SqlMemory, SqlMemoryCompartment
from omnigent.db.utils import (
    ensure_memories_fts_table,
    generate_compartment_id,
    generate_memory_id,
    get_or_create_engine,
    insert_memory_fts,
    make_managed_session_maker,
    now_epoch,
    strip_nul_bytes,
)
from omnigent.stores.memory_store.embeddings import Embedder, format_vector

_logger = logging.getLogger(__name__)

_DAY = 86_400

# Default per-scope half-lives (seconds), ADR-0132. ``team`` (e.g. roster) is
# effectively non-decaying; ``agent`` / ``topic`` notes age out on a ~2-week
# scale. Callers (compaction-summary capture, fact-extraction) override per
# compartment at creation time.
_DEFAULT_HALF_LIFE: dict[str, int] = {
    "agent": 14 * _DAY,
    "topic": 14 * _DAY,
    "team": 3650 * _DAY,
}
_DEFAULT_READ_FLOOR = 0.1
_DEFAULT_ARCHIVE_FLOOR = 0.05
_DEFAULT_SWEEP_GRACE = 30 * _DAY
_VALID_SCOPES = frozenset({"agent", "team", "topic"})


@dataclass(frozen=True)
class MemoryHit:
    """A recalled memory with its decayed effective weight."""

    id: str
    compartment_id: str
    content: str
    weight: float
    effective_weight: float
    created_at: int
    last_accessed_at: int
    source_conversation_id: str | None
    source_compaction_id: str | None


def _effective_weight(
    weight: float, last_accessed_at: int, half_life_seconds: int, now: int
) -> float:
    """Decayed weight ``weight * exp(-age / half_life)``.

    A non-positive ``half_life_seconds`` is treated as non-decaying.

    :param weight: The stored salience.
    :param last_accessed_at: Unix epoch seconds of the last reinforcement.
    :param half_life_seconds: The compartment's decay constant.
    :param now: Current Unix epoch seconds.
    :returns: The effective (decayed) weight.
    """
    if half_life_seconds <= 0:
        return weight
    age = max(0, now - last_accessed_at)
    return weight * math.exp(-age / half_life_seconds)


class SqlAlchemyMemoryStore:
    """Durable compartmented weighted-decay memory; omnigent is the sole writer."""

    def __init__(self, storage_location: str, *, embedder: Embedder | None = None) -> None:
        """
        :param storage_location: SQLAlchemy database URI (the same engine the
            conversation store uses), e.g. ``"sqlite:///omnigent.db"`` or
            ``"postgresql+psycopg://user:pass@host/db"``.
        :param embedder: Optional embedding backend. When present, ``append``
            embeds-on-write and ``query`` uses semantic (pgvector) recall on
            PostgreSQL. ``None`` (the default, and the only path on SQLite)
            keeps recall lexical.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._is_sqlite = self._engine.dialect.name == "sqlite"
        self._embedder = embedder
        # Per-call recall-mode override (sub-seam #30, BDP-2369): ``None`` keeps the
        # historical embedder+dialect decision; ``"semantic"`` / ``"lexical"`` force
        # a branch for the duration of a :meth:`recall_mode` context.
        self._recall_mode_override: str | None = None
        ensure_memories_fts_table(self._engine)

    @property
    def engine(self):
        """The underlying SQLAlchemy engine (used for advisory-lock coordination)."""
        return self._engine

    @property
    def embedder(self) -> Embedder | None:
        """The attached recall embedder, or ``None`` for lexical-only recall."""
        return self._embedder

    @contextmanager
    def recall_mode(self, mode: str | None):
        """Force the recall branch for the duration of the block (sub-seam #30).

        :param mode: ``"semantic"`` / ``"lexical"`` to force that branch, or
            ``None`` to keep the default embedder+dialect auto-selection. Restores
            the prior override on exit (nesting-safe).
        """
        previous = self._recall_mode_override
        self._recall_mode_override = mode
        try:
            yield
        finally:
            self._recall_mode_override = previous

    # ── compartments ──────────────────────────────────────────────

    def _get_or_create_compartment(
        self,
        session,
        scope: str,
        owner: str,
        name: str,
        *,
        half_life_seconds: int | None,
        read_floor: float,
        archive_floor: float,
        now: int,
    ) -> SqlMemoryCompartment:
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"invalid memory scope {scope!r}; expected one of {sorted(_VALID_SCOPES)}"
            )
        row = session.execute(
            select(SqlMemoryCompartment).where(
                SqlMemoryCompartment.scope == scope,
                SqlMemoryCompartment.owner == owner,
                SqlMemoryCompartment.name == name,
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        row = SqlMemoryCompartment(
            id=generate_compartment_id(),
            scope=scope,
            owner=owner,
            name=name,
            half_life_seconds=(
                half_life_seconds
                if half_life_seconds is not None
                else _DEFAULT_HALF_LIFE.get(scope, 14 * _DAY)
            ),
            read_floor=read_floor,
            archive_floor=archive_floor,
            created_at=now,
        )
        session.add(row)
        session.flush()
        return row

    def list_compartments(
        self, *, scope: str | None = None, owner: str | None = None
    ) -> list[dict]:
        """Enumerate compartments, optionally filtered by ``scope`` / ``owner``."""
        with self._session() as session:
            stmt = select(SqlMemoryCompartment)
            if scope is not None:
                stmt = stmt.where(SqlMemoryCompartment.scope == scope)
            if owner is not None:
                stmt = stmt.where(SqlMemoryCompartment.owner == owner)
            rows = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "scope": r.scope,
                    "owner": r.owner,
                    "name": r.name,
                    "half_life_seconds": r.half_life_seconds,
                    "read_floor": r.read_floor,
                    "archive_floor": r.archive_floor,
                }
                for r in rows
            ]

    # ── write (sole writer of record) ─────────────────────────────

    def append(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        content: str,
        weight: float = 1.0,
        search_text: str | None = None,
        embedding: str | None = None,
        embedding_model_version: str | None = None,
        source_conversation_id: str | None = None,
        source_compaction_id: str | None = None,
        salience: float | None = None,
        confidence: float | None = None,
        metadata: str | None = None,
        key: str | None = None,
        half_life_seconds: int | None = None,
        read_floor: float = _DEFAULT_READ_FLOOR,
        archive_floor: float = _DEFAULT_ARCHIVE_FLOOR,
        now: int | None = None,
    ) -> str:
        """Append a memory, creating its compartment on first use.

        :param key: Optional addressable slot key (BDP-2457). A non-null key makes
            this row a deterministic exact-lookup SLOT excluded from similarity
            recall + decay sweep; the partial unique index enforces one live slot
            per (compartment, key). ``None`` (default) is an ordinary ambient
            memory — unchanged behaviour.
        :returns: The new memory id (``"mem_..."``).
        """
        now = now if now is not None else now_epoch()
        content = strip_nul_bytes(content)
        st = strip_nul_bytes(search_text if search_text is not None else content)
        # Embed-on-write (T5): semantic recall stores the vector alongside the
        # row. Best-effort — a failed embed never blocks the durable write
        # (recall falls back to lexical for that row).
        if embedding is None and self._embedder is not None:
            try:
                embedding = format_vector(self._embedder.embed([st])[0])
                embedding_model_version = (
                    embedding_model_version or self._embedder.model_version
                )
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "embed-on-write failed; storing memory without embedding",
                    exc_info=True,
                )
        with self._session() as session:
            comp = self._get_or_create_compartment(
                session,
                scope,
                owner,
                name,
                half_life_seconds=half_life_seconds,
                read_floor=read_floor,
                archive_floor=archive_floor,
                now=now,
            )
            mid = generate_memory_id()
            session.add(
                SqlMemory(
                    id=mid,
                    compartment_id=comp.id,
                    content=content,
                    search_text=st,
                    weight=weight,
                    created_at=now,
                    last_accessed_at=now,
                    access_count=0,
                    source_conversation_id=source_conversation_id,
                    source_compaction_id=source_compaction_id,
                    salience=salience,
                    confidence=confidence,
                    archived=False,
                    embedding=embedding,
                    embedding_model_version=embedding_model_version,
                    meta=metadata,
                    key=key,
                )
            )
            insert_memory_fts(session, mid, comp.id, st)
        return mid

    # ── addressable (keyed) slots (BDP-2457) ──────────────────────
    #
    # A keyed slot is a deterministic exact-lookup row (e.g. address
    # ``org:charter``) filtered on the indexed ``key`` column — never similarity-
    # recalled, never decay-swept (see :meth:`query` / the sweep). The partial
    # unique index ``uq_memories_compartment_key_live`` enforces one live slot per
    # (compartment, key); a keyed write is archive-prior + ``append(key=...)``.

    def _resolve_compartment_id(
        self, session, *, scope: str, owner: str, name: str
    ) -> str | None:
        """Resolve an EXISTING compartment id (read-only — never creates)."""
        return session.execute(
            select(SqlMemoryCompartment.id).where(
                SqlMemoryCompartment.scope == scope,
                SqlMemoryCompartment.owner == owner,
                SqlMemoryCompartment.name == name,
            )
        ).scalar_one_or_none()

    def get_keyed(self, *, scope: str, owner: str, name: str, key: str) -> dict | None:
        """Exact-key lookup of the live addressable slot, or ``None``.

        Filters on the indexed ``key`` column — deterministic, no similarity, no
        decay. The partial unique index guarantees at most one live slot; ordering
        by ``created_at`` desc is belt-and-suspenders mid-transition.
        """
        with self._session() as session:
            cid = self._resolve_compartment_id(session, scope=scope, owner=owner, name=name)
            if cid is None:
                return None
            row = (
                session.execute(
                    select(SqlMemory)
                    .where(
                        SqlMemory.compartment_id == cid,
                        SqlMemory.key == key,
                        SqlMemory.archived.is_(False),
                    )
                    .order_by(SqlMemory.created_at.desc())
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return {
                "memory_id": row.id,
                "content": row.content,
                "weight": row.weight,
                "created_at": row.created_at,
                "confidence": row.confidence,
                "source_conversation_id": row.source_conversation_id,
            }

    def archive_keyed(self, *, scope: str, owner: str, name: str, key: str) -> int:
        """Archive every live row at *key* in this compartment; returns the count.

        Used by ``unset`` and by keyed overwrite (archive-prior-then-write). Scoped
        to the resolved compartment, so a caller can only touch a slot in a
        compartment it can already address.
        """
        with self._session() as session:
            cid = self._resolve_compartment_id(session, scope=scope, owner=owner, name=name)
            if cid is None:
                return 0
            result = session.execute(
                update(SqlMemory)
                .where(
                    SqlMemory.compartment_id == cid,
                    SqlMemory.key == key,
                    SqlMemory.archived.is_(False),
                )
                .values(archived=True)
            )
        return result.rowcount or 0

    def list_keyed(self, *, scope: str, owner: str, name: str) -> list[dict]:
        """List every live addressable slot in a compartment (BDP-2459).

        Browse what's stored under an address prefix *without* a query —
        deterministic, no decay, newest first. Returns ``{key, content, weight}``
        per live keyed row.
        """
        with self._session() as session:
            cid = self._resolve_compartment_id(session, scope=scope, owner=owner, name=name)
            if cid is None:
                return []
            rows = (
                session.execute(
                    select(SqlMemory)
                    .where(
                        SqlMemory.compartment_id == cid,
                        SqlMemory.key.isnot(None),
                        SqlMemory.archived.is_(False),
                    )
                    .order_by(SqlMemory.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [{"key": r.key, "content": r.content, "weight": r.weight} for r in rows]

    # ── reinforcement (out-of-band; off the recall path) ──────────

    def reinforce(self, memory_ids: list[str], *, now: int | None = None) -> int:
        """Reset the decay clock for recalled memories (batched, out-of-band).

        Sets ``last_accessed_at = now`` and increments ``access_count`` for the
        given non-archived memories in a single batched UPDATE. Base ``weight``
        is intentionally **not** inflated — recall slows aging (clock reset)
        without immunizing a memory from decay (avoids the monotonic-up runaway;
        ADR-0132 / Hermes lesson). Invoked by the reinforcement-buffer flush off
        the recall path, never inline in :meth:`query`.

        :param memory_ids: Memory ids to reinforce.
        :param now: Current epoch seconds; defaults to :func:`now_epoch`.
        :returns: The number of rows updated.
        """
        ids = [m for m in memory_ids if m]
        if not ids:
            return 0
        now = now if now is not None else now_epoch()
        with self._session() as session:
            result = session.execute(
                update(SqlMemory)
                .where(SqlMemory.id.in_(ids), SqlMemory.archived.is_(False))
                .values(
                    last_accessed_at=now,
                    access_count=SqlMemory.access_count + 1,
                )
            )
        return result.rowcount or 0

    def note_recalled(
        self, hits: list[MemoryHit], *, now: int | None = None
    ) -> None:
        """Record recalled hits for out-of-band reinforcement (BDP-2369).

        Encapsulates the in-memory reinforcement-buffer write so callers (the
        ``memory_query`` tool) go through the store/port instead of reaching past
        it into ``db.utils`` + the buffer module directly. This stays a pure
        in-memory record — the durable clock reset happens later in the batched
        :meth:`reinforce` flush, keeping the recall path a pure DB read.

        :param hits: The hits a recall surfaced.
        :param now: Current epoch seconds; defaults to :func:`now_epoch`.
        """
        if not hits:
            return
        from omnigent.stores.memory_store.reinforcement import get_reinforcement_buffer

        now = now if now is not None else now_epoch()
        get_reinforcement_buffer().record([h.id for h in hits], now=now)

    # ── read (PURE — no writes on this path) ───────────────────────

    def query(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        query: str,
        limit: int = 10,
        kind: str = "ambient",
        now: int | None = None,
    ) -> list[MemoryHit]:
        """Recall memories from a compartment, ranked by decayed weight.

        *kind* (BDP-2459) selects which rows are search candidates: ``"ambient"``
        = key-NULL decaying memories (the default / historical behaviour);
        ``"addressable"`` = keyed slots only; ``"all"`` = both. Keyed slots are
        embedded + FTS-indexed like any row, so semantic/keyword search spans
        them once they are not filtered out.

        Pure read: lexical candidate set within the compartment, decayed by
        ``effective_weight``, sub-``read_floor`` rows dropped, ranked by
        effective weight. Returns at most ``limit`` hits. No row is mutated.
        """
        now = now if now is not None else now_epoch()
        with self._session() as session:
            comp = session.execute(
                select(SqlMemoryCompartment).where(
                    SqlMemoryCompartment.scope == scope,
                    SqlMemoryCompartment.owner == owner,
                    SqlMemoryCompartment.name == name,
                )
            ).scalar_one_or_none()
            if comp is None:
                return []
            # id -> relevance (1.0 lexical; cosine similarity on the PG
            # semantic path). Composite recall score = relevance x decayed
            # weight; the read floor drops sub-floor rows (real fall-off).
            candidates = self._candidates(session, comp, query, limit * 5)
            if not candidates:
                return []
            # Keyed slots were excluded from recall in BDP-2457; BDP-2459 makes
            # that a *kind* choice so search can span ambient + addressable.
            where_clauses = [
                SqlMemory.id.in_(list(candidates)),
                SqlMemory.archived.is_(False),
            ]
            if kind == "ambient":
                where_clauses.append(SqlMemory.key.is_(None))
            elif kind == "addressable":
                where_clauses.append(SqlMemory.key.isnot(None))
            # kind == "all": no key filter — both ambient and keyed rows.
            rows = (
                session.execute(select(SqlMemory).where(*where_clauses))
                .scalars()
                .all()
            )
            scored: list[tuple[float, MemoryHit]] = []
            dropped_sub_floor = 0
            for r in rows:
                ew = _effective_weight(
                    r.weight, r.last_accessed_at, comp.half_life_seconds, now
                )
                composite = candidates.get(r.id, 1.0) * ew
                if composite < comp.read_floor:
                    dropped_sub_floor += 1
                    continue
                scored.append(
                    (
                        composite,
                        MemoryHit(
                            id=r.id,
                            compartment_id=r.compartment_id,
                            content=r.content,
                            weight=r.weight,
                            effective_weight=ew,
                            created_at=r.created_at,
                            last_accessed_at=r.last_accessed_at,
                            source_conversation_id=r.source_conversation_id,
                            source_compaction_id=r.source_compaction_id,
                        ),
                    )
                )
            scored.sort(key=lambda s: s[0], reverse=True)
            returned = [hit for _, hit in scored[:limit]]
            # Recall observability (T13): counts make decay/floor tuning
            # falsifiable rather than guessed (ADR-0132 / Hermes lesson).
            mode = "semantic" if self._use_semantic() else "lexical"
            _logger.info(
                "memory_query scope=%s owner=%s name=%s mode=%s candidates=%d "
                "considered=%d dropped_sub_floor=%d returned=%d",
                scope,
                owner,
                name,
                mode,
                len(candidates),
                len(rows),
                dropped_sub_floor,
                len(returned),
            )
            return returned

    def list_by_source_conversation(
        self,
        source_conversation_id: str,
        *,
        include_archived: bool = False,
        limit: int = 50,
        sort_by: str = "created_at",
    ) -> list[SqlMemory]:
        """List memories captured from a session, newest-first (PURE READ).

        Unlike :meth:`query`, this is a direct lookup by the
        ``source_conversation_id`` column rather than a compartment + lexical
        recall — it answers "what did this session contribute to memory?"
        without a query string or decay ranking. No row is mutated. Used by the
        read-only ``GET /v1/sessions/{id}/memories`` data surface (Phase 9a).

        :param source_conversation_id: Session/conversation id the memories
            were captured from, e.g. ``"conv_abc123"``.
        :param include_archived: When ``False`` (the default), archived
            (evicted) memories are excluded.
        :param limit: Maximum number of rows to return (the route bounds it).
        :param sort_by: Column to order by, ``"created_at"`` (default) or
            ``"weight"``; anything else falls back to ``"created_at"``.
        :returns: Detached :class:`SqlMemory` rows ordered newest/heaviest
            first. Empty when the session captured no memories.
        """
        order_col = SqlMemory.weight if sort_by == "weight" else SqlMemory.created_at
        with self._session() as session:
            stmt = select(SqlMemory).where(
                SqlMemory.source_conversation_id == source_conversation_id
            )
            if not include_archived:
                stmt = stmt.where(SqlMemory.archived.is_(False))
            stmt = stmt.order_by(order_col.desc()).limit(limit)
            rows = session.execute(stmt).scalars().all()
            # Detach so callers can read attributes after the session closes.
            for row in rows:
                session.expunge(row)
            return list(rows)

    def _use_semantic(self) -> bool:
        """Whether this recall should use the semantic (pgvector) branch.

        Default decision (override unset): semantic only when an embedder is
        attached and the dialect is PostgreSQL — the historical gate. A
        :meth:`recall_mode` override forces ``semantic`` / ``lexical``; a forced
        ``semantic`` is still only honored when the embedder+dialect substrate
        exists (otherwise the store cannot cast a ``TEXT`` column to ``vector``), so
        it degrades to lexical rather than crashing.
        """
        substrate = self._embedder is not None and not self._is_sqlite
        override = self._recall_mode_override
        if override == "lexical":
            return False
        if override == "semantic":
            return substrate
        return substrate

    def _candidates(self, session, comp, query: str, limit: int) -> dict[str, float]:
        """Candidate memory ids -> relevance for *comp*.

        Semantic (pgvector cosine similarity) when :meth:`_use_semantic`; otherwise
        lexical (relevance 1.0).
        """
        if self._use_semantic():
            return self._semantic_candidates(session, comp.id, query, limit)
        return {mid: 1.0 for mid in self._lexical_candidate_ids(session, comp.id, query, limit)}

    def _semantic_candidates(
        self, session, compartment_id: str, query: str, limit: int
    ) -> dict[str, float]:
        """PostgreSQL pgvector cosine retrieval — id -> cosine similarity.

        Uses the ivfflat index the migration adds. Verified by the opt-in
        Postgres integration suite + the in-cluster slice proof (T14);
        unreachable on SQLite (guarded by the dialect check in
        :meth:`_candidates`).
        """
        assert self._embedder is not None  # guarded by _candidates
        qvec = format_vector(self._embedder.embed([query])[0])
        stmt = text(
            "SELECT id, 1 - (embedding <=> CAST(:qvec AS vector)) AS sim "
            "FROM memories "
            "WHERE compartment_id = :cid AND archived = false "
            "AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :lim"
        )
        rows = session.execute(
            stmt, {"qvec": qvec, "cid": compartment_id, "lim": limit}
        ).fetchall()
        return {row[0]: float(row[1]) for row in rows}

    def _lexical_candidate_ids(
        self, session, compartment_id: str, query: str, limit: int
    ) -> list[str]:
        """Lexical match within a compartment (SQLite FTS5 / Postgres tsvector)."""
        if self._is_sqlite:
            stmt = text(
                "SELECT memory_id FROM memories_fts "
                "WHERE compartment_id = :cid AND search_text MATCH :q "
                "ORDER BY rank LIMIT :lim"
            )
            return [
                row[0]
                for row in session.execute(
                    stmt, {"cid": compartment_id, "q": query, "lim": limit}
                ).fetchall()
            ]
        # Postgres: tsvector match against the GIN index the migration adds.
        stmt = text(
            "SELECT id FROM memories "
            "WHERE compartment_id = :cid AND archived = false "
            "AND to_tsvector('english', coalesce(search_text, '')) "
            "@@ plainto_tsquery('english', :q) "
            "ORDER BY created_at DESC LIMIT :lim"
        )
        return [
            row[0]
            for row in session.execute(
                stmt, {"cid": compartment_id, "q": query, "lim": limit}
            ).fetchall()
        ]

    # ── decay / eviction sweep ─────────────────────────────────────

    def sweep(
        self, *, now: int | None = None, grace_seconds: int = _DEFAULT_SWEEP_GRACE
    ) -> int:
        """Archive memories whose effective weight is below ``archive_floor``
        and older than ``grace_seconds`` — real eviction from recall.

        :returns: The number of memories archived this sweep.
        """
        now = now if now is not None else now_epoch()
        archived = 0
        with self._session() as session:
            comps = {
                c.id: c
                for c in session.execute(select(SqlMemoryCompartment)).scalars().all()
            }
            rows = (
                session.execute(
                    select(SqlMemory).where(
                        SqlMemory.archived.is_(False),
                        # Addressable keyed slots (BDP-2457) are durable, deliberate
                        # references — never decay-evicted. Sweep ambient rows only.
                        SqlMemory.key.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                comp = comps.get(r.compartment_id)
                if comp is None:
                    continue
                ew = _effective_weight(
                    r.weight, r.last_accessed_at, comp.half_life_seconds, now
                )
                if ew < comp.archive_floor and (now - r.created_at) > grace_seconds:
                    r.archived = True
                    archived += 1
        return archived

    def exists_for_compaction(self, compaction_id: str) -> bool:
        """Whether a memory already captured this compaction item (dedup, T10)."""
        if not compaction_id:
            return False
        with self._session() as session:
            row = session.execute(
                select(SqlMemory.id)
                .where(SqlMemory.source_compaction_id == compaction_id)
                .limit(1)
            ).first()
            return row is not None
