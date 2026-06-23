"""Unit tests for RsaAssertionVerifier (BDP-2424 asymmetric principal signing).

The point of the asymmetric scheme: only the holder of the PRIVATE key (Office)
can mint a token the verifier accepts; a holder of just the public key (omnigent)
can verify but NEVER forge. The forgery-rejection test pins exactly that.
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from omnigent.identity.verifiers import RsaAssertionVerifier

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_PUB_PEM = (
    _KEY.public_key()
    .public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mint(payload: dict, key: rsa.RSAPrivateKey = _KEY) -> str:
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = key.sign(payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    return f"{_b64(payload_bytes)}.{_b64(sig)}"


def _verifier() -> RsaAssertionVerifier:
    return RsaAssertionVerifier.from_pem(_PUB_PEM)


def test_valid_rsa_token_verifies():
    payload = {"user_id": "alice@x", "exp": time.time() + 300}
    assert _verifier().verify(_mint(payload)) == payload


def test_from_pem_accepts_bytes():
    v = RsaAssertionVerifier.from_pem(_PUB_PEM.encode("ascii"))
    assert v.verify(_mint({"user_id": "a", "exp": time.time() + 300})) is not None


# ── the asymmetric security property: omnigent cannot forge ───────────────────


def test_token_signed_by_other_key_is_rejected():
    # A holder of only the public key (or a different private key) cannot mint a
    # token this verifier accepts — the whole point of asymmetric signing.
    forged = _mint({"user_id": "alice@x", "exp": time.time() + 300}, key=_OTHER_KEY)
    assert _verifier().verify(forged) is None


def test_tampered_payload_is_rejected():
    # Sign one payload, swap the payload segment for another → signature no longer
    # matches → rejected.
    good = _mint({"user_id": "alice@x", "exp": time.time() + 300})
    _payload_b64, _, sig_b64 = good.partition(".")
    other_payload = _b64(json.dumps({"user_id": "mallory@x", "exp": time.time() + 300}).encode())
    assert _verifier().verify(f"{other_payload}.{sig_b64}") is None


# ── shared invariants (exp, shape) carry over from the HMAC verifier ──────────


def test_rsa_rejects_missing_exp():
    assert _verifier().verify(_mint({"user_id": "alice"})) is None


def test_rsa_rejects_non_numeric_and_bool_exp():
    assert _verifier().verify(_mint({"user_id": "a", "exp": "soon"})) is None
    assert _verifier().verify(_mint({"user_id": "a", "exp": True})) is None


def test_rsa_rejects_expired():
    assert _verifier().verify(_mint({"user_id": "a", "exp": time.time() - 1000})) is None


def test_rsa_rejects_malformed():
    v = _verifier()
    for bad in ("", "no-dot", "a.b.c", "not-b64!.also-bad!"):
        assert v.verify(bad) is None


# ── unconfigured ⇒ fail closed (no key trusts nothing) ────────────────────────


def test_unconfigured_verifier_fails_closed():
    v = RsaAssertionVerifier(None)
    assert v.verify(_mint({"user_id": "a", "exp": time.time() + 300})) is None


def test_from_env_unset_fails_closed(monkeypatch):
    monkeypatch.delenv("OMNIGENT_ASSERTION_RSA_PUBLIC_KEY", raising=False)
    v = RsaAssertionVerifier.from_env()
    assert v.verify(_mint({"user_id": "a", "exp": time.time() + 300})) is None


def test_from_env_loads_pem(monkeypatch):
    monkeypatch.setenv("OMNIGENT_ASSERTION_RSA_PUBLIC_KEY", _PUB_PEM)
    v = RsaAssertionVerifier.from_env()
    assert v.verify(_mint({"user_id": "alice", "exp": time.time() + 300})) is not None


def test_conforms_to_assertion_verifier_protocol():
    from omnigent.identity.ports import AssertionVerifier

    assert isinstance(RsaAssertionVerifier(None), AssertionVerifier)
    assert RsaAssertionVerifier(None).name == "rsa"
