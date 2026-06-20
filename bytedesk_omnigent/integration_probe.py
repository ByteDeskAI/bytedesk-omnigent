"""Deterministic ingress smoke-test probe compiler.

Operators connecting a third-party webhook need a repeatable way to prove the
Omnigent ingress binding, secret, and parked signal are wired correctly before
asking the provider to send production traffic. This module compiles a signed,
copy/pasteable probe for the default Omnigent/GitHub-style webhook contract
without contacting the provider or mutating runtime state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_EXPECTED_STATUSES: dict[int, str] = {
    202: "binding exists and parked signal was delivered",
    401: "signature/header mismatch or wrong secret",
    404: "source, binding, or pending wait is not configured",
    409: "event replayed after the signal was already resolved",
    410: "parked wait expired before delivery",
}


@dataclass(frozen=True)
class WebhookProbe:
    """A signed, deterministic smoke test for ``POST /v1/ingress/{source}``."""

    source: str
    match_key: str
    url: str
    body: str
    headers: dict[str, str]
    curl_command: str
    expected_statuses: dict[int, str]


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _join_ingress_url(base_url: str, source: str) -> str:
    return f"{base_url.rstrip('/')}/ingress/{source}"


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _curl_command(url: str, headers: Mapping[str, str], body: str) -> str:
    header_args = " ".join(
        f"-H {_shell_single_quote(f'{name}: {value}')}" for name, value in headers.items()
    )
    return (
        f"curl -fsS -X POST {_shell_single_quote(url)} "
        f"{header_args} --data {_shell_single_quote(body)}"
    )


def compile_webhook_probe(
    *,
    source: str,
    match_key: str,
    secret: str,
    payload: Mapping[str, Any] | None = None,
    raw_body: str | bytes | None = None,
    base_url: str = "http://localhost:8000/v1",
) -> WebhookProbe:
    """Compile a signed ingress smoke test for a webhook binding.

    ``payload`` is canonicalized to compact, sorted JSON so the same input always
    produces the same body and signature. ``raw_body`` preserves a captured
    provider payload byte-for-byte for native replay. Exactly one of ``payload``
    or ``raw_body`` may be supplied.
    """
    if (payload is None) == (raw_body is None):
        raise ValueError("provide exactly one of payload or raw_body")

    if raw_body is not None:
        body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
    else:
        assert payload is not None  # narrowed by the xor check above
        body = _canonical_json(payload)

    url = _join_ingress_url(base_url, source)
    signature = _sign(body, secret)
    headers = {
        "content-type": "application/json",
        "x-omnigent-event": match_key,
        "x-omnigent-signature": signature,
    }
    return WebhookProbe(
        source=source,
        match_key=match_key,
        url=url,
        body=body,
        headers=headers,
        curl_command=_curl_command(url, headers, body),
        expected_statuses=dict(_EXPECTED_STATUSES),
    )
