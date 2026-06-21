"""Deterministic third-party integration contract fingerprints.

Omnigent integrations are assembled from OAuth scopes, webhook event bindings,
source-specific headers, and agent actions. Before an integration is activated we
need a small, deterministic review handle that says: "this is the exact external
contract this agent will rely on."  The compiler below normalizes that contract
into canonical JSON and fingerprints it, so activation gates, PRs, and ByteDesk
Platform can detect drift or permission expansion without storing secrets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "IntegrationContract",
    "IntegrationContractFingerprint",
    "compile_integration_contract_fingerprint",
]


@dataclass(frozen=True)
class IntegrationContract:
    """Declarative external-service contract for one Omnigent integration.

    :param source: Third-party source name, e.g. ``"github"`` or ``"notion"``.
    :param auth: Auth mechanism, e.g. ``"oauth2"`` or ``"hmac"``.
    :param events: Webhook/event names the integration consumes.
    :param scopes: OAuth/API scopes or permissions the integration needs.
    :param webhook_headers: Required webhook headers and their requirement notes.
    :param actions: Agent/runtime actions unlocked by this contract.
    """

    source: str
    auth: str
    events: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    webhook_headers: dict[str, str] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IntegrationContractFingerprint:
    """Stable review summary for an :class:`IntegrationContract`."""

    fingerprint: str
    canonical: dict[str, Any]
    review_tags: list[str]


def compile_integration_contract_fingerprint(
    contract: IntegrationContract,
) -> IntegrationContractFingerprint:
    """Compile *contract* into canonical JSON plus a stable SHA-256 fingerprint.

    The compiler intentionally ignores caller ordering and duplicate list entries:
    the same requested integration contract should review to the same handle no
    matter which catalog, planner, or agent emitted it first. Any material change
    (for example a new OAuth scope) changes the fingerprint and can block or
    re-request approval before activation.
    """
    canonical = {
        "actions": _normalized_list(contract.actions),
        "auth": _normalized_token(contract.auth),
        "events": _normalized_list(contract.events),
        "scopes": _normalized_list(contract.scopes),
        "source": _normalized_token(contract.source),
        "webhook_headers": _normalized_headers(contract.webhook_headers),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    review_tags = [
        f"source:{canonical['source']}",
        f"auth:{canonical['auth']}",
        f"events:{len(canonical['events'])}",
        f"scopes:{len(canonical['scopes'])}",
        f"actions:{len(canonical['actions'])}",
    ]
    return IntegrationContractFingerprint(
        fingerprint=f"icf_{digest[:24]}",
        canonical=canonical,
        review_tags=review_tags,
    )


def _normalized_token(value: str) -> str:
    return value.strip().casefold()


def _normalized_list(values: list[str]) -> list[str]:
    return sorted({_normalized_token(value) for value in values if value.strip()})


def _normalized_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        _normalized_token(name): value.strip()
        for name, value in sorted(
            headers.items(), key=lambda item: _normalized_token(item[0])
        )
        if name.strip() and value.strip()
    }
