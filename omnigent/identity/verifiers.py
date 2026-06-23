"""Inbound assertion verifiers (the trust subpart).

:class:`HmacAssertionVerifier` is the in-box default: it factors out the
shared-secret HMAC-SHA256 verification that was hardwired inside
``bytedesk_omnigent.auth.principal_resolver.ByteDeskPrincipalResolver._verify``
so the trust mechanism becomes an independently swappable port. A consumer can
register a JWKS / OIDC-introspect verifier under the ``assertion_verifier`` seam
without touching how the header is parsed or how claims map to a principal.

**Secure-default invariant (ADR):** a verified payload MUST carry a numeric
``exp``. The original inline check skipped expiry when ``exp`` was absent or
non-numeric — a never-expiring assertion. Here a missing/non-numeric ``exp`` is
a verification FAILURE (``None``). A real gateway always stamps ``exp``.

The shared HMAC secret is symmetric: a holder can *forge* any payload, so a
verified assertion is an identity *claim*, never an authorization grant.
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
from typing import Any

logger = logging.getLogger(__name__)

#: Generic env var the core default verifier reads its secret from. Consumers
#: with their own secret (e.g. the ByteDesk gateway secret) construct a
#: configured verifier directly instead of relying on this env.
DEFAULT_SECRET_ENV = "OMNIGENT_ASSERTION_HMAC_SECRET"

#: Default clock-skew tolerance (seconds) applied to the ``exp`` check.
DEFAULT_CLOCK_SKEW_S = 60.0


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string without padding; raises on malformed input."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class HmacAssertionVerifier:
    """Verify a ``base64url(payload).base64url(hmac_sha256(secret, payload))`` token.

    :param secret: The shared HMAC signing secret (``str``/``bytes``). ``None``
        means *unconfigured* — :meth:`verify` then fail-closes on everything (a
        verifier with no secret trusts nothing), so the registry default never
        raises at construction in a standalone deployment.
    :param clock_skew_s: Tolerance added to ``exp`` before treating a token as
        expired.
    :param require_exp: Require a numeric ``exp`` claim (default ``True`` — the
        secure default). Set ``False`` only for a deliberately exp-less scheme.
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
        signature mismatch (constant-time), non-object/non-JSON payload, a
        missing/non-numeric ``exp`` (when required), or an expired ``exp``.
        """
        if self._secret is None:
            logger.debug("assertion rejected: verifier has no secret configured")
            return None

        payload_b64, sep, sig_b64 = header.partition(".")
        if not sep or not payload_b64 or not sig_b64:
            logger.warning("assertion rejected: malformed token shape")
            return None

        try:
            payload_bytes = _b64url_decode(payload_b64)
            provided_sig = _b64url_decode(sig_b64)
        except (binascii.Error, ValueError):
            logger.warning("assertion rejected: undecodable token")
            return None

        expected_sig = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, provided_sig):
            logger.warning("assertion rejected: signature mismatch")
            return None

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
            # "never expires" (the prior fail-open). ``bool`` is excluded
            # explicitly because ``isinstance(True, int)`` is True in Python.
            if self._require_exp:
                logger.warning("assertion rejected: missing or non-numeric exp")
                return None
        elif time.time() > exp + self._clock_skew_s:
            logger.warning("assertion rejected: token expired")
            return None

        return payload


__all__ = ["DEFAULT_CLOCK_SKEW_S", "DEFAULT_SECRET_ENV", "HmacAssertionVerifier"]
