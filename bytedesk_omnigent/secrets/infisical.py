"""Infisical-backed secret store for omnigent (BDP-2303).

A :class:`omnigent.onboarding.secrets.SecretBackend` that resolves
``keychain:<name>`` references from an Infisical project — the **default**
backend when Universal Auth credentials are present, with omnigent's local
keyring/file store as the fallback (selection lives in core ``secrets.py``).

**Load minimisation (the whole point of the caching here):**

- **Bulk list, not per-secret.** One ``GET /api/v3/secrets/raw`` per
  ``(host, project, env, path)`` *scope* returns every secret in that scope and
  fills the cache. Resolving N ``keychain:`` refs at spawn time costs **one** API
  call per scope per refresh window, not N.
- **Two cache tiers.** An in-process dict (fast path, single-flight locked so a
  burst of resolutions triggers at most one fetch) over a ``0600`` on-disk cache
  (``<config_home>/infisical-cache.json``) that survives short-lived CLI
  processes — the tier that actually cuts repeat load.
- **TTL staleness** (``OMNIGENT_INFISICAL_CACHE_TTL``, default 300s; ``0`` =
  always fresh). Within the window, reads hit cache and make **zero** calls.
- **Stale-on-error.** A failed refresh returns the last good cache (logged) so a
  transient Infisical/network blip never breaks secret resolution.

``invalidate()`` is the seam for a future Infisical-**webhook** push path (true
"only on change", zero polling). ``store``/``delete`` write through and invalidate.

Config (env): ``INFISICAL_HOST_URL`` (default ``https://infisical.prod.bytedesk.ai``),
``INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`` / ``_SECRET``,
``OMNIGENT_INFISICAL_PROJECT_SLUG`` (default ``bytedesk-agent-configuration``) or
``OMNIGENT_INFISICAL_WORKSPACE_ID``, ``OMNIGENT_INFISICAL_ENV`` (default
``development``), ``OMNIGENT_INFISICAL_SECRET_PATH`` (default ``/``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "https://infisical.prod.bytedesk.ai"
_DEFAULT_PROJECT_SLUG = "bytedesk-agent-configuration"
_DEFAULT_ENV = "development"
_DEFAULT_PATH = "/"
_DEFAULT_TTL_S = 300.0
_TIMEOUT_S = 10.0
_TOKEN_SKEW_S = 60.0  # refresh the token a minute before it actually expires
_RETRY_ATTEMPTS = 3
_RETRY_BASE_S = 0.25  # exponential backoff base; small so tests stay fast


def _is_transient(exc: Exception) -> bool:
    """Transient = retry; permanent (4xx) = give up. (ADR-0009 retry-with-backoff.)"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)  # connect/timeout/read/write errors


def _with_retry(call):
    """Run ``call()`` with bounded exponential backoff, retrying only transients.

    Sits INSIDE the read path, before the stale-on-error fallback: a cold cache +
    one transient blip now recovers instead of failing the whole resolution.
    """
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not retryable/last
            if attempt == _RETRY_ATTEMPTS - 1 or not _is_transient(exc):
                raise
            delay = _RETRY_BASE_S * (2 ** attempt)
            logger.warning("infisical transient error (attempt %d/%d), retrying in %.2fs: %s",
                           attempt + 1, _RETRY_ATTEMPTS, delay, exc)
            time.sleep(delay)


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _config_home() -> str:
    return os.environ.get("OMNIGENT_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".omnigent"
    )


class InfisicalBackend:
    """Read-through/write-through Infisical secret backend with a 2-tier cache."""

    name = "infisical"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._host = (_first_env("INFISICAL_HOST_URL", "INFISICAL_API_URL", "INFISICAL_URL") or _DEFAULT_HOST).rstrip("/")
        self._client_id = _first_env("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID", "INFISICAL_CLIENT_ID")
        self._client_secret = _first_env("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", "INFISICAL_CLIENT_SECRET")
        self._workspace_id = os.environ.get("OMNIGENT_INFISICAL_WORKSPACE_ID")
        self._project_slug = os.environ.get("OMNIGENT_INFISICAL_PROJECT_SLUG") or _DEFAULT_PROJECT_SLUG
        self._env = os.environ.get("OMNIGENT_INFISICAL_ENV") or _DEFAULT_ENV
        self._path = os.environ.get("OMNIGENT_INFISICAL_SECRET_PATH") or _DEFAULT_PATH
        try:
            self._ttl = float(os.environ.get("OMNIGENT_INFISICAL_CACHE_TTL", _DEFAULT_TTL_S))
        except ValueError:
            self._ttl = _DEFAULT_TTL_S

        self._client = client  # injectable for tests; built lazily otherwise
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_exp = 0.0
        # in-memory tier: {name: {"value": str, "updatedAt": str|None}} + fetch stamp
        self._mem: dict[str, dict] | None = None
        self._mem_fetched_at = 0.0

    # ── selection ────────────────────────────────────────────────────────────

    def available(self) -> bool:
        """Usable only when Universal Auth creds are present (else local fallback)."""
        return bool(self._client_id and self._client_secret)

    # ── public SecretBackend API ─────────────────────────────────────────────

    def load(self, name: str) -> str | None:
        try:
            secrets = self._scope_secrets()
        except Exception as exc:  # noqa: BLE001 — never break the chain; fall through to local
            logger.warning("infisical load failed for %r: %s", name, exc)
            return None
        entry = secrets.get(name)
        return entry["value"] if entry else None

    def store(self, name: str, value: str) -> None:
        body = {
            "secretValue": value,
            "environment": self._env,
            "secretPath": self._path,
            **self._workspace_param(),
        }
        client = self._http()
        # create, falling back to update on conflict (secret already exists)
        resp = client.post(f"/api/v3/secrets/raw/{name}", json=body, headers=self._auth_headers())
        if resp.status_code in (400, 409, 422):
            resp = client.patch(f"/api/v3/secrets/raw/{name}", json=body, headers=self._auth_headers())
        resp.raise_for_status()
        self.invalidate()

    def delete(self, name: str) -> None:
        body = {"environment": self._env, "secretPath": self._path, **self._workspace_param()}
        resp = self._http().request(
            "DELETE", f"/api/v3/secrets/raw/{name}", json=body, headers=self._auth_headers()
        )
        if resp.status_code != 404:  # absent is a no-op
            resp.raise_for_status()
        self.invalidate()

    def invalidate(self, scope: str | None = None) -> None:
        """Drop the cached scope (in-memory + on-disk). The webhook-push seam.

        ``scope`` is accepted for forward-compatibility with a multi-scope cache;
        a single backend instance owns one scope, so any value clears it.
        """
        self._mem = None
        self._mem_fetched_at = 0.0
        self._write_disk_cache(None)

    # ── caching core ─────────────────────────────────────────────────────────

    def _scope_secrets(self) -> dict[str, dict]:
        """Return ``{name: {"value", "updatedAt"}}`` for the scope, via the cache.

        Order: fresh in-memory → fresh disk → single-flight refresh from Infisical.
        """
        now = time.time()
        if self._mem is not None and now - self._mem_fetched_at < self._ttl:
            return self._mem

        disk = self._read_disk_cache()
        if disk is not None and now - disk.get("fetched_at", 0.0) < self._ttl:
            self._mem, self._mem_fetched_at = disk["secrets"], disk["fetched_at"]
            return self._mem

        with self._lock:
            # Another thread may have refreshed while we waited for the lock.
            now = time.time()
            if self._mem is not None and now - self._mem_fetched_at < self._ttl:
                return self._mem
            try:
                fetched = self._fetch_scope()
            except Exception:
                stale = self._mem if self._mem is not None else (disk or {}).get("secrets")
                if stale is not None:
                    logger.warning("infisical refresh failed; serving stale cache", exc_info=True)
                    return stale
                raise
            self._mem, self._mem_fetched_at = fetched, now
            self._write_disk_cache({"fetched_at": now, "secrets": fetched})
            return fetched

    def _fetch_scope(self) -> dict[str, dict]:
        """One bulk ``GET /secrets/raw`` for the whole scope (bounded retry on transients)."""
        params = {"environment": self._env, "secretPath": self._path, **self._workspace_param()}

        def _get() -> httpx.Response:
            resp = self._http().get("/api/v3/secrets/raw", params=params, headers=self._auth_headers())
            resp.raise_for_status()
            return resp

        resp = _with_retry(_get)
        out: dict[str, dict] = {}
        for s in resp.json().get("secrets", []):
            out[s["secretKey"]] = {"value": s.get("secretValue"), "updatedAt": s.get("updatedAt")}
        return out

    # ── auth ─────────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp:
            return self._token

        def _login() -> httpx.Response:
            resp = self._http().post(
                "/api/v1/auth/universal-auth/login",
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
            )
            resp.raise_for_status()
            return resp

        resp = _with_retry(_login)
        data = resp.json()
        self._token = data["accessToken"]
        self._token_exp = now + float(data.get("expiresIn", 600)) - _TOKEN_SKEW_S
        return self._token

    # ── http + scope helpers ─────────────────────────────────────────────────

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self._host, timeout=_TIMEOUT_S)
        return self._client

    def _workspace_param(self) -> dict[str, str]:
        # Prefer an explicit workspace id; otherwise resolve by project slug.
        if self._workspace_id:
            return {"workspaceId": self._workspace_id}
        return {"workspaceSlug": self._project_slug}

    def _scope_key(self) -> str:
        ws = self._workspace_id or self._project_slug
        return f"{self._host}|{ws}|{self._env}|{self._path}"

    # ── disk cache (0600, survives short CLI processes) ──────────────────────

    def _cache_path(self) -> str:
        return os.path.join(_config_home(), "infisical-cache.json")

    def _read_disk_cache(self) -> dict | None:
        path = self._cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get(self._scope_key())
        except (json.JSONDecodeError, OSError):
            logger.warning("infisical cache unreadable; ignoring", exc_info=True)
            return None

    def _write_disk_cache(self, entry: dict | None) -> None:
        path = self._cache_path()
        try:
            data: dict = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            if entry is None:
                data.pop(self._scope_key(), None)
            else:
                data[self._scope_key()] = entry
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("could not write infisical cache", exc_info=True)
