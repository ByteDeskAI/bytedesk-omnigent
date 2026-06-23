"""BDP-2424 P2 — the MCP proxy mints + attaches the signed
``X-Omnigent-Acting-Identity`` carrier on runner dispatch.

The server is the *producer*: it resolves the inbound principal, mints a per-agent
:class:`ActingIdentity` token, and attaches it on the ``/mcp/execute`` POST so the
runner (Path A) can decode it into ``ToolContext.acting_identity``.

- **unit** — the ``_mint_acting_identity_header`` degrade matrix (every missing
  input ⇒ ``None`` ⇒ today's behaviour) and a happy path that the *real*
  runner-side decoder (``decode_acting_identity``) round-trips.
- **integration** — drive the real ``_handle_mcp_tools_call`` through an ALLOW
  policy to the runner POST, capture the headers, and prove the real verifier
  decodes the carrier back to the inbound principal (the server→runner seam).
"""

from __future__ import annotations

import types
from dataclasses import dataclass

import httpx
import pytest

from omnigent.entities.conversation import Conversation
from omnigent.identity.identity import ActingIdentity
from omnigent.identity.signer import (
    HEADER_NAME,
    HmacAssertionSigner,
    decode_acting_identity,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.policies.types import PolicyResult
from omnigent.server.principal import Principal
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    _handle_mcp_tools_call,
    _mint_acting_identity_header,
)
from omnigent.spec.types import PolicyAction

_SECRET = "p2-acting-identity-secret"
_SESSION_ID = "conv_p2_acting_identity"
_TENANT = "11111111-2222-3333-4444-555555555555"


def _signer() -> HmacAssertionSigner:
    return HmacAssertionSigner(_SECRET)


def _verifier() -> HmacAssertionVerifier:
    return HmacAssertionVerifier(_SECRET)


def _principal() -> Principal:
    return Principal(user_id="alice@example.com", tenant_id=_TENANT, roles=("agent", "office"))


def _request_with_signer(signer: HmacAssertionSigner | None) -> types.SimpleNamespace:
    """Minimal request stand-in — the helper reads only ``.app.state.assertion_signer``."""
    state = types.SimpleNamespace()
    if signer is not None:
        state.assertion_signer = signer
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


@dataclass
class _StubAuthProvider:
    principal: Principal | None

    def get_principal(self, request: object) -> Principal | None:
        del request
        return self.principal


# ── unit: degrade matrix (every missing input ⇒ no header) ───────────────────


def test_mint_returns_none_when_no_request() -> None:
    assert _mint_acting_identity_header(None, _StubAuthProvider(_principal()), "ag_1") is None


def test_mint_returns_none_when_no_auth_provider() -> None:
    assert _mint_acting_identity_header(_request_with_signer(_signer()), None, "ag_1") is None


def test_mint_returns_none_when_no_principal() -> None:
    req = _request_with_signer(_signer())
    assert _mint_acting_identity_header(req, _StubAuthProvider(None), "ag_1") is None


def test_mint_returns_none_when_no_signer_on_state() -> None:
    req = _request_with_signer(None)  # state has no assertion_signer attr
    assert _mint_acting_identity_header(req, _StubAuthProvider(_principal()), "ag_1") is None


def test_mint_returns_none_when_signer_unconfigured() -> None:
    # A signer present but with no secret mints no token (degrade-to-default).
    req = _request_with_signer(HmacAssertionSigner(None))
    assert _mint_acting_identity_header(req, _StubAuthProvider(_principal()), "ag_1") is None


# ── unit / seam: happy path round-trips through the REAL runner-side decoder ──


def test_mint_header_decodes_to_acting_identity() -> None:
    req = _request_with_signer(_signer())
    headers = _mint_acting_identity_header(req, _StubAuthProvider(_principal()), "ag_42")

    assert headers is not None and HEADER_NAME in headers
    # The exact call the runner's mcp_execute makes on the inbound header.
    acting = decode_acting_identity(headers[HEADER_NAME], _verifier())
    assert isinstance(acting, ActingIdentity)
    assert acting.agent_id == "ag_42"
    assert acting.principal is not None
    assert acting.principal.user_id == "alice@example.com"
    assert acting.principal.tenant_id == _TENANT
    assert list(acting.principal.roles) == ["agent", "office"]


# ── integration: the real handler attaches the header on the runner POST ─────


class _CapturingRunnerClient:
    """Records POST kwargs then raises — we assert on the headers, not the response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, "headers": kwargs.get("headers")})
        raise httpx.ConnectError("captured")


@dataclass
class _StubConversationStore:
    conv: Conversation

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.conv if session_id == self.conv.id else None


class _AllowPolicyEngine:
    async def evaluate(self, ctx: object) -> PolicyResult:
        del ctx
        return PolicyResult(action=PolicyAction.ALLOW, reason=None)

    def apply_label_writes(self, set_labels: object) -> None:
        del set_labels

    def apply_state_updates(self, updates: object) -> None:
        del updates


@pytest.mark.asyncio
async def test_handler_attaches_acting_identity_header_on_runner_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_handle_mcp_tools_call`` mints + attaches the carrier on the runner POST.

    A regression here (header dropped, or attached unsigned/forgeable) means the
    per-agent acting identity never reaches the runner, so ``acting_identity``
    stays ``None`` and the whole OBO chain silently no-ops.
    """
    conv = Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        agent_id="ag_test",
    )
    capturing = _CapturingRunnerClient()

    monkeypatch.setattr(
        sessions_mod, "_load_agent_spec_for_session", lambda conv, agent_store: "fake_spec"
    )
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda spec, session_id, conversation_store: _AllowPolicyEngine(),
    )

    async def _fake_get_runner_client(
        session_id: str, runner_router: object
    ) -> _CapturingRunnerClient:
        del session_id, runner_router
        return capturing

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _fake_get_runner_client)

    await _handle_mcp_tools_call(
        rpc_id=1,
        session_id=_SESSION_ID,
        params={"name": "sys_os_read", "arguments": {}},
        conversation_store=_StubConversationStore(conv),  # type: ignore[arg-type]
        agent_store=object(),  # type: ignore[arg-type]
        runner_router=None,
        request=_request_with_signer(_signer()),  # type: ignore[arg-type]
        auth_provider=_StubAuthProvider(_principal()),  # type: ignore[arg-type]
    )

    assert capturing.calls, "runner POST was never reached (policy gate?)"
    headers = capturing.calls[0]["headers"]
    assert isinstance(headers, dict) and HEADER_NAME in headers, (
        "the MCP proxy did not attach the acting-identity carrier on runner dispatch"
    )
    acting = decode_acting_identity(headers[HEADER_NAME], _verifier())
    assert acting is not None
    assert acting.agent_id == "ag_test"
    assert acting.principal is not None
    assert acting.principal.user_id == "alice@example.com"
    assert acting.principal.tenant_id == _TENANT
