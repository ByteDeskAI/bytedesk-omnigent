"""Shared HTTP-adapter scaffolding for the SaaS tool clients (ADR-0008 Adapter).

The ``github`` / ``jira`` / ``confluence`` / ``slack`` tool clients each wrap an
external REST API as a lazily-credentialed ``httpx`` client. Their credential
resolution and auth headers genuinely differ per provider; the httpx lifecycle
(lazy client build + the require-configured → request skeleton) and the
secret-fallback lookup do **not** — those were copy-pasted verbatim across the
four files. This module holds the shared half so each provider client overrides
only ``_require_configured`` (resolve creds + raise if unset) and ``_headers``
(provider auth).
"""

from __future__ import annotations

from typing import Any

import httpx

#: Default per-request wallclock for the SaaS adapters. Every provider client
#: used the same 20s value; override ``_timeout_s`` on a subclass to change it.
_DEFAULT_TIMEOUT_S = 20.0


def first_secret(names: tuple[str, ...]) -> str:
    """Return the first non-empty secret value among ``names`` (or empty string)."""
    from omnigent.onboarding.secrets import load_secret

    for name in names:
        value = (load_secret(name) or "").strip()
        if value:
            return value
    return ""


class HttpToolClient:
    """Lazy-``httpx`` base for the SaaS tool adapters.

    Subclasses set ``self._base_url`` and ``self._client`` in ``__init__`` and
    implement :meth:`_require_configured` and :meth:`_headers`. ``_http`` (lazy,
    cached client) is shared by all four providers; ``_request`` (the
    require-configured → request skeleton) is shared by every provider whose
    ``_headers`` takes no arguments — Slack keeps its own ``_get``/``_post``
    because its header set depends on whether the body is JSON.
    """

    _base_url: str | None
    _client: httpx.Client | None
    #: Per-request timeout; subclasses may override.
    _timeout_s: float = _DEFAULT_TIMEOUT_S

    def _require_configured(self) -> None:
        """Resolve credentials and raise the provider's not-configured error."""
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        """Return the provider's auth headers."""
        raise NotImplementedError

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self._base_url, timeout=self._timeout_s)
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._require_configured()
        return self._http().request(method, path, headers=self._headers(), **kwargs)
