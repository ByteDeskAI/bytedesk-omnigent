"""Per-agent SoT tier marker (BDP-2149, ADR-0133/0136).

The mutable flip-able cutover marker lives on the agents row (params are
immutable), written via AgentStore — not inferred from registry presence.
"""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.db.utils import generate_agent_id, get_or_create_engine
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def test_sot_tier_column_exists(tmp_path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    cols = {c["name"] for c in sa.inspect(engine).get_columns("agents")}
    assert "sot_tier" in cols


def test_sot_tier_defaults_none_and_round_trips(tmp_path) -> None:
    store = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'a.db'}")
    agent_id = generate_agent_id()
    store.create(agent_id, name="chief-of-staff", bundle_location="x:///b")

    # Default (OpenClaw-resident) — unset.
    assert store.get_sot_tier(agent_id) is None

    # Flip to migrated (the cutover ceremony's load-bearing act).
    assert store.set_sot_tier(agent_id, "migrated") is True
    assert store.get_sot_tier(agent_id) == "migrated"

    # Flip back (rollback).
    assert store.set_sot_tier(agent_id, None) is True
    assert store.get_sot_tier(agent_id) is None


def test_set_sot_tier_unknown_agent_returns_false(tmp_path) -> None:
    store = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'a.db'}")
    assert store.set_sot_tier("ag_missing", "migrated") is False
    assert store.get_sot_tier("ag_missing") is None
