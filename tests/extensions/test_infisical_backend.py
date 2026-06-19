"""Tests for the Infisical secret backend + its 2-tier cache (BDP-2303).

httpx is mocked via ``httpx.MockTransport``; a request counter proves the cache
makes **one** bulk fetch per scope per TTL window (not one per secret, not one
per read).
"""

from __future__ import annotations

import httpx
import pytest

from bytedesk_omnigent.secrets import infisical as inf
from bytedesk_omnigent.secrets.infisical import InfisicalBackend


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(inf.time, "time", lambda: state["t"])
    return state


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("OMNIGENT_INFISICAL_PROJECT_SLUG", "bytedesk-agent-configuration")
    monkeypatch.setenv("OMNIGENT_INFISICAL_ENV", "development")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_INFISICAL_CACHE_TTL", "300")
    return tmp_path


def _mock_client(counts, secrets, *, fail_list=False):
    """An httpx.Client whose transport counts calls and serves a fixed scope."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if path == "/api/v1/auth/universal-auth/login":
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        if path == "/api/v3/secrets/raw":
            if fail_list:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, json={"secrets": secrets})
        return httpx.Response(200, json={})

    return httpx.Client(base_url="https://infisical.test", transport=httpx.MockTransport(handler))


_DEEPSEEK = [
    {"secretKey": "deepseek", "secretValue": "sk-deepseek", "updatedAt": "2026-06-19T00:00:00Z"},
    {"secretKey": "anthropic", "secretValue": "sk-ant", "updatedAt": "2026-06-19T00:00:00Z"},
]


def test_available_requires_creds(env, monkeypatch):
    assert InfisicalBackend().available() is True
    monkeypatch.delenv("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("INFISICAL_CLIENT_SECRET", raising=False)  # the alias _first_env falls back to
    assert InfisicalBackend().available() is False


def test_load_returns_value(env, clock):
    counts: dict = {}
    b = InfisicalBackend(client=_mock_client(counts, _DEEPSEEK))
    assert b.load("deepseek") == "sk-deepseek"
    assert b.load("missing") is None


def test_one_bulk_fetch_serves_many_secrets_in_window(env, clock):
    counts: dict = {}
    b = InfisicalBackend(client=_mock_client(counts, _DEEPSEEK))
    # three resolutions, two distinct names → still ONE /secrets/raw call
    b.load("deepseek")
    b.load("anthropic")
    b.load("deepseek")
    assert counts["/api/v3/secrets/raw"] == 1
    assert counts["/api/v1/auth/universal-auth/login"] == 1


def test_disk_cache_survives_new_instance(env, clock):
    counts: dict = {}
    client = _mock_client(counts, _DEEPSEEK)
    InfisicalBackend(client=client).load("deepseek")
    assert counts["/api/v3/secrets/raw"] == 1
    # a fresh process/instance reads the 0600 disk cache — no new fetch
    assert InfisicalBackend(client=client).load("anthropic") == "sk-ant"
    assert counts["/api/v3/secrets/raw"] == 1


def test_ttl_expiry_triggers_refetch(env, clock):
    counts: dict = {}
    b = InfisicalBackend(client=_mock_client(counts, _DEEPSEEK))
    b.load("deepseek")
    clock["t"] += 301  # past the 300s TTL
    b.load("deepseek")
    assert counts["/api/v3/secrets/raw"] == 2


def test_stale_on_error(env, clock):
    counts: dict = {}
    secrets = list(_DEEPSEEK)
    client = _mock_client(counts, secrets)
    b = InfisicalBackend(client=client)
    assert b.load("deepseek") == "sk-deepseek"
    # now make the list endpoint fail and expire the cache
    b._client = _mock_client(counts, secrets, fail_list=True)
    clock["t"] += 301
    # stale-on-error: last good value still served
    assert b.load("deepseek") == "sk-deepseek"


def test_store_invalidates_cache(env, clock):
    counts: dict = {}
    secrets = list(_DEEPSEEK)
    client = _mock_client(counts, secrets)
    b = InfisicalBackend(client=client)
    b.load("deepseek")
    assert counts["/api/v3/secrets/raw"] == 1
    b.store("newkey", "newval")  # POST/PATCH + invalidate
    b.load("deepseek")  # cache dropped → refetch
    assert counts["/api/v3/secrets/raw"] == 2


def test_disk_cache_is_0600(env, clock):
    counts: dict = {}
    b = InfisicalBackend(client=_mock_client(counts, _DEEPSEEK))
    b.load("deepseek")
    cache = env / "infisical-cache.json"
    assert cache.exists()
    assert (cache.stat().st_mode & 0o777) == 0o600


def test_extension_contributes_backend(env):
    from bytedesk_omnigent.extension import BytedeskExtension

    backends = BytedeskExtension().secret_backends()
    assert len(backends) == 1
    assert backends[0].name == "infisical"
