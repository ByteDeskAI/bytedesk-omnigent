"""Inbound assertion verifiers (the trust subpart).

Two interchangeable verifiers of the same compact
``base64url(payload).base64url(signature)`` token shape, both enforcing the
require-``exp`` invariant:

- :class:`HmacAssertionVerifier` — shared-secret HMAC-SHA256. The holder of the
  secret can *forge* any payload, so a verified assertion is an identity *claim*,
  never an authorization grant. Fine for omnigent-internal carriers (server →
  runner) where both sides are omnigent.
- :class:`RsaAssertionVerifier` — RSA PKCS#1 v1.5 + SHA-256 (BDP-2424). Only the
  holder of the PRIVATE key (the platform/Office gateway) can mint; omnigent
  holds the PUBLIC key and can verify but NEVER forge. This is the asymmetric
  hardening for the inbound Office→omnigent principal: it closes the
  confused-deputy/forgery hole the symmetric HMAC leaves open.

Both satisfy the ``AssertionVerifier`` port (``name`` + ``verify``), so the
trust mechanism is swappable under the ``assertion_verifier`` seam.

**Secure-default invariant:** a verified payload MUST carry a numeric ``exp`` (a
missing/non-numeric ``exp`` is a verification FAILURE, never "never expires").

Import-light: ``cryptography`` is imported lazily inside the RSA paths, so the
default HMAC verifier and the runner hot path pull no asymmetric crypto.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

logger = logging.getLogger(__name__)

#: Generic env var the core default HMAC verifier reads its secret from.
DEFAULT_SECRET_ENV = "OMNIGENT_ASSERTION_HMAC_SECRET"

#: Env var the RSA verifier reads its PEM public key from (a public key is not a
#: secret — it is safe to distribute to every verifier via plain config).
DEFAULT_RSA_PUBLIC_KEY_ENV = "OMNIGENT_ASSERTION_RSA_PUBLIC_KEY"

#: Default clock-skew tolerance (seconds) applied to the ``exp`` check.
DEFAULT_CLOCK_SKEW_S = 60.0


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string without padding; raises on malformed input."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _split_token(header: str) -> tuple[bytes, bytes] | None:
    """Split a compact token into ``(payload_bytes, signature_bytes)``, or ``None``.

    ``None`` on a malformed shape or undecodable base64 (logged at WARNING).
    """
    payload_b64, sep, sig_b64 = header.partition(".")
    if not sep or not payload_b64 or not sig_b64:
        logger.warning("assertion rejected: malformed token shape")
        return None
    try:
        return _b64url_decode(payload_b64), _b64url_decode(sig_b64)
    except (binascii.Error, ValueError):
        logger.warning("assertion rejected: undecodable token")
        return None


def _payload_if_valid(
    payload_bytes: bytes, *, clock_skew_s: float, require_exp: bool
) -> dict[str, Any] | None:
    """Parse payload JSON + enforce the ``exp`` invariant; ``None`` on failure.

    Shared by both verifiers so the secure-default ``exp`` logic (including the
    ``isinstance(True, int)`` bool footgun) is identical for HMAC and RSA.
    """
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("assertion rejected: payload not valid JSON")
        return None
    if not isinstance(payload, dict):
        logger.warning("assertion rejected: payload not an object")
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        # Secure default: a missing/non-numeric exp must NOT be accepted as
        # "never expires". ``bool`` is excluded explicitly because
        # ``isinstance(True, int)`` is True in Python.
        if require_exp:
            logger.warning("assertion rejected: missing or non-numeric exp")
            return None
    elif time.time() > exp + clock_skew_s:
        logger.warning("assertion rejected: token expired")
        return None
    return payload


class HmacAssertionVerifier:
    """Verify a ``base64url(payload).base64url(hmac_sha256(secret, payload))`` token.

    :param secret: The shared HMAC signing secret (``str``/``bytes``). ``None``
        means *unconfigured* — :meth:`verify` then fail-closes on everything, so
        the registry default never raises at construction in a standalone deploy.
    :param clock_skew_s: Tolerance added to ``exp`` before treating a token as
        expired.
    :param require_exp: Require a numeric ``exp`` claim (default ``True``).
    """

    name = "hmac"

    def __init__(
        self,
        secret: str | bytes | None,
        *,
        clock_skew_s: float = DEFAULT_CLOCK_SKEW_S,
        require_exp: bool = True,
    ) -> None:
        if secret is None:
            self._secret: bytes | None = None
        elif isinstance(secret, str):
            self._secret = secret.encode("utf-8")
        else:
            self._secret = secret
        self._clock_skew_s = clock_skew_s
        self._require_exp = require_exp

    @classmethod
    def from_env(cls, env: str = DEFAULT_SECRET_ENV, **kwargs: Any) -> HmacAssertionVerifier:
        """Build a verifier whose secret comes from *env* (``None`` if unset)."""
        return cls(os.environ.get(env) or None, **kwargs)

    def verify(self, header: str) -> dict[str, Any] | None:
        """Verify the signed token and return its payload dict, or ``None``.

        Fail-closed on: no secret configured, malformed shape, bad base64,
        signature mismatch (constant-time), non-object/non-JSON payload, or a
        missing/non-numeric/expired ``exp``.
        """
        if self._secret is None:
            logger.debug("assertion rejected: hmac verifier has no secret configured")
            return None
        decoded = _split_token(header)
        if decoded is None:
            return None
        payload_bytes, provided_sig = decoded
        expected_sig = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, provided_sig):
            logger.warning("assertion rejected: signature mismatch")
            return None
        return _payload_if_valid(
            payload_bytes, clock_skew_s=self._clock_skew_s, require_exp=self._require_exp
        )


def _load_rsa_public_key(pem: str | bytes) -> RSAPublicKey:
    """Load an RSA public key from PEM (lazy ``cryptography`` import)."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    data = pem.encode("utf-8") if isinstance(pem, str) else pem
    # load_pem_public_key returns a generic public key; callers pass an RSA PEM.
    return load_pem_public_key(data)  # type: ignore[return-value]


class RsaAssertionVerifier:
    """Verify ``base64url(payload).base64url(rsa_pkcs1v15_sha256(payload))`` with a public key.

    Asymmetric (BDP-2424): the signer holds the private key; omnigent holds only
    the public key, so it can verify but cannot forge. Same compact token shape
    and require-``exp`` invariant as :class:`HmacAssertionVerifier`.

    :param public_key: An ``RSAPublicKey``, or ``None`` (unconfigured →
        :meth:`verify` fail-closes on everything).
    """

    name = "rsa"

    def __init__(
        self,
        public_key: RSAPublicKey | None,
        *,
        clock_skew_s: float = DEFAULT_CLOCK_SKEW_S,
        require_exp: bool = True,
    ) -> None:
        self._public_key = public_key
        self._clock_skew_s = clock_skew_s
        self._require_exp = require_exp

    @classmethod
    def from_pem(cls, pem: str | bytes, **kwargs: Any) -> RsaAssertionVerifier:
        """Build a verifier from a PEM public key string."""
        return cls(_load_rsa_public_key(pem), **kwargs)

    @classmethod
    def from_env(
        cls, env: str = DEFAULT_RSA_PUBLIC_KEY_ENV, **kwargs: Any
    ) -> RsaAssertionVerifier:
        """Build a verifier whose PEM public key comes from *env* (``None`` if unset)."""
        pem = os.environ.get(env)
        return cls(_load_rsa_public_key(pem) if pem else None, **kwargs)

    def verify(self, header: str) -> dict[str, Any] | None:
        """Verify the RSA-signed token and return its payload dict, or ``None``.

        Fail-closed on: no public key configured, malformed shape, bad base64,
        signature mismatch, non-object/non-JSON payload, or a
        missing/non-numeric/expired ``exp``.
        """
        if self._public_key is None:
            logger.debug("assertion rejected: rsa verifier has no public key configured")
            return None
        decoded = _split_token(header)
        if decoded is None:
            return None
        payload_bytes, signature = decoded
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            self._public_key.verify(signature, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            logger.warning("assertion rejected: rsa signature mismatch")
            return None
        except Exception:  # noqa: BLE001 — any verify error is a fail-closed rejection
            logger.warning("assertion rejected: rsa verify error", exc_info=True)
            return None
        return _payload_if_valid(
            payload_bytes, clock_skew_s=self._clock_skew_s, require_exp=self._require_exp
        )


__all__ = [
    "DEFAULT_CLOCK_SKEW_S",
    "DEFAULT_RSA_PUBLIC_KEY_ENV",
    "DEFAULT_SECRET_ENV",
    "HmacAssertionVerifier",
    "RsaAssertionVerifier",
]
