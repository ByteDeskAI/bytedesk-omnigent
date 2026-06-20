"""Deterministic OAuth authorization URL compiler for external integrations.

This module owns the first install-click handoff for popular SaaS integrations:
given a provider, client id, redirect URI, signed state token, and optional scopes,
it returns the exact authorization URL the ByteDesk Platform should send the admin
to. It deliberately stores no secrets and performs no network I/O; the caller owns
state-token creation and the callback code exchange.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode


class UnknownOAuthProviderError(ValueError):
    """Raised when no OAuth authorization endpoint is registered for a provider."""


class InvalidOAuthAuthorizationRequest(ValueError):
    """Raised when a required authorization URL input is absent."""


@dataclass(frozen=True)
class OAuthProviderSpec:
    """Static authorization-endpoint metadata for a third-party provider."""

    provider: str
    authorization_endpoint: str
    default_scopes: tuple[str, ...]
    scope_separator: str = " "
    response_type: str = "code"
    extra_params: Mapping[str, str] | None = None


@dataclass(frozen=True)
class OAuthAuthorizationUrl:
    """Compiled OAuth authorization URL plus the normalized scopes it carries."""

    provider: str
    url: str
    scopes: tuple[str, ...]


OAUTH_PROVIDER_SPECS: dict[str, OAuthProviderSpec] = {
    "slack": OAuthProviderSpec(
        provider="slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        default_scopes=("channels:read", "chat:write"),
        scope_separator=",",
    ),
    "github": OAuthProviderSpec(
        provider="github",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        default_scopes=("repo", "read:org"),
    ),
    "linear": OAuthProviderSpec(
        provider="linear",
        authorization_endpoint="https://linear.app/oauth/authorize",
        default_scopes=("read", "write"),
        scope_separator=",",
    ),
    "notion": OAuthProviderSpec(
        provider="notion",
        authorization_endpoint="https://api.notion.com/v1/oauth/authorize",
        default_scopes=(),
        extra_params={"owner": "user"},
    ),
    "google-workspace": OAuthProviderSpec(
        provider="google-workspace",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        default_scopes=(
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/drive.file",
        ),
        extra_params={"access_type": "offline", "prompt": "consent"},
    ),
}


def compile_oauth_authorization_url(
    *,
    provider: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str] | None = None,
    extra_params: Mapping[str, str] | None = None,
) -> OAuthAuthorizationUrl:
    """Compile a deterministic OAuth authorization URL for *provider*.

    ``state`` is mandatory so callers carry a signed tenant/install token through
    the third-party OAuth redirect without this compiler needing secret access.
    Scopes are deduplicated while preserving order; when absent, provider defaults
    are used.
    """
    provider_key = _required("provider", provider).lower()
    spec = OAUTH_PROVIDER_SPECS.get(provider_key)
    if spec is None:
        known = ", ".join(sorted(OAUTH_PROVIDER_SPECS))
        raise UnknownOAuthProviderError(
            f"unknown OAuth provider {provider!r}; known providers: {known}"
        )

    requested_scopes = tuple(scopes) if scopes is not None else spec.default_scopes
    normalized_scopes = _dedupe_scopes(requested_scopes)
    params: dict[str, str] = {
        "client_id": _required("client_id", client_id),
        "redirect_uri": _required("redirect_uri", redirect_uri),
        "response_type": spec.response_type,
        "state": _required("state", state),
    }
    if normalized_scopes:
        params["scope"] = spec.scope_separator.join(normalized_scopes)
    if spec.extra_params:
        params.update({key: value for key, value in spec.extra_params.items() if value})
    if extra_params:
        params.update({key: value for key, value in extra_params.items() if value})

    return OAuthAuthorizationUrl(
        provider=spec.provider,
        url=f"{spec.authorization_endpoint}?{urlencode(params)}",
        scopes=normalized_scopes,
    )


def _required(name: str, value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise InvalidOAuthAuthorizationRequest(f"{name} is required")
    return normalized


def _dedupe_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for scope in scopes:
        normalized = scope.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)
