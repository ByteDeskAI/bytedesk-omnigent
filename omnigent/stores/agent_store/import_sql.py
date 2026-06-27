"""One-time SQL-to-AgentStore import utility."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import Automation
from omnigent.stores.agent_store import AgentStore


@dataclass(frozen=True)
class AgentImportReport:
    """Summary of a SQL AgentStore import run."""

    imported: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _LegacyAgentSnapshot:
    agent: Automation
    sot_tier: str | None
    capabilities: tuple[str, ...]
    category: str | None


def import_sql_agents(db_uri: str, agent_store: AgentStore) -> AgentImportReport:
    """Copy legacy SQL ``agents`` rows into the active AgentStore.

    Existing identical records are skipped. Existing records with different
    material fields are reported as conflicts and left untouched.
    """
    engine = get_or_create_engine(db_uri)
    session_factory = make_managed_session_maker(engine)
    imported = 0
    skipped = 0
    conflicts: list[str] = []
    with session_factory() as session:
        snapshots = [
            _snapshot(row)
            for row in session.execute(select(SqlAgent).order_by(SqlAgent.created_at)).scalars()
        ]
    for snapshot in snapshots:
        source = snapshot.agent
        existing = agent_store.get(source.id)
        if existing is not None:
            if _same_agent(existing, source) and _same_metadata(
                agent_store,
                source.id,
                snapshot,
            ):
                skipped += 1
            else:
                conflicts.append(source.id)
            continue
        agent_store.create(
            agent_id=source.id,
            name=source.name,
            bundle_location=source.bundle_location,
            description=source.description,
            session_id=source.session_id,
        )
        if snapshot.sot_tier:
            agent_store.set_sot_tier(source.id, snapshot.sot_tier)
        if snapshot.capabilities:
            agent_store.set_capabilities(source.id, snapshot.capabilities)
        if snapshot.category:
            agent_store.set_category(source.id, snapshot.category)
        imported += 1
    return AgentImportReport(imported=imported, skipped=skipped, conflicts=conflicts)


def _snapshot(row: SqlAgent) -> _LegacyAgentSnapshot:
    return _LegacyAgentSnapshot(
        agent=sql_agent_to_entity(row),
        sot_tier=row.sot_tier,
        capabilities=_decode_capabilities(row.capabilities),
        category=row.category,
    )


def _same_agent(left: Automation, right: Automation) -> bool:
    return (
        left.name == right.name
        and left.bundle_location == right.bundle_location
        and left.description == right.description
        and left.session_id == right.session_id
    )


def _same_metadata(
    agent_store: AgentStore,
    agent_id: str,
    snapshot: _LegacyAgentSnapshot,
) -> bool:
    return (
        agent_store.get_sot_tier(agent_id) == snapshot.sot_tier
        and agent_store.get_capabilities(agent_id) == snapshot.capabilities
        and agent_store.get_category(agent_id) == snapshot.category
    )


def _decode_capabilities(raw: str | None) -> tuple[str, ...]:
    import json

    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, str))
