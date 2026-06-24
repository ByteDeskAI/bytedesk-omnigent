"""Unit tests for the RFC-8693 token-exchange (OBO) mint strategy.

``TokenExchangeMintStrategy`` is the first mint strategy whose load-bearing
input is a caller-supplied ``subject_token`` (the user's OpenIddict MCP access
token): it POSTs the token-exchange grant to Identity's ``/connect/token`` as
the agent client and returns the on-behalf-of bearer. The actor is conveyed by
the authenticated agent client, so the strategy requires ``subject_token`` (not
an ``ActingIdentity``) to be meaningful.
"""

from __future__ import annotations

import pytest

from omnigent.identity import Credential
from omnigent.identity.mint import MINT_REGISTRY, TokenExchangeMintStrategy
from omnigent.identity.ports import MintStrategy
from omnigent.spec.types import MCPOAuthConfig

_OAUTH = MCPOAuthConfig(
    token_url="https://identity.bytedesk.ai/connect/token",
    client_id="agent-client",
    client_secret="agent-secret",
    scopes=["mcp.invoke"],
    resource="urn:bytedesk:mcp",
)


# ── happy path ───────────────────────────────────────────────────────────────


def test_token_exchange_strategy_delegates(monkeypatch):
    monkeypatch.setattr(
        "omnigent.tools.mcp._resolve_token_exchange_token",
        lambda oauth, subject_token: "obo-tok",
    )
    cred = TokenExchangeMintStrategy().mint(
        identity=None,
        integration="mcp",
        config={"oauth": _OAUTH, "subject_token": "user-access-tok"},
    )
    assert isinstance(cred, Credential)
    assert cred.header_value == "Bearer obo-tok"


def test_token_exchange_strategy_posts_token_exchange_grant(monkeypatch):
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "obo-tok", "expires_in": 600}

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)
    cred = TokenExchangeMintStrategy().mint(
        identity=None,
        integration="mcp",
        config={"oauth": _OAUTH, "subject_token": "user-access-tok"},
    )
    assert cred.header_value == "Bearer obo-tok"
    form = captured["data"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == "user-access-tok"
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert form["client_id"] == "agent-client"


# ── config validation ────────────────────────────────────────────────────────


def test_token_exchange_strategy_requires_oauth():
    with pytest.raises(ValueError, match="mcp"):
        TokenExchangeMintStrategy().mint(
            identity=None, integration="mcp", config={"subject_token": "user-tok"}
        )


def test_token_exchange_strategy_requires_subject_token():
    with pytest.raises(ValueError, match="mcp"):
        TokenExchangeMintStrategy().mint(
            identity=None, integration="mcp", config={"oauth": _OAUTH}
        )


# ── registry wiring ──────────────────────────────────────────────────────────


def test_mint_registry_resolves_token_exchange():
    strategy = MINT_REGISTRY.get("token_exchange")
    assert strategy.name == "token_exchange"
    assert isinstance(strategy, MintStrategy)
