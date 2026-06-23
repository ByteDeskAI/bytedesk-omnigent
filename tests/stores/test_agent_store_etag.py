"""Optimistic-concurrency (If-Match/ETag) on AgentStore.update (BDP-2412, ADR-0150).

The agent row already carries a monotonic ``version``; these tests pin the new
guarded compare-and-swap path: a matching ``expected_version`` succeeds and bumps,
a stale one raises ``StaleWriteError`` (the row is NOT clobbered), and omitting it
keeps the unconditional behavior every existing caller relies on.
"""

from __future__ import annotations

import pytest

from omnigent.errors import StaleWriteError
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def _seed(agent_store: SqlAlchemyAgentStore) -> str:
    agent = agent_store.create(
        agent_id="ag_etag", name="etag-demo", bundle_location="ag_etag/v1"
    )
    assert agent.version == 1
    return agent.id


def test_update_with_matching_expected_version_succeeds(
    agent_store: SqlAlchemyAgentStore,
) -> None:
    agent_id = _seed(agent_store)
    updated = agent_store.update(agent_id, "ag_etag/v2", expected_version=1)
    assert updated is not None
    assert updated.version == 2
    assert updated.bundle_location == "ag_etag/v2"


def test_update_with_stale_expected_version_raises_and_does_not_clobber(
    agent_store: SqlAlchemyAgentStore,
) -> None:
    agent_id = _seed(agent_store)
    agent_store.update(agent_id, "ag_etag/v2", expected_version=1)  # -> version 2
    with pytest.raises(StaleWriteError):
        agent_store.update(agent_id, "ag_etag/v3", expected_version=1)  # stale
    # the rejected write must NOT have applied
    assert agent_store.get(agent_id).bundle_location == "ag_etag/v2"


def test_update_without_expected_version_is_unconditional(
    agent_store: SqlAlchemyAgentStore,
) -> None:
    # back-compat: every existing caller passes no precondition
    agent_id = _seed(agent_store)
    updated = agent_store.update(agent_id, "ag_etag/v2")
    assert updated is not None
    assert updated.version == 2


def test_guarded_update_on_missing_agent_is_not_found_not_conflict(
    agent_store: SqlAlchemyAgentStore,
) -> None:
    # rowcount 0 on a non-existent agent must read as NOT_FOUND (None),
    # never a spurious StaleWriteError (the 404-vs-412 disambiguation)
    assert agent_store.update("ag_missing", "x/y", expected_version=5) is None


def test_concurrent_clobber_is_closed_exactly_one_wins(
    agent_store: SqlAlchemyAgentStore,
) -> None:
    # the named AgentImageUpdate clobber: two writers both read version=1;
    # exactly one commits, the other gets StaleWriteError (no silent clobber)
    agent_id = _seed(agent_store)
    first = agent_store.update(agent_id, "ag_etag/A", expected_version=1)
    assert first.version == 2 and first.bundle_location == "ag_etag/A"
    with pytest.raises(StaleWriteError):
        agent_store.update(agent_id, "ag_etag/B", expected_version=1)
    assert agent_store.get(agent_id).bundle_location == "ag_etag/A"
