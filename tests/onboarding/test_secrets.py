"""Tests for the pluggable secret store (BDP-2303).

Covers the ``SecretBackend`` protocol, the local keyring/file backend (the
historical behaviour, unchanged), and the chain that lets an extension-contributed
backend (e.g. Infisical) take precedence while local stays the fallback.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding import secrets as s


@pytest.fixture
def file_home(tmp_path, monkeypatch):
    """Force the file backend at an isolated config home."""
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    # No extension backends, no explicit override → pure local. Stub the seam so the
    # ambient shell's Infisical creds don't make the remote backend primary here.
    monkeypatch.delenv("OMNIGENT_SECRET_BACKEND", raising=False)
    monkeypatch.setattr(s, "_extension_backends", lambda: [])
    s.reset_backends()
    yield tmp_path
    s.reset_backends()


# ── local backend: historical behaviour preserved ────────────────────────────


def test_file_backend_roundtrip(file_home):
    s.store_secret("anthropic", "sk-ant-xyz")
    assert s.load_secret("anthropic") == "sk-ant-xyz"


def test_load_missing_returns_none(file_home):
    assert s.load_secret("nope") is None


def test_delete_is_idempotent(file_home):
    s.store_secret("k", "v")
    s.delete_secret("k")
    s.delete_secret("k")  # no error on absent
    assert s.load_secret("k") is None


def test_file_backend_is_0600(file_home):
    s.store_secret("k", "v")
    mode = (file_home / "secrets.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_active_backend_reports_local(file_home):
    assert s.active_backend() == s.FILE_BACKEND


# ── chain: extension backend takes precedence, local is the fallback ──────────


class _FakeBackend:
    """A minimal in-memory SecretBackend for chain tests."""

    def __init__(self, name, *, available=True, store=None):
        self.name = name
        self._available = available
        self._store = dict(store or {})
        self.writes: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    def load(self, name):
        return self._store.get(name)

    def store(self, name, value):
        self.writes.append((name, value))
        self._store[name] = value

    def delete(self, name):
        self._store.pop(name, None)


def test_available_extension_backend_is_primary(file_home, monkeypatch):
    fake = _FakeBackend("infisical", store={"deepseek": "from-infisical"})
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    s.reset_backends()
    assert s.active_backend() == "infisical"
    assert s.load_secret("deepseek") == "from-infisical"


def test_unavailable_extension_backend_falls_back_to_local(file_home, monkeypatch):
    fake = _FakeBackend("infisical", available=False)
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    s.reset_backends()
    # Infisical inert (no creds) → local file backend is primary.
    assert s.active_backend() == s.FILE_BACKEND
    s.store_secret("k", "v")
    assert s.load_secret("k") == "v"
    assert fake.writes == []  # never written to the inert backend


def test_load_falls_through_to_local_when_primary_misses(file_home, monkeypatch):
    fake = _FakeBackend("infisical", store={})  # available but doesn't have it
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    s.reset_backends()
    s.store_secret("local-only", "v")  # written to primary (infisical fake)
    # primary recorded the write; simulate it not being readable there
    fake._store.clear()
    # seed the local file backend directly
    monkeypatch.setattr(s, "_extension_backends", lambda: [])
    s.reset_backends()
    s.store_secret("local-only", "v")
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    s.reset_backends()
    assert s.load_secret("local-only") == "v"  # fell through to local


def test_explicit_override_selects_local(file_home, monkeypatch):
    fake = _FakeBackend("infisical", store={"x": "remote"})
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    monkeypatch.setenv("OMNIGENT_SECRET_BACKEND", "file")
    s.reset_backends()
    assert s.active_backend() == s.FILE_BACKEND
    assert s.load_secret("x") is None  # didn't consult infisical
