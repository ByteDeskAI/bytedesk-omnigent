"""VAPID key loading for Web Push."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


@dataclass(frozen=True)
class VapidKeys:
    """VAPID key pair for signing Web Push requests."""

    public_key: str
    private_key: str
    claims_email: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_ephemeral_vapid_keys(claims_email: str = "admin@omnigent.local") -> VapidKeys:
    """Generate a fresh P-256 VAPID key pair (dev / test)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return VapidKeys(
        public_key=_b64url(public_bytes),
        private_key=_b64url(private_bytes),
        claims_email=claims_email,
    )


def load_vapid_keys() -> VapidKeys | None:
    """
    Load VAPID keys from environment.

    ``OMNIGENT_VAPID_PUBLIC_KEY`` / ``OMNIGENT_VAPID_PRIVATE_KEY`` must be
    url-safe base64. When unset, returns ephemeral keys (dev only).
    """
    public_key = os.environ.get("OMNIGENT_VAPID_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("OMNIGENT_VAPID_PRIVATE_KEY", "").strip()
    claims_email = os.environ.get("OMNIGENT_VAPID_CLAIMS_EMAIL", "admin@omnigent.local").strip()
    if public_key and private_key:
        return VapidKeys(public_key=public_key, private_key=private_key, claims_email=claims_email)
    if os.environ.get("OMNIGENT_VAPID_EPHEMERAL", "1") == "0":
        return None
    return generate_ephemeral_vapid_keys(claims_email=claims_email)