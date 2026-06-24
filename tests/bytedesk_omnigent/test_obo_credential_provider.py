"""ByteDesk OBO OutboundCredentialProvider (BDP-2434 Part 3).

The provider selects the ``token_exchange`` mint strategy when the acting
identity carries a ``subject_token`` (the user's MCP access token) and the
integration config supplies its ``oauth`` block; otherwise it returns ``None``
so the outbound-credential registry falls back to the default
``static_secret`` provider — i.e. today's ``client_credentials`` egress.
"""

from __future__ import annotations

from bytedesk_omnigent.auth.obo_credential_provider import (
    OnBehalfOfCredentialProvider,
)
from bytedesk_omnigent.extension import BytedeskExtension
from omnigent.identity import Credential
from omnigent.identity.defaults import acting_identity_for
from omnigent.identity.ports import OutboundCredentialProvider
from omnigent.server.principal import Principal
from omnigent.spec.types import MCPOAuthConfig

_OAUTH = MCPOAuthConfig(
    token_url="https://identity.bytedesk.ai/connect/token",
    client_id="agent-client",
    client_secret="agent-secret",
    scopes=["mcp.invoke"],
    resource="urn:bytedesk:mcp",
)


def _identity(subject_token):
    return acting_identity_for(
        Principal(user_id="alice", tenant_id="t1"), agent_id="maya", subject_token=subject_token
    )


# ── conformance ──────────────────────────────────────────────────────────────


def test_provider_conforms_to_port():
    assert isinstance(OnBehalfOfCredentialProvider(), OutboundCredentialProvider)


def test_provider_has_a_stable_name():
    assert OnBehalfOfCredentialProvider().name == "token_exchange_obo"


# ── selects token_exchange when subject_token present ────────────────────────


def test_mint_uses_token_exchange_when_subject_token_present(monkeypatch):
    monkeypatch.setattr(
        "omnigent.tools.mcp._resolve_token_exchange_token",
        lambda oauth, subject_token: "obo-tok",
    )
    cred = OnBehalfOfCredentialProvider().mint(
        identity=_identity("user-access-tok"),
        integration="bytedesk-mcp",
        config={"oauth": _OAUTH},
    )
    assert isinstance(cred, Credential)
    assert cred.header_value == "Bearer obo-tok"


# ── falls back (None) when not resolvable ────────────────────────────────────


def test_mint_returns_none_when_no_subject_token():
    # No subject_token on the identity ⇒ fall back to the default provider.
    assert (
        OnBehalfOfCredentialProvider().mint(
            identity=_identity(None), integration="bytedesk-mcp", config={"oauth": _OAUTH}
        )
        is None
    )


def test_mint_returns_none_when_identity_absent():
    assert (
        OnBehalfOfCredentialProvider().mint(
            identity=None, integration="bytedesk-mcp", config={"oauth": _OAUTH}
        )
        is None
    )


def test_mint_returns_none_when_no_oauth_config():
    # subject_token present but the integration can't do token-exchange ⇒ fall back.
    assert (
        OnBehalfOfCredentialProvider().mint(
            identity=_identity("user-access-tok"), integration="bytedesk-mcp", config=None
        )
        is None
    )
    assert (
        OnBehalfOfCredentialProvider().mint(
            identity=_identity("user-access-tok"), integration="bytedesk-mcp", config={}
        )
        is None
    )


# ── extension registers the provider on the outbound seam hook ───────────────


def test_extension_contributes_the_obo_provider():
    providers = BytedeskExtension().outbound_credential_providers()
    assert "token_exchange_obo" in providers
    built = providers["token_exchange_obo"]()
    assert isinstance(built, OutboundCredentialProvider)
    assert built.name == "token_exchange_obo"


def test_extension_provider_registers_on_the_outbound_registry(monkeypatch):
    # Prove the per-seam hook wiring resolves the provider on the live registry.
    from omnigent.identity.registry import build_outbound_credential_registry

    monkeypatch.setattr(
        "omnigent.pluggable.registry.discover_extensions", lambda: [BytedeskExtension()]
    )
    reg = build_outbound_credential_registry()
    reg.discover_extensions(hook="outbound_credential_providers")
    assert "token_exchange_obo" in reg.names()
    assert reg.get("token_exchange_obo").name == "token_exchange_obo"
