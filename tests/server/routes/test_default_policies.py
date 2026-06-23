"""Tests for the default policies CRUD routes (``/v1/policies``).

The default policies router is only mounted when ``create_app`` receives
a ``policy_store``. The standard conftest ``app`` fixture does not
supply one, so these tests provide their own app/client that include it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


@pytest.fixture()
def policy_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Build a FastAPI app that includes the policy store."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


@pytest_asyncio.fixture()
async def policy_client(
    policy_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the policy-enabled app."""
    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _policy_payload(**overrides: object) -> dict:
    """Build a valid CreateDefaultPolicyRequest payload."""
    base: dict = {
        "name": "test_url_policy",
        "type": "url",
        "handler": "https://example.com/policies/eval",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


# ── If-Match / optimistic concurrency (BDP-2412) ──────────────────────


async def test_update_default_policy_if_match(policy_client: httpx.AsyncClient) -> None:
    """If-Match guards the default-policy PATCH through the real route."""
    created = await policy_client.post("/v1/policies", json=_policy_payload(name="etag_pol"))
    assert created.status_code in (200, 201), created.text
    policy_id = created.json()["id"]

    # Matching ETag → 200, version bumps to 2.
    ok = await policy_client.patch(
        f"/v1/policies/{policy_id}",
        json={"enabled": False},
        headers={"If-Match": '"1"'},
    )
    assert ok.status_code == 200, ok.text
    assert ok.headers["etag"] == '"2"'

    # Stale ETag → 412 (no clobber).
    stale = await policy_client.patch(
        f"/v1/policies/{policy_id}",
        json={"enabled": True},
        headers={"If-Match": '"1"'},
    )
    assert stale.status_code == 412, stale.text

    # No If-Match → unconditional (back-compat).
    uncond = await policy_client.patch(
        f"/v1/policies/{policy_id}", json={"enabled": True}
    )
    assert uncond.status_code == 200, uncond.text


# ── POST /v1/policies ────────────────────────────────────────────────


async def test_create_default_policy(policy_client: httpx.AsyncClient) -> None:
    """Creating a default URL policy returns the policy object."""
    resp = await policy_client.post("/v1/policies", json=_policy_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test_url_policy"
    assert body["type"] == "url"
    assert body["handler"] == "https://example.com/policies/eval"
    assert body["object"] == "default_policy"
    assert body["enabled"] is True
    assert body["id"].startswith("pol_")


async def test_create_policy_rejects_below_floor_param(
    policy_client: httpx.AsyncClient,
) -> None:
    """A schema-valid but below-floor two_key policy is rejected at the create
    boundary, not persisted to fail-closed later at agent load (BDP-2411)."""
    from omnigent.policies.registry import load_registry

    load_registry()  # ensure the bytedesk floor handlers are registered
    payload = {
        "name": "two_key_floor",
        "type": "python",
        "handler": "bytedesk_omnigent.policies.two_key.two_key_required",
        "factory_params": {"patterns": ["billing\\.refund"], "min_approvers": 1},
    }
    resp = await policy_client.post("/v1/policies", json=payload)
    assert resp.status_code == 400, resp.text
    assert "min_approvers" in resp.text

    # The same policy with a safe floor value (>= 2) is accepted.
    payload["factory_params"]["min_approvers"] = 2
    ok = await policy_client.post("/v1/policies", json=payload)
    assert ok.status_code == 200, ok.text


async def test_create_duplicate_policy_name(policy_client: httpx.AsyncClient) -> None:
    """Creating two default policies with the same name returns 409."""
    await policy_client.post("/v1/policies", json=_policy_payload(name="dup"))
    resp = await policy_client.post("/v1/policies", json=_policy_payload(name="dup"))
    assert resp.status_code == 409


async def test_create_policy_unregistered_python_handler(policy_client: httpx.AsyncClient) -> None:
    """A python policy with an unregistered handler is rejected."""
    resp = await policy_client.post(
        "/v1/policies",
        json=_policy_payload(
            name="bad_py",
            type="python",
            handler="some.unregistered.handler",
        ),
    )
    assert resp.status_code == 400


# ── GET /v1/policies ─────────────────────────────────────────────────


async def test_list_default_policies_empty(policy_client: httpx.AsyncClient) -> None:
    """Empty policy store returns an empty list."""
    resp = await policy_client.get("/v1/policies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []


async def test_list_default_policies_after_create(policy_client: httpx.AsyncClient) -> None:
    """Created policies appear in the list."""
    create_resp = await policy_client.post("/v1/policies", json=_policy_payload())
    pid = create_resp.json()["id"]

    resp = await policy_client.get("/v1/policies")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()["data"]]
    assert pid in ids


# ── GET /v1/policies/{policy_id} ─────────────────────────────────────


async def test_get_default_policy(policy_client: httpx.AsyncClient) -> None:
    """Get a specific policy by ID."""
    create_resp = await policy_client.post("/v1/policies", json=_policy_payload())
    pid = create_resp.json()["id"]

    resp = await policy_client.get(f"/v1/policies/{pid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


async def test_get_default_policy_not_found(policy_client: httpx.AsyncClient) -> None:
    """Getting a nonexistent policy returns 404."""
    resp = await policy_client.get("/v1/policies/pol_nonexistent")
    assert resp.status_code == 404


# ── PATCH /v1/policies/{policy_id} ───────────────────────────────────


async def test_update_default_policy(policy_client: httpx.AsyncClient) -> None:
    """Patching a policy's name returns the updated policy."""
    create_resp = await policy_client.post("/v1/policies", json=_policy_payload())
    pid = create_resp.json()["id"]

    resp = await policy_client.patch(
        f"/v1/policies/{pid}",
        json={"name": "renamed_policy"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed_policy"


async def test_update_default_policy_not_found(policy_client: httpx.AsyncClient) -> None:
    """Patching a nonexistent policy returns 404."""
    resp = await policy_client.patch(
        "/v1/policies/pol_nonexistent",
        json={"name": "renamed"},
    )
    assert resp.status_code == 404


async def test_update_default_policy_toggle_enabled(policy_client: httpx.AsyncClient) -> None:
    """Disabling a policy sets enabled=false."""
    create_resp = await policy_client.post("/v1/policies", json=_policy_payload())
    pid = create_resp.json()["id"]

    resp = await policy_client.patch(f"/v1/policies/{pid}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ── DELETE /v1/policies/{policy_id} ──────────────────────────────────


async def test_delete_default_policy(policy_client: httpx.AsyncClient) -> None:
    """Deleting a policy returns deleted: true."""
    create_resp = await policy_client.post("/v1/policies", json=_policy_payload())
    pid = create_resp.json()["id"]

    resp = await policy_client.delete(f"/v1/policies/{pid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone
    get_resp = await policy_client.get(f"/v1/policies/{pid}")
    assert get_resp.status_code == 404


async def test_delete_default_policy_idempotent(policy_client: httpx.AsyncClient) -> None:
    """Deleting a nonexistent policy still returns deleted: true."""
    resp = await policy_client.delete("/v1/policies/pol_nonexistent")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
