"""Admin tenant-scoped session-list visibility (BDP-2438).

``GET /v1/sessions`` filters ``accessible_by=user_id`` against the per-owner
``session_permissions`` ACL. Office-driven sessions are owned by a *different*
principal (the ``local``/synthetic owner Office sends) than the operator who
logs into the UI, so the operator never sees them in the sidebar even though
the agent replies arrive (via the Office relay, independent of this list).

This pins the fix: an **admin** caller gets the per-owner ACL relaxed so they
see every session — but the BDP-2395 tenant filter is still layered on top, so
a tenant-scoped admin sees only their own tenant, and a **non-admin** still
sees only sessions they own. A tenant-less (single-org / local) admin sees
all, which is the local-operator case that motivated this.

Drives the real ``list_sessions`` route through ``create_app`` with a real
``SqlAlchemyPermissionStore`` + ``SqlAlchemyConversationStore`` and a tiny
header-driven auth provider so the acting user id and tenant are controllable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.principal import Principal
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


class _UserTenantProvider(AuthProvider):
    """Test auth provider: identity from request headers.

    ``X-Test-User`` → ``user_id``; ``X-Test-Tenant`` → ``tenant_id`` (absent →
    ``None``, the single-org / local posture). Lets a test drive both the ACL
    dimension (who is calling) and the BDP-2395 tenant dimension precisely,
    without standing up OIDC/accounts or the gateway principal header.
    """

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user") or None

    def get_principal(self, request: HTTPConnection) -> Principal | None:
        uid = request.headers.get("x-test-user")
        if not uid:
            return None
        return Principal(user_id=uid, tenant_id=request.headers.get("x-test-tenant") or None)


@pytest.fixture()
def admin_vis_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with a permission store + the header-driven test provider."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=_UserTenantProvider(),
    )


@pytest_asyncio.fixture()
async def admin_vis_client(
    admin_vis_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the admin-visibility app (mirrors ``auth_client``)."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=admin_vis_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)


def _make_session(db_uri: str, *, owner: str, tenant: str | None) -> str:
    """Create a session row owned by ``owner`` in ``tenant`` and return its id.

    A session is a conversation with a non-``None`` ``agent_id`` plus a
    ``LEVEL_OWNER`` grant — exactly what ``POST /v1/sessions`` produces, created
    directly here so the list semantics are tested in isolation. A real agent
    row is registered first to satisfy the conversation→agent foreign key.
    """
    convs = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)

    agent_id = f"ag_{uuid4().hex}"
    agents.create(agent_id=agent_id, name=agent_id, bundle_location=f"{agent_id}/bundle")
    conv = convs.create_conversation(agent_id=agent_id, tenant_id=tenant)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


async def _list_ids(
    client: httpx.AsyncClient, *, user: str, tenant: str | None = None
) -> set[str]:
    headers = {"X-Test-User": user}
    if tenant is not None:
        headers["X-Test-Tenant"] = tenant
    resp = await client.get("/v1/sessions?kind=any&limit=1000", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["id"] for item in resp.json()["data"]}


async def test_admin_sees_another_users_session_in_tenant(
    admin_vis_client: httpx.AsyncClient, db_uri: str
) -> None:
    """An admin sees a session owned by a different user in the same tenant."""
    sid = _make_session(db_uri, owner="alice", tenant="t1")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob")
    perms.set_admin("bob", True)

    assert sid in await _list_ids(admin_vis_client, user="bob", tenant="t1")


async def test_non_admin_does_not_see_another_users_session(
    admin_vis_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A non-admin still sees only sessions they own (ACL unchanged)."""
    sid = _make_session(db_uri, owner="alice", tenant="t1")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("carol")  # member, not admin

    assert sid not in await _list_ids(admin_vis_client, user="carol", tenant="t1")


async def test_admin_tenant_isolation_is_preserved(
    admin_vis_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Relaxing the owner ACL for admins does NOT cross tenants (BDP-2395)."""
    own_tenant = _make_session(db_uri, owner="alice", tenant="t1")
    other_tenant = _make_session(db_uri, owner="dave", tenant="t2")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob")
    perms.set_admin("bob", True)

    visible = await _list_ids(admin_vis_client, user="bob", tenant="t1")
    assert own_tenant in visible
    assert other_tenant not in visible


async def test_tenantless_admin_sees_all(
    admin_vis_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A tenant-less (single-org / local) admin sees every session.

    This is the local-operator case: the accounts admin carries no tenant, so
    the tenant filter is skipped and the relaxed ACL surfaces all sessions —
    including the ``local``-owned Office sessions that motivated BDP-2438.
    """
    a = _make_session(db_uri, owner="alice", tenant="t1")
    b = _make_session(db_uri, owner="local", tenant="t2")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob")
    perms.set_admin("bob", True)

    visible = await _list_ids(admin_vis_client, user="bob")  # no tenant header
    assert {a, b} <= visible
