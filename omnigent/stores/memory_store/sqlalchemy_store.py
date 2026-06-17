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

import math
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

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI (the same engine the
            conversation store uses), e.g. ``"sqlite:///omnigent.db"`` or
            ``"postgresql+psycopg://user:pass@host/db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._is_sqlite = self._engine.dialect.name == "sqlite"
        ensure_memories_fts_table(self._engine)

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
        half_life_seconds: int | None = None,
        read_floor: float = _DEFAULT_READ_FLOOR,
        archive_floor: float = _DEFAULT_ARCHIVE_FLOOR,
        now: int | None = None,
    ) -> str:
        """Append a memory, creating its compartment on first use.

        :returns: The new memory id (``"mem_..."``).
        """
        now = now if now is not None else now_epoch()
        content = strip_nul_bytes(content)
        st = strip_nul_bytes(search_text if search_text is not None else content)
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
                )
            )
            insert_memory_fts(session, mid, comp.id, st)
        return mid

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

    # ── read (PURE — no writes on this path) ───────────────────────

    def query(
        self,
        *,
        scope: str,
        owner: str,
        name: str,
        query: str,
        limit: int = 10,
        now: int | None = None,
    ) -> list[MemoryHit]:
        """Recall memories from a compartment, ranked by decayed weight.

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
            candidate_ids = self._lexical_candidate_ids(
                session, comp.id, query, limit * 5
            )
            if not candidate_ids:
                return []
            rows = (
                session.execute(
                    select(SqlMemory).where(
                        SqlMemory.id.in_(candidate_ids),
                        SqlMemory.archived.is_(False),
                    )
                )
                .scalars()
                .all()
            )
            hits: list[MemoryHit] = []
            for r in rows:
                ew = _effective_weight(
                    r.weight, r.last_accessed_at, comp.half_life_seconds, now
                )
                if ew < comp.read_floor:
                    continue
                hits.append(
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
                    )
                )
            hits.sort(key=lambda h: h.effective_weight, reverse=True)
            return hits[:limit]

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
                    select(SqlMemory).where(SqlMemory.archived.is_(False))
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
