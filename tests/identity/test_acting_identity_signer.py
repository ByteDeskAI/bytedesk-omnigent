"""Unit tests for the ActingIdentity signer + codec (BDP-2422 Phase 1a).

The signer is the additive inverse of HmacAssertionVerifier: a token it mints
must verify, the codec must round-trip an ActingIdentity, and every degrade path
(unconfigured signer, no principal, absent/invalid token) must yield None so the
runner falls back to today's behaviour.
"""

from __future__ import annotations

import time

from omnigent.identity.defaults import acting_identity_for
from omnigent.identity.identity import ActingIdentity
from omnigent.identity.signer import (
    HmacAssertionSigner,
    decode_acting_identity,
    encode_acting_identity,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.server.principal import Principal

_SECRET = "test-acting-identity-secret"


def _signer(secret=_SECRET, **kw):
    return HmacAssertionSigner(secret, **kw)


def _verifier(secret=_SECRET):
    return HmacAssertionVerifier(secret)


# ── signer ↔ verifier round-trip ─────────────────────────────────────────────


def test_sign_then_verify_roundtrips():
    exp = time.time() + 300
    token = _signer().sign({"user_id": "alice", "exp": exp})
    assert _verifier().verify(token) == {"user_id": "alice", "exp": exp}


def test_sign_stamps_exp_when_absent():
    # The verifier REQUIRES a numeric exp; the signer must stamp one.
    token = _signer().sign({"user_id": "alice"})
    payload = _verifier().verify(token)
    assert payload is not None and isinstance(payload["exp"], (int, float))


def test_sign_expired_ttl_is_rejected():
    token = _signer().sign({"user_id": "alice"}, ttl_s=-1000)
    assert _verifier().verify(token) is None


def test_unconfigured_signer_returns_none():
    assert HmacAssertionSigner(None).sign({"user_id": "alice"}) is None


def test_wrong_secret_does_not_verify():
    token = _signer(secret="one").sign({"user_id": "alice", "exp": time.time() + 300})
    assert HmacAssertionVerifier("two").verify(token) is None


# ── ActingIdentity codec ─────────────────────────────────────────────────────


def test_encode_decode_roundtrip():
    ident = acting_identity_for(
        Principal(user_id="alice@x", tenant_id="t1", roles=("admin", "member")),
        agent_id="maya",
        delegation=["root"],
    )
    token = encode_acting_identity(ident, _signer())
    assert token is not None
    decoded = decode_acting_identity(token, _verifier())
    assert decoded is not None
    assert decoded.principal.user_id == "alice@x"
    assert decoded.principal.tenant_id == "t1"
    assert decoded.principal.roles == ("admin", "member")
    assert decoded.agent_id == "maya"
    assert decoded.delegation == ("root",)


def test_encode_decode_roundtrip_minimal_principal():
    # tenant None, no roles/agent — the standalone/local shape.
    ident = acting_identity_for(Principal(user_id="local"))
    decoded = decode_acting_identity(encode_acting_identity(ident, _signer()), _verifier())
    assert decoded is not None
    assert decoded.principal.user_id == "local"
    assert decoded.principal.tenant_id is None
    assert decoded.principal.roles == ()
    assert decoded.agent_id is None


def test_encode_none_identity_or_principal_returns_none():
    assert encode_acting_identity(None, _signer()) is None
    assert encode_acting_identity(ActingIdentity(principal=None, agent_id="a"), _signer()) is None


def test_encode_unconfigured_signer_returns_none():
    ident = acting_identity_for(Principal(user_id="alice"))
    assert encode_acting_identity(ident, HmacAssertionSigner(None)) is None


def test_decode_absent_or_bad_token_returns_none():
    v = _verifier()
    assert decode_acting_identity(None, v) is None
    assert decode_acting_identity("", v) is None
    assert decode_acting_identity("garbage", v) is None
    assert decode_acting_identity("a.b.c", v) is None
    # valid shape, wrong secret
    bad = HmacAssertionSigner("other").sign({"user_id": "alice", "exp": time.time() + 300})
    assert decode_acting_identity(bad, v) is None


def test_decode_payload_missing_user_id_returns_none():
    # A signed-but-userless token must fail closed (no identity).
    token = _signer().sign({"tenant_id": "t1", "exp": time.time() + 300})
    assert decode_acting_identity(token, _verifier()) is None
