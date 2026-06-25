"""Edge tests for Infisical backend error paths and HTTP branches."""

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


def test_invalid_cache_ttl_env_defaults(env, monkeypatch):
    monkeypatch.setenv("OMNIGENT_INFISICAL_CACHE_TTL", "not-a-number")
    backend = InfisicalBackend()
    assert backend._ttl == float(inf._DEFAULT_TTL_S)


def test_load_returns_none_when_scope_fetch_raises(env, monkeypatch):
    backend = InfisicalBackend(client=httpx.Client(base_url="https://infisical.test"))

    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(backend, "_scope_secrets", _boom)
    assert backend.load("anything") is None


def test_delete_raises_on_server_error(env, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        if request.method == "DELETE":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={})

    backend = InfisicalBackend(
        client=httpx.Client(
            base_url="https://infisical.test", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        backend.delete("secret")


def test_scope_secrets_returns_mem_refreshed_under_lock(env, clock):
    backend = InfisicalBackend(
        client=httpx.Client(
            base_url="https://infisical.test",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        )
    )
    warmed = {"secret": {"value": "cached", "updatedAt": None}}
    backend._mem = None
    backend._mem_fetched_at = 0.0
    real_lock = backend._lock

    class _LockWithWarm:
        def __enter__(self):
            backend._mem = warmed
            backend._mem_fetched_at = inf.time.time()
            return real_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            return real_lock.__exit__(exc_type, exc, tb)

    backend._lock = _LockWithWarm()
    assert backend._scope_secrets() is warmed


def test_store_patches_on_conflict_and_delete_tolerates_missing(env, clock):
    counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if path == "/api/v1/auth/universal-auth/login":
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        if path.endswith("/newkey") and request.method == "POST":
            return httpx.Response(409, json={"error": "exists"})
        if path.endswith("/newkey") and request.method == "PATCH":
            return httpx.Response(200, json={})
        if path.endswith("/gone") and request.method == "DELETE":
            return httpx.Response(404, json={})
        if path.endswith("/gone") and request.method == "DELETE":
            return httpx.Response(404, json={})
        if path == "/api/v3/secrets/raw":
            return httpx.Response(200, json={"secrets": []})
        return httpx.Response(200, json={})

    client = httpx.Client(
        base_url="https://infisical.test", transport=httpx.MockTransport(handler)
    )
    backend = InfisicalBackend(client=client)
    backend.store("newkey", "value")
    assert counts.get("/api/v3/secrets/raw/newkey", 0) >= 1
    backend.delete("gone")  # 404 is a no-op


def test_workspace_id_param_used_when_set(env, clock, monkeypatch):
    monkeypatch.setenv("OMNIGENT_INFISICAL_WORKSPACE_ID", "ws-123")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        if request.url.path == "/api/v3/secrets/raw":
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"secrets": []})
        return httpx.Response(200, json={})

    client = httpx.Client(
        base_url="https://infisical.test", transport=httpx.MockTransport(handler)
    )
    backend = InfisicalBackend(client=client)
    backend.load("missing")
    assert seen.get("workspaceId") == "ws-123"


def test_lazy_http_client_and_refresh_raises_without_stale(env, clock, monkeypatch):
    backend = InfisicalBackend()
    assert str(backend._http().base_url).rstrip("/") == backend._host.rstrip("/")

    def _fail():
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(backend, "_fetch_scope", _fail)
    with pytest.raises(RuntimeError, match="refresh failed"):
        backend._scope_secrets()


def test_disk_cache_read_errors_are_tolerated(env, caplog):
    cache = env / "infisical-cache.json"
    cache.write_text("{not json", encoding="utf-8")
    backend = InfisicalBackend(
        client=httpx.Client(
            base_url="https://infisical.test",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        )
    )
    with caplog.at_level("WARNING", logger="bytedesk_omnigent.secrets.infisical"):
        assert backend._read_disk_cache() is None
    assert any("infisical cache unreadable" in r.message for r in caplog.records)


def test_disk_cache_write_failures_are_tolerated(env, clock, monkeypatch, caplog):
    counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if path == "/api/v1/auth/universal-auth/login":
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        if path == "/api/v3/secrets/raw":
            return httpx.Response(
                200,
                json={
                    "secrets": [
                        {
                            "secretKey": "deepseek",
                            "secretValue": "sk",
                            "updatedAt": "2026-06-19T00:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={})

    backend = InfisicalBackend(
        client=httpx.Client(
            base_url="https://infisical.test", transport=httpx.MockTransport(handler)
        )
    )

    real_makedirs = inf.os.makedirs

    def _makedirs_fail(path, *args, **kwargs):
        if str(path) == str(env):
            raise OSError("disk full")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(inf.os, "makedirs", _makedirs_fail)
    with caplog.at_level("WARNING", logger="bytedesk_omnigent.secrets.infisical"):
        assert backend.load("deepseek") == "sk"
    assert any("could not write infisical cache" in r.message for r in caplog.records)
