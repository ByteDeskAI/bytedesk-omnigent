"""Deterministic OAuth token-refresh plan compiler.

This module deliberately does not call OAuth providers or read secrets. It turns
connected-app metadata into a stable workflow description that an Omnigent agent,
worker, or ByteDesk Platform control plane can execute under locks,
idempotency, vault-backed secret resolution, and explicit health probes.
"""

from __future__ import annotations

from collections.abc import Iterable


def _require_non_empty(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _slug_provider(provider: str) -> str:
    """Normalize a provider label into a deterministic integration slug."""
    value = _require_non_empty("provider", provider).lower().replace("_", "-")
    parts = [part for part in value.replace("/", "-").split() if part]
    return "-".join(parts)


def _dedupe_scopes(scopes: Iterable[str]) -> list[str]:
    """Trim scopes, drop blanks, and preserve first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for scope in scopes:
        trimmed = scope.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        result.append(trimmed)
    return result


def compile_oauth_refresh_plan(
    *,
    provider: str,
    connected_app_id: str,
    token_url: str,
    refresh_token_secret_ref: str,
    scopes: Iterable[str] = (),
) -> dict:
    """Compile a deterministic OAuth refresh workflow for one connected app.

    The returned dict is intentionally JSON-serializable and side-effect-free:
    it carries provider/app identifiers, the token endpoint, vault secret refs,
    the idempotency/lock keys an executor should use, ordered execution steps,
    and rollback behavior. Secret values never appear in the plan.
    """
    provider_slug = _slug_provider(provider)
    app_id = _require_non_empty("connected_app_id", connected_app_id)
    endpoint = _require_non_empty("token_url", token_url)
    refresh_ref = _require_non_empty(
        "refresh_token_secret_ref", refresh_token_secret_ref
    )

    return {
        "kind": "oauth_refresh_plan",
        "provider": provider_slug,
        "connected_app_id": app_id,
        "token_url": endpoint,
        "secret_refs": {"refresh_token": refresh_ref},
        "idempotency_scope": f"oauth-refresh:{provider_slug}",
        "lock_key": f"oauth-refresh:{provider_slug}:{app_id}",
        "steps": [
            "claim_refresh_lock",
            "resolve_refresh_token_secret",
            "request_token_refresh",
            "persist_rotated_token_material",
            "run_provider_health_probe",
            "release_refresh_lock",
        ],
        "required_scopes": _dedupe_scopes(scopes),
        "rollback": {
            "on_refresh_failure": "keep_existing_access_token_until_expiry",
            "on_persist_failure": "dead_letter_refresh_attempt_for_operator_review",
        },
    }


__all__ = ["compile_oauth_refresh_plan"]
