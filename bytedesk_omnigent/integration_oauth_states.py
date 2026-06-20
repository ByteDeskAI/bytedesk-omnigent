"""Deterministic OAuth state tokens for connected-app installs.

ByteDesk Platform starts third-party OAuth installs (Google Workspace, HubSpot,
Notion, etc.) and Omnigent receives the callback later. The callback needs a
small, tamper-evident handoff contract that binds the provider, workspace,
redirect URI, requested scopes, and installer nonce without storing secrets in the
browser or creating one-off glue per provider.

This module issues and verifies compact HMAC-signed state tokens. It is pure and
injectable so Platform, routes, and tests can use the same deterministic contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

_TOKEN_PREFIX = "omni-oauth-v1"
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3600


@dataclass(frozen=True)
class OAuthStateClaims:
    """Claims carried inside a signed connected-app OAuth state token."""

    provider: str
    workspace_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    install_id: str
    nonce: str
    issued_at: int
    expires_at: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize claims for API responses without exposing signing secrets."""
        return {
            "provider": self.provider,
            "workspace_id": self.workspace_id,
            "redirect_uri": self.redirect_uri,
            "scopes": list(self.scopes),
            "install_id": self.install_id,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class IssuedOAuthState:
    """A freshly issued state token and its decoded claims."""

    state: str
    claims: OAuthStateClaims

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "claims": self.claims.to_dict()}


@dataclass(frozen=True)
class OAuthStateVerification:
    """Verification result for a state token callback."""

    valid: bool
    reason: str
    claims: OAuthStateClaims | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "claims": self.claims.to_dict() if self.claims is not None else None,
        }


def issue_oauth_state(
    *,
    provider: str,
    workspace_id: str,
    redirect_uri: str,
    scopes: list[str] | tuple[str, ...],
    secret: str,
    install_id: str | None = None,
    nonce: str | None = None,
    now: int | None = None,
    ttl_seconds: int = 600,
) -> IssuedOAuthState:
    """Issue a signed OAuth state token for a connected-app install.

    :param provider: Third-party provider slug/name (normalized to a slug).
    :param workspace_id: ByteDesk workspace/tenant the install belongs to.
    :param redirect_uri: Callback URI the provider should return to.
    :param scopes: Requested OAuth scopes; duplicates are removed and sorted.
    :param secret: HMAC signing secret; never embedded in the token.
    :param install_id: Stable install attempt id, generated when omitted.
    :param nonce: CSRF nonce, generated when omitted.
    :param now: Epoch seconds for deterministic tests/harnesses.
    :param ttl_seconds: Token lifetime, clamped by validation to 60..3600 seconds.
    :raises ValueError: when required fields or TTL are invalid.
    """
    issued_at = int(time.time()) if now is None else int(now)
    claims = OAuthStateClaims(
        provider=_normalize_provider(provider),
        workspace_id=_require_non_empty("workspace_id", workspace_id),
        redirect_uri=_require_non_empty("redirect_uri", redirect_uri),
        scopes=_normalize_scopes(scopes),
        install_id=install_id or f"oauth_install_{uuid.uuid4().hex}",
        nonce=nonce or uuid.uuid4().hex,
        issued_at=issued_at,
        expires_at=issued_at + _validate_ttl(ttl_seconds),
    )
    payload = _encode_json(claims.to_dict())
    signature = _sign(payload, _require_non_empty("secret", secret))
    return IssuedOAuthState(state=f"{_TOKEN_PREFIX}.{payload}.{signature}", claims=claims)


def verify_oauth_state(
    state: str,
    *,
    secret: str,
    expected_provider: str | None = None,
    expected_workspace_id: str | None = None,
    now: int | None = None,
) -> OAuthStateVerification:
    """Verify a connected-app OAuth state token.

    Returns a structured failure reason instead of raising so callback routes can
    fail closed while preserving operator-observable diagnostics.
    """
    try:
        prefix, payload, signature = state.split(".", 2)
    except ValueError:
        return OAuthStateVerification(False, "malformed")
    if prefix != _TOKEN_PREFIX:
        return OAuthStateVerification(False, "wrong_version")
    expected_signature = _sign(payload, _require_non_empty("secret", secret))
    if not hmac.compare_digest(expected_signature, signature):
        return OAuthStateVerification(False, "bad_signature")
    try:
        claims = _claims_from_dict(_decode_json(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return OAuthStateVerification(False, "malformed")
    current = int(time.time()) if now is None else int(now)
    if current >= claims.expires_at:
        return OAuthStateVerification(False, "expired")
    if expected_provider is not None and claims.provider != _normalize_provider(expected_provider):
        return OAuthStateVerification(False, "provider_mismatch")
    if expected_workspace_id is not None and claims.workspace_id != expected_workspace_id:
        return OAuthStateVerification(False, "workspace_mismatch")
    return OAuthStateVerification(True, "ok", claims)


def _claims_from_dict(data: dict[str, Any]) -> OAuthStateClaims:
    return OAuthStateClaims(
        provider=_normalize_provider(str(data["provider"])),
        workspace_id=_require_non_empty("workspace_id", str(data["workspace_id"])),
        redirect_uri=_require_non_empty("redirect_uri", str(data["redirect_uri"])),
        scopes=_normalize_scopes(tuple(str(scope) for scope in data["scopes"])),
        install_id=_require_non_empty("install_id", str(data["install_id"])),
        nonce=_require_non_empty("nonce", str(data["nonce"])),
        issued_at=int(data["issued_at"]),
        expires_at=int(data["expires_at"]),
    )


def _normalize_provider(provider: str) -> str:
    value = _require_non_empty("provider", provider).strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        raise ValueError("provider is required")
    return value


def _normalize_scopes(scopes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
    if not normalized:
        raise ValueError("at least one scope is required")
    return normalized


def _validate_ttl(ttl_seconds: int) -> int:
    ttl = int(ttl_seconds)
    if ttl < _MIN_TTL_SECONDS or ttl > _MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be between 60 and 3600")
    return ttl


def _require_non_empty(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _encode_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64url(raw)


def _decode_json(value: str) -> dict[str, Any]:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    data = json.loads(decoded)
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    return data


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    return _b64url(digest.digest())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
