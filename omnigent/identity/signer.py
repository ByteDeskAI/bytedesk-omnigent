"""Sign + carry an :class:`ActingIdentity` across the server→runner boundary.

The inbound principal is resolved at the server route but the runner is a
separate process; to thread it to the point of action (a tool's
:class:`~omnigent.tools.base.ToolContext`) the server mints a short-lived signed
token and the runner verifies it. This module is the **additive inverse** of
:mod:`omnigent.identity.verifiers`:

- :class:`HmacAssertionSigner` produces a ``base64url(payload).base64url(hmac)``
  token that :class:`~omnigent.identity.verifiers.HmacAssertionVerifier` accepts
  (same secret env ``OMNIGENT_ASSERTION_HMAC_SECRET``, mandatory numeric ``exp``).
- :func:`encode_acting_identity` / :func:`decode_acting_identity` are the one
  shared codec for the ``ActingIdentity`` wire shape, so the server and the
  runner agree on exactly one format.

**Degrade-to-default is the whole safety story.** An unconfigured signer (no
secret), a ``None`` identity, or a ``None`` principal all encode to ``None`` — no
carrier is minted. A ``None``/absent/invalid/expired token decodes to ``None`` —
the runner falls back to ``acting_identity=None``, i.e. today's behaviour. A
missing token is never an error.

Import-light: this module imports only ``identity.*`` + stdlib (never
``omnigent.server.app``), so it is safe on the runner hot path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from omnigent.identity.defaults import acting_identity_for
from omnigent.identity.identity import ActingIdentity
from omnigent.identity.verifiers import (
    DEFAULT_SECRET_ENV,
    HmacAssertionVerifier,
)
from omnigent.server.principal import Principal

logger = logging.getLogger(__name__)

#: Internal header carrying the signed ActingIdentity token across the boundary.
HEADER_NAME = "X-Omnigent-Acting-Identity"

#: Body field carrying the same token for the body-only streaming dispatch path.
BODY_FIELD = "acting_identity"

#: Default token lifetime. A turn round-trip is sub-second; a short TTL bounds
#: replay of the symmetric token.
DEFAULT_TTL_S = 120.0


def _b64url_encode(raw: bytes) -> str:
    """URL-safe base64 without ``=`` padding (matches the verifier's decode)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class HmacAssertionSigner:
    """Mint a token the :class:`HmacAssertionVerifier` with the same secret accepts.

    :param secret: The shared HMAC signing secret (``str``/``bytes``). ``None``
        means *unconfigured* — :meth:`sign` returns ``None`` so no carrier is
        minted (degrade-to-default), exactly mirroring the verifier's no-secret
        fail-closed.
    :param default_ttl_s: Seconds added to ``iat`` to stamp ``exp`` when the
        claims omit it.
    """

    name = "hmac"

    def __init__(
        self, secret: str | bytes | None, *, default_ttl_s: float = DEFAULT_TTL_S
    ) -> None:
        if secret is None:
            self._secret: bytes | None = None
        elif isinstance(secret, str):
            self._secret = secret.encode("utf-8")
        else:
            self._secret = secret
        self._default_ttl_s = default_ttl_s

    @classmethod
    def from_env(cls, env: str = DEFAULT_SECRET_ENV, **kwargs: Any) -> HmacAssertionSigner:
        """Build a signer whose secret comes from *env* (``None`` if unset)."""
        return cls(os.environ.get(env) or None, **kwargs)

    def sign(self, claims: dict[str, Any], *, ttl_s: float | None = None) -> str | None:
        """Sign *claims* → a token, or ``None`` when unconfigured.

        Stamps a numeric ``exp`` (``= now + ttl``) when the claims omit one — the
        verifier rejects a token without ``exp``. The claims dict is not mutated.
        """
        if self._secret is None:
            return None
        payload = dict(claims)
        if "exp" not in payload:
            payload["exp"] = time.time() + (ttl_s if ttl_s is not None else self._default_ttl_s)
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def encode_acting_identity(
    identity: ActingIdentity | None, signer: HmacAssertionSigner
) -> str | None:
    """Encode an :class:`ActingIdentity` to a signed token, or ``None``.

    Returns ``None`` (no carrier) when *identity* or its principal is absent, or
    the signer is unconfigured — the degrade-to-default rule.
    """
    if identity is None or identity.principal is None:
        return None
    principal = identity.principal
    claims: dict[str, Any] = {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "roles": list(principal.roles),
        "agent_id": identity.agent_id,
        "delegation": list(identity.delegation),
    }
    # Omit the key entirely when absent so the no-OBO carrier stays byte-identical
    # to the pre-subject_token wire shape (never ``subject_token: null``).
    if identity.subject_token is not None:
        claims["subject_token"] = identity.subject_token
    return signer.sign(claims)


def decode_acting_identity(
    token: str | None, verifier: HmacAssertionVerifier
) -> ActingIdentity | None:
    """Verify *token* and rebuild the :class:`ActingIdentity`, or ``None``.

    Fail-closed/absent ⇒ ``None``: a ``None``/empty/invalid/expired token, an
    unconfigured verifier, or a payload missing ``user_id`` all yield ``None`` so
    the runner falls back to ``acting_identity=None`` (today's behaviour).
    """
    if not token:
        return None
    payload = verifier.verify(token)
    if payload is None:
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    tenant_id = payload.get("tenant_id")
    roles = payload.get("roles")
    principal = Principal(
        user_id=user_id,
        tenant_id=tenant_id if isinstance(tenant_id, str) and tenant_id else None,
        roles=tuple(r for r in roles if isinstance(r, str)) if isinstance(roles, list) else (),
        claims={},
    )
    agent_id = payload.get("agent_id")
    delegation = payload.get("delegation")
    subject_token = payload.get("subject_token")
    return acting_identity_for(
        principal,
        agent_id=agent_id if isinstance(agent_id, str) and agent_id else None,
        delegation=[d for d in delegation if isinstance(d, str)]
        if isinstance(delegation, list)
        else (),
        subject_token=subject_token if isinstance(subject_token, str) and subject_token else None,
    )


__all__ = [
    "BODY_FIELD",
    "DEFAULT_TTL_S",
    "HEADER_NAME",
    "HmacAssertionSigner",
    "decode_acting_identity",
    "encode_acting_identity",
]
