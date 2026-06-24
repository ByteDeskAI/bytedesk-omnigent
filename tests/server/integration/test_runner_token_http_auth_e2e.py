"""Integration tests for accounts-mode runner HTTP auth (BDP-2437).

In accounts mode a runner spawned on a host pod calls back to the server
(``GET /v1/sessions/{id}/agent/contents``, ``/items``, ``/v1/sessions/{id}``)
carrying ONLY its server-issued tunnel binding token — no user cookie. Before
this fix those callbacks 401'd and every turn failed with
``spec_resolver_failed``. The :class:`RunnerTokenAuthProvider` resolves the
runner's launch owner from the trusted launch record and is wired as a TAIL
resolver of the principal composite (after the user cookie), the symmetric
HTTP-side mirror of the BDP-2436 WS-tunnel fix.

These tests exercise the wiring against a real accounts-mode FastAPI app:

1. A runner token bound to a recorded launch owner authorizes a protected route
   AS that owner.
2. A forged token the server never launched → 401 (security invariant).
3. A real user session cookie still authenticates with the runner provider in
   the chain (no shadowing).
4. **Finding-1 regression**: the configured accounts provider still sits in the
   composite's ``_base`` slot (``accounts_provider`` / ``unwrap_auth_base`` see
   it; the SPA login URL survives) — the tail resolver never displaced it.
5. A runner token authenticates only AS its launch owner: the route's own
   per-owner scoping still applies (identity ≠ cross-owner access).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)

pytestmark = pytest.mark.asyncio

_COOKIE_SECRET_HEX = secrets.token_hex(32)
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "admin-pw-12345"


def _build_accounts_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a production-shaped accounts-mode app (mirrors test_accounts_auth_e2e)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", _COOKIE_SECRET_HEX)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", _ADMIN_PASSWORD)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", _ADMIN_USERNAME)
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)

    db_url = f"sqlite:///{tmp_path}/test.db"

    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    get_or_create_engine(db_url)
    telemetry.init()
    permission_store = SqlAlchemyPermissionStore(db_url)
    agent_store = SqlAlchemyAgentStore(db_url)
    conversation_store = SqlAlchemyConversationStore(db_url)
    file_store = SqlAlchemyFileStore(db_url)
    comment_store = SqlAlchemyCommentStore(db_url)
    host_store = HostStore(db_url)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
    )

    auth_provider = create_auth_provider()
    account_store = SqlAlchemyAccountStore(db_url)
    return create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        host_store=host_store,
        auth_provider=auth_provider,
        account_store=account_store,
    )


@pytest.fixture()
def accounts_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    return _build_accounts_app(tmp_path, monkeypatch)


@pytest_asyncio.fixture()
async def client(accounts_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.db.utils import clear_engine_cache

    transport = httpx.ASGITransport(app=accounts_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    clear_engine_cache()


def _record_launch_owner(app: FastAPI, token: str, owner: str) -> str:
    """Record a trusted launch owner for a token-bound runner id; return the id."""
    runner_id = token_bound_runner_id(token)
    app.state.tunnel_registry.record_launch_owner(runner_id, owner)
    return runner_id


def _user_cookie_header(app: FastAPI, user_id: str) -> dict[str, str]:
    """Mint a valid accounts session cookie for ``user_id`` as a Cookie header.

    Sent as an explicit ``Cookie`` header (not httpx per-request ``cookies=``,
    which is deprecated and unreliable under ``ASGITransport``); the server
    reads it via ``request.cookies`` identically. The cookie NAME is read from
    the live accounts config so it matches the HTTP-vs-HTTPS ``__Host-`` rule.
    """
    from omnigent.server.auth import accounts_provider
    from omnigent.server.oidc import mint_session_cookie

    cfg = accounts_provider(app.state.auth_provider)._accounts_config
    token = mint_session_cookie(
        user_id,
        cfg.cookie_secret,
        ttl_hours=1,
        provider="accounts",
    )
    return {"Cookie": f"{cfg.session_cookie_name}={token}"}


# ── Finding-1 regression: tail resolver never displaces the base ──────


def test_wiring_keeps_accounts_provider_in_base_slot(accounts_app: FastAPI) -> None:
    # The whole reason for the tail_resolvers seam (BDP-2437): a runner-token
    # resolver wired as a tail must NOT occupy `_base`, or accounts mode breaks
    # server-wide (no bootstrap, no login router, no login page).
    from omnigent.server.auth import accounts_provider, unwrap_auth_base

    wired = accounts_app.state.auth_provider
    assert accounts_provider(wired) is not None
    assert getattr(unwrap_auth_base(wired), "login_url", None) == "/login"


# ── Forged token rejected (security invariant) ────────────────────────


async def test_forged_runner_token_is_rejected_401(client: httpx.AsyncClient) -> None:
    # A token the server never launched has NO launch record → the runner
    # provider yields None → the composite falls through → 401.
    resp = await client.get(
        "/v1/sessions",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "attacker-chosen-token"},
    )
    assert resp.status_code == 401, resp.text


async def test_no_credentials_is_rejected_401(client: httpx.AsyncClient) -> None:
    # Sanity: accounts mode 401s an unauthenticated request (baseline).
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 401, resp.text


# ── Valid launch-record token authorizes ──────────────────────────────


async def test_runner_token_with_launch_record_authorizes(
    client: httpx.AsyncClient, accounts_app: FastAPI
) -> None:
    token = "runner-binding-token-valid"
    _record_launch_owner(accounts_app, token, "alice@example.com")
    resp = await client.get(
        "/v1/sessions",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    assert resp.status_code == 200, resp.text


# ── Real user cookie still authenticates (no shadowing) ───────────────


async def test_user_cookie_still_authenticates_with_runner_provider(
    client: httpx.AsyncClient, accounts_app: FastAPI
) -> None:
    resp = await client.get(
        "/v1/sessions",
        headers=_user_cookie_header(accounts_app, "bob@example.com"),
    )
    assert resp.status_code == 200, resp.text


# ── Identity = launch owner, not cross-owner access ───────────────────


async def test_runner_token_authenticates_only_as_launch_owner(
    client: httpx.AsyncClient, accounts_app: FastAPI
) -> None:
    # Two runner tokens recorded for two different owners. Each authorizes (200)
    # but as its OWN owner — the route's accessible_by scoping uses the resolved
    # identity. We assert both succeed AND the resolved identities differ by
    # confirming the provider maps each token to its recorded owner directly
    # (route 200 proves the chain authorized; the unit tests pin owner equality,
    # and here we pin that distinct tokens resolve to distinct owners live).
    from starlette.requests import HTTPConnection

    from omnigent.server.auth import RunnerTokenAuthProvider

    alice_token = "runner-binding-token-alice"
    bob_token = "runner-binding-token-bob"
    _record_launch_owner(accounts_app, alice_token, "alice@example.com")
    _record_launch_owner(accounts_app, bob_token, "bob@example.com")

    # Both authorize at the route.
    for tok in (alice_token, bob_token):
        resp = await client.get(
            "/v1/sessions", headers={RUNNER_TUNNEL_TOKEN_HEADER: tok}
        )
        assert resp.status_code == 200, resp.text

    # And each resolves to its OWN owner against the live registry — a runner
    # authenticates only AS the owner it was launched for.
    provider = RunnerTokenAuthProvider(accounts_app.state.tunnel_registry)

    def _conn(tok: str) -> HTTPConnection:
        raw = [(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), tok.encode())]
        return HTTPConnection({"type": "http", "headers": raw})

    assert provider.get_user_id(_conn(alice_token)) == "alice@example.com"
    assert provider.get_user_id(_conn(bob_token)) == "bob@example.com"
