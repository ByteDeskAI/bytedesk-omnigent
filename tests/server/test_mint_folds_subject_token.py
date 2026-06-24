"""The ``tools/call`` mint site folds the stashed subject_token (BDP-2434 Part 1+2).

``_mint_acting_identity_header`` mints the signed ``X-Omnigent-Acting-Identity``
carrier on the server→runner hop. That hop does not carry
``X-Bytedesk-Subject-Token``, so the mint reads the per-session stash and folds
the token into the carrier. Absent stash ⇒ today's carrier (no subject_token).
"""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.identity.signer import (
    HEADER_NAME,
    HmacAssertionSigner,
    decode_acting_identity,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.server.principal import Principal
from omnigent.server.routes.sessions import _mint_acting_identity_header
from omnigent.server.subject_token_stash import (
    SUBJECT_TOKEN_HEADER,
    stash_subject_token_from_headers,
)

_SECRET = "mint-fold-secret"


class _AuthProvider:
    """Minimal AuthProvider returning a fixed principal."""

    def __init__(self, principal: Principal | None):
        self._p = principal

    def get_principal(self, request):
        return self._p


def _request(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _state(with_signer=True):
    state = SimpleNamespace()
    state.assertion_signer = HmacAssertionSigner(_SECRET) if with_signer else None
    return state


def _decode(headers: dict[str, str]):
    return decode_acting_identity(headers[HEADER_NAME], HmacAssertionVerifier(_SECRET))


# ── folds the stashed token ──────────────────────────────────────────────────


def test_mint_folds_stashed_subject_token():
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "user-access-tok"})
    headers = _mint_acting_identity_header(
        _request(state),
        _AuthProvider(Principal(user_id="alice", tenant_id="t1")),
        agent_id="maya",
        session_id="conv_1",
    )
    assert headers is not None
    decoded = _decode(headers)
    assert decoded is not None
    assert decoded.subject_token == "user-access-tok"
    assert decoded.principal.user_id == "alice"
    assert decoded.agent_id == "maya"


# ── degrade: no stash ⇒ today's carrier, no subject_token ─────────────────────


def test_mint_without_stash_omits_subject_token():
    state = _state()
    headers = _mint_acting_identity_header(
        _request(state),
        _AuthProvider(Principal(user_id="alice")),
        agent_id="maya",
        session_id="conv_1",
    )
    assert headers is not None
    decoded = _decode(headers)
    assert decoded is not None
    assert decoded.subject_token is None


def test_mint_without_session_id_omits_subject_token():
    # Even with a stash present, no session_id ⇒ no lookup ⇒ unchanged carrier.
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "user-tok"})
    headers = _mint_acting_identity_header(
        _request(state),
        _AuthProvider(Principal(user_id="alice")),
        agent_id="maya",
    )
    assert headers is not None
    assert _decode(headers).subject_token is None


# ── existing degrade paths unchanged ─────────────────────────────────────────


def test_mint_no_principal_returns_none():
    state = _state()
    assert (
        _mint_acting_identity_header(
            _request(state), _AuthProvider(None), agent_id="maya", session_id="conv_1"
        )
        is None
    )


def test_mint_no_signer_returns_none():
    state = _state(with_signer=False)
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "user-tok"})
    assert (
        _mint_acting_identity_header(
            _request(state),
            _AuthProvider(Principal(user_id="alice")),
            agent_id="maya",
            session_id="conv_1",
        )
        is None
    )
