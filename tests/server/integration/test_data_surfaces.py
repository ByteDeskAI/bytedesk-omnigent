"""Integration tests for the read-only data-surface routes (Phase 9a).

Covers the five additive GET surfaces (BDP-2444, ADR-0152):
``/sessions/{id}/memories``, ``/sessions/{id}/usage/summary``,
``/users/{id}/cost/daily``, ``/sessions/{id}/spawn-tree``,
``/elicitations/pending``, and ``/hosts/health``.

Each route has a happy-path test (response shape) and a cross-owner / cross-
user leakage test. Auth is active (real ``SqlAlchemyPermissionStore`` +
``UnifiedAuthProvider(source="header")``), so ``X-Forwarded-Email`` selects the
caller and access is enforced exactly as in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import now_epoch
from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_pending_index() -> AsyncIterator[None]:
    """Reset the process-global pending-elicitations index around each test."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Auth-enabled app with the host store wired so /hosts/health is live."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        host_store=HostStore(db_uri),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seed_session(db_uri: str, owner: str, *, level: int = LEVEL_OWNER) -> str:
    """Create a conversation and grant ``owner`` ``level`` on it."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()
    perm = SqlAlchemyPermissionStore(db_uri)
    perm.ensure_user(owner)
    perm.grant(owner, conv.id, level)
    return conv.id


# ── /sessions/{id}/memories ─────────────────────────────────────────────────


async def test_memories_happy_path_returns_session_scoped_memories(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Alice's session lists only the memories captured from that session."""
    session_id = _seed_session(db_uri, ALICE)
    mem = SqlAlchemyMemoryStore(db_uri)
    mem.append(
        scope="topic", owner="t", name="n", content="from this session",
        source_conversation_id=session_id, salience=0.8, confidence=0.5,
    )
    mem.append(
        scope="topic", owner="t", name="n", content="from another session",
        source_conversation_id="conv_other",
    )

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/memories",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert body["has_more"] is False
    assert [m["content"] for m in body["data"]] == ["from this session"]
    one = body["data"][0]
    assert one["object"] == "memory"
    assert one["source_conversation_id"] == session_id
    assert one["salience"] == 0.8
    assert one["archived"] is False
    assert {"id", "weight", "created_at", "last_accessed_at", "access_count"} <= one.keys()


async def test_memories_not_leaked_to_other_owner(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Bob cannot read the memories of Alice's session (404, no existence leak)."""
    session_id = _seed_session(db_uri, ALICE)
    SqlAlchemyMemoryStore(db_uri).append(
        scope="topic", owner="t", name="n", content="alice secret",
        source_conversation_id=session_id,
    )
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/memories",
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 404, resp.text


# ── /sessions/{id}/usage/summary ────────────────────────────────────────────


async def test_usage_summary_happy_path(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Usage summary projects the persisted session_usage blob."""
    session_id = _seed_session(db_uri, ALICE)
    SqlAlchemyConversationStore(db_uri).set_session_usage(
        session_id,
        {
            "input_tokens": 1200,
            "output_tokens": 340,
            "total_tokens": 1540,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 200,
            "total_cost_usd": 0.42,
            "by_model": {"claude-sonnet-4-6": {"input_tokens": 1200, "total_cost_usd": 0.42}},
        },
    )
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/usage/summary",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input_tokens"] == 1200
    assert body["output_tokens"] == 340
    assert body["total_tokens"] == 1540
    assert body["cache_read_input_tokens"] == 800
    assert body["cache_creation_input_tokens"] == 200
    assert body["total_cost_usd"] == 0.42
    assert body["usage_by_model"]["claude-sonnet-4-6"]["total_cost_usd"] == 0.42


async def test_usage_summary_not_leaked_to_other_owner(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Bob cannot read Alice's session usage."""
    session_id = _seed_session(db_uri, ALICE)
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/usage/summary",
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 404, resp.text


# ── /users/{id}/cost/daily ──────────────────────────────────────────────────


async def test_daily_cost_happy_path(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A user reads their own accumulated daily cost."""
    conv = SqlAlchemyConversationStore(db_uri)
    SqlAlchemyPermissionStore(db_uri).ensure_user(ALICE)
    conv.add_daily_cost(ALICE, "2026-06-24", 1.25)
    resp = await auth_client.get(
        "/v1/users/alice@example.com/cost/daily?date=2026-06-24",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date_utc"] == "2026-06-24"
    assert body["cost_usd"] == 1.25
    assert "ask_approved_usd" in body


async def test_daily_cost_not_leaked_to_other_user(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Bob cannot read Alice's daily cost (403, not admin)."""
    SqlAlchemyPermissionStore(db_uri).ensure_user(BOB)
    resp = await auth_client.get(
        "/v1/users/alice@example.com/cost/daily",
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 403, resp.text


# ── /sessions/{id}/spawn-tree ───────────────────────────────────────────────


async def test_spawn_tree_happy_path(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The spawn tree nests sub-agent children under the requested root."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation()
    perm = SqlAlchemyPermissionStore(db_uri)
    perm.ensure_user(ALICE)
    perm.grant(ALICE, root.id, LEVEL_OWNER)
    conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=root.id,
        sub_agent_name="researcher",
    )

    resp = await auth_client.get(
        f"/v1/sessions/{root.id}/spawn-tree",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == root.id
    assert body["object"] == "spawn_tree"
    assert body["agent_type"] == "root"
    assert len(body["children"]) == 1
    child = body["children"][0]
    assert child["agent_type"] == "researcher"
    assert child["metadata"]["sub_agent_name"] == "researcher"
    assert child["metadata"]["title"] == "researcher:auth"


async def test_spawn_tree_not_leaked_to_other_owner(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Bob cannot read Alice's spawn tree."""
    session_id = _seed_session(db_uri, ALICE)
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/spawn-tree",
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 404, resp.text


# ── /elicitations/pending ───────────────────────────────────────────────────


def _record_pending(conversation_id: str, elicitation_id: str, message: str) -> None:
    """Record one outstanding elicitation in the in-memory index."""
    pending_elicitations.record_publish(
        conversation_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"message": message},
        },
    )


async def test_pending_elicitations_scoped_to_caller(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Only the caller's accessible sessions appear; others are silently omitted."""
    alice_session = _seed_session(db_uri, ALICE)
    bob_session = _seed_session(db_uri, BOB)
    _record_pending(alice_session, "elicit_a", "Approve A?")
    _record_pending(bob_session, "elicit_b", "Approve B?")

    resp = await auth_client.get(
        "/v1/elicitations/pending",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == 1
    assert len(body["by_session"]) == 1
    entry = body["by_session"][0]
    assert entry["conversation_id"] == alice_session
    assert entry["pending_count"] == 1
    assert entry["oldest_created_at"] is None
    assert entry["elicitations"][0]["prompt"] == "Approve A?"


async def test_pending_elicitations_filter_excludes_inaccessible(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Requesting a session_ids filter for Bob's session yields nothing for Alice."""
    bob_session = _seed_session(db_uri, BOB)
    _record_pending(bob_session, "elicit_b", "Approve B?")
    resp = await auth_client.get(
        f"/v1/elicitations/pending?session_ids={bob_session}",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == 0
    assert body["by_session"] == []


# ── /hosts/health ───────────────────────────────────────────────────────────


async def test_hosts_health_happy_path(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Fleet health aggregates the caller's hosts by status and provider."""
    store = HostStore(db_uri)
    store.upsert_on_connect(host_id="host_alice1", name="laptop", owner=ALICE)
    resp = await auth_client.get(
        "/v1/hosts/health",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_hosts"] == 1
    assert body["online_hosts"] == 1
    assert body["offline_hosts"] == 0
    assert body["hosts_by_sandbox_provider"] == {"external": 1}
    assert body["avg_last_seen_seconds_ago"] is not None


async def test_hosts_health_managed_host_not_leaked_across_owners(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A managed (sandbox) host is owner-scoped (ADR-0151): Bob never counts it.

    External hosts are org-shared, so the isolation boundary that
    ``/hosts/health`` must honor is the *managed* host — it stays owner-only,
    exactly as ``GET /v1/hosts`` (the read this aggregates) scopes it.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_alice_managed",
        name="managed-a1",
        owner=ALICE,
        token="tok-alice",
        provider="modal",
        sandbox_id="sb-1",
        token_expires_at=now_epoch() + 3600,
    )
    # Alice sees her managed host.
    alice_resp = await auth_client.get(
        "/v1/hosts/health", headers={"X-Forwarded-Email": ALICE}
    )
    assert alice_resp.status_code == 200, alice_resp.text
    assert alice_resp.json()["hosts_by_sandbox_provider"] == {"modal": 1}
    # Bob does not.
    bob_resp = await auth_client.get(
        "/v1/hosts/health", headers={"X-Forwarded-Email": BOB}
    )
    assert bob_resp.status_code == 200, bob_resp.text
    assert bob_resp.json()["total_hosts"] == 0
