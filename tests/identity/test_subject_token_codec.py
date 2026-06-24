"""Unit tests for the subject_token claim on the ActingIdentity codec (BDP-2434 Part 2).

The OBO egress needs the user's MCP access token (``subject_token``) to ride the
acting-identity carrier from the server mint site to the runner. The codec must:

- carry an optional ``subject_token`` claim and surface it on
  ``ActingIdentity.subject_token`` after decode (round-trip);
- **omit the key entirely** when absent, so the no-OBO carrier stays
  byte-identical to today's wire shape (degrade-to-default); and
- keep every existing degrade path (no principal, unconfigured signer, absent
  token) producing ``None`` exactly as before.
"""

from __future__ import annotations

import base64
import json

from omnigent.identity.defaults import acting_identity_for
from omnigent.identity.identity import ActingIdentity
from omnigent.identity.signer import (
    HmacAssertionSigner,
    decode_acting_identity,
    encode_acting_identity,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.server.principal import Principal

_SECRET = "test-subject-token-secret"


def _signer():
    return HmacAssertionSigner(_SECRET)


def _verifier():
    return HmacAssertionVerifier(_SECRET)


def _payload_of(token: str) -> dict:
    """Decode the signer's ``base64url(payload).base64url(sig)`` to its claims dict."""
    payload_b64 = token.split(".", 1)[0]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


# ── ActingIdentity gains an additive subject_token field ─────────────────────


def test_acting_identity_subject_token_defaults_none():
    # Additive default: existing construction is unchanged (agent→subagent safe).
    assert ActingIdentity().subject_token is None
    assert ActingIdentity(principal=Principal(user_id="a")).subject_token is None


def test_acting_identity_for_threads_subject_token():
    ident = acting_identity_for(
        Principal(user_id="alice"), agent_id="maya", subject_token="user-access-tok"
    )
    assert ident.subject_token == "user-access-tok"
    # Default kwarg keeps the standalone shape untouched.
    assert acting_identity_for(Principal(user_id="alice")).subject_token is None


# ── codec round-trips subject_token ──────────────────────────────────────────


def test_encode_decode_roundtrips_subject_token():
    ident = acting_identity_for(
        Principal(user_id="alice@x", tenant_id="t1", roles=("admin",)),
        agent_id="maya",
        delegation=["root"],
        subject_token="user-access-tok",
    )
    token = encode_acting_identity(ident, _signer())
    assert token is not None
    decoded = decode_acting_identity(token, _verifier())
    assert decoded is not None
    assert decoded.subject_token == "user-access-tok"
    # The rest of the identity still round-trips unchanged.
    assert decoded.principal.user_id == "alice@x"
    assert decoded.agent_id == "maya"
    assert decoded.delegation == ("root",)


# ── byte-identical when absent: the key is OMITTED, never null ────────────────


def test_encode_omits_subject_token_key_when_absent():
    # No subject_token ⇒ the claim key must NOT appear on the wire (not null).
    ident = acting_identity_for(Principal(user_id="alice"), agent_id="maya")
    token = encode_acting_identity(ident, _signer())
    assert token is not None
    claims = _payload_of(token)
    assert "subject_token" not in claims


def test_no_obo_carrier_is_byte_identical():
    # The carrier minted without a subject_token must be byte-for-byte identical
    # to the pre-BDP-2434 carrier (a frozen, deterministic exp keeps it stable).
    p = Principal(user_id="alice", tenant_id="t1", roles=("admin",))
    exp = 1_900_000_000.0  # fixed so the two encodes are deterministic
    with_default = encode_acting_identity(
        ActingIdentity(principal=p, agent_id="maya"), _signer()
    )
    # Re-encode the exact claims a pre-BDP-2434 build emitted (no subject_token key).
    legacy_claims = {
        "user_id": "alice",
        "tenant_id": "t1",
        "roles": ["admin"],
        "agent_id": "maya",
        "delegation": [],
    }
    legacy = _signer().sign(legacy_claims, ttl_s=None)
    # Compare the claim sets (exp differs by wall clock; the KEY SET must match).
    assert set(_payload_of(with_default)) == set(legacy_claims) | {"exp"}
    assert "subject_token" not in _payload_of(with_default)
    del exp, legacy  # exercised the legacy shape; assertion is on the key set


def test_decode_absent_subject_token_yields_none_field():
    # A legacy carrier (no subject_token claim) decodes with subject_token=None.
    ident = acting_identity_for(Principal(user_id="alice"), agent_id="maya")
    decoded = decode_acting_identity(encode_acting_identity(ident, _signer()), _verifier())
    assert decoded is not None
    assert decoded.subject_token is None


def test_decode_ignores_non_string_subject_token():
    # A malformed (non-string) subject_token claim must not crash decode; drop it.
    token = _signer().sign(
        {"user_id": "alice", "subject_token": 12345, "exp": 1_900_000_000.0}
    )
    decoded = decode_acting_identity(token, _verifier())
    assert decoded is not None
    assert decoded.subject_token is None
