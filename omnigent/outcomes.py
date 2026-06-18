"""Business Outcome Ledger (BDP-2268 B7, ADR-0142).

An append-only ledger of attributed business outcomes — a won deal, a resolved
ticket, a shipped feature — that the org *learns* from. Recording an outcome
both appends the ledger row AND upserts the agent's cumulative
``scoreboard_entries`` value for the outcome's metric (one guarded transaction),
so find-specialist ranking + the accountability loop reflect what actually
worked. Mirrors the goals/peer single-writer store shape (``omnigent/goals.py``,
``omnigent/peer.py``); shares the conversation store's engine.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from omnigent.db.db_models import SqlBusinessOutcome, SqlScoreboardEntry
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class BusinessOutcome:
    """A row of ``business_outcomes``."""

    id: str
    agent_id: str
    kind: str
    metric: str
    value: float
    ref: str | None
    created_at: int


def _to_outcome(row: SqlBusinessOutcome) -> BusinessOutcome:
    return BusinessOutcome(
        id=row.id,
        agent_id=row.agent_id,
        kind=row.kind,
        metric=row.metric,
        value=row.value,
        ref=row.ref,
        created_at=row.created_at,
    )


class SqlAlchemyOutcomeLedger:
    """Durable business-outcome ledger + scoreboard rollup (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def record_outcome(
        self,
        *,
        agent_id: str,
        kind: str,
        metric: str,
        value: float = 1.0,
        ref: str | None = None,
        meta: dict | None = None,
        now: int | None = None,
    ) -> BusinessOutcome:
        """Append an outcome and roll its cumulative total into the scoreboard.

        One guarded write: insert the ledger row, recompute the agent's cumulative
        sum for ``metric``, and upsert the ``(agent_id, metric, 'all')``
        scoreboard entry to that total — so find-specialist ranks by real results.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlBusinessOutcome(
                id=f"out_{uuid.uuid4().hex}",
                agent_id=agent_id,
                kind=kind,
                metric=metric,
                value=value,
                ref=ref,
                created_at=now,
                meta=json.dumps(meta) if meta is not None else None,
            )
            session.add(row)
            session.flush()

            total = session.execute(
                select(func.coalesce(func.sum(SqlBusinessOutcome.value), 0.0)).where(
                    SqlBusinessOutcome.agent_id == agent_id,
                    SqlBusinessOutcome.metric == metric,
                )
            ).scalar_one()

            entry = session.get(SqlScoreboardEntry, (agent_id, metric, "all"))
            if entry is not None:
                entry.value = float(total)
                entry.updated_at = now
            else:
                session.add(
                    SqlScoreboardEntry(
                        agent_id=agent_id,
                        metric=metric,
                        window="all",
                        value=float(total),
                        updated_at=now,
                    )
                )
            session.flush()
            return _to_outcome(row)

    def list_outcomes(
        self, *, agent_id: str | None = None, kind: str | None = None, limit: int = 100
    ) -> list[BusinessOutcome]:
        """List outcomes (newest first), optionally filtered by agent / kind."""
        stmt = select(SqlBusinessOutcome)
        if agent_id is not None:
            stmt = stmt.where(SqlBusinessOutcome.agent_id == agent_id)
        if kind is not None:
            stmt = stmt.where(SqlBusinessOutcome.kind == kind)
        stmt = stmt.order_by(SqlBusinessOutcome.created_at.desc()).limit(limit)
        with self._session() as session:
            return [_to_outcome(r) for r in session.execute(stmt).scalars().all()]

    def leaderboard(self, *, metric: str, limit: int = 10) -> list[tuple[str, float]]:
        """Return ``(agent_id, cumulative_value)`` for a metric, ranked desc."""
        stmt = (
            select(
                SqlBusinessOutcome.agent_id,
                func.sum(SqlBusinessOutcome.value).label("total"),
            )
            .where(SqlBusinessOutcome.metric == metric)
            .group_by(SqlBusinessOutcome.agent_id)
            .order_by(func.sum(SqlBusinessOutcome.value).desc())
            .limit(limit)
        )
        with self._session() as session:
            return [(r.agent_id, float(r.total)) for r in session.execute(stmt).all()]


_outcome_ledger_cache: dict[str, SqlAlchemyOutcomeLedger] = {}


def get_outcome_ledger() -> SqlAlchemyOutcomeLedger:
    """Return the durable business-outcome ledger (BDP-2268 B7, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    ledger = _outcome_ledger_cache.get(location)
    if ledger is None:
        ledger = SqlAlchemyOutcomeLedger(location)
        _outcome_ledger_cache[location] = ledger
    return ledger
