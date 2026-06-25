"""Tests for the pluggable secret store (BDP-2303).

Covers the ``SecretBackend`` protocol, the local keyring/file backend (the
historical behaviour, unchanged), and the chain that lets an extension-contributed
backend (e.g. Infisical) take precedence while local stays the fallback.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding import secrets as s

# Captured before onboarding/conftest autouse stubs _extension_backends.
_REAL_EXTENSION_BACKENDS = s._extension_backends


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


def test_config_home_defaults_under_user_omnigent(tmp_path, monkeypatch):
    """Without ``OMNIGENT_CONFIG_HOME``, secrets land under ``~/.omnigent``."""
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(s, "_extension_backends", lambda: [])
    s.reset_backends()

    s.store_secret("home-default", "value")
    assert (tmp_path / ".omnigent" / "secrets.json").exists()
    assert s.load_secret("home-default") == "value"


def test_keyring_backend_roundtrip(monkeypatch):
    """When keyring is enabled, reads/writes go through the OS keychain."""
    store: dict[str, str] = {}

    def _get(service: str, name: str) -> str | None:
        return store.get(f"{service}:{name}")

    def _set(service: str, name: str, value: str) -> None:
        store[f"{service}:{name}"] = value

    def _delete(service: str, name: str) -> None:
        store.pop(f"{service}:{name}", None)

    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    monkeypatch.setattr(s, "_extension_backends", lambda: [])
    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.get_password", _get)
    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.set_password", _set)
    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.delete_password", _delete)
    s.reset_backends()

    assert s.active_backend() == s.KEYRING_BACKEND
    s.store_secret("openai", "sk-test")
    assert s.load_secret("openai") == "sk-test"
    s.delete_secret("openai")
    assert s.load_secret("openai") is None


def test_keyring_errors_fall_back_to_file(tmp_path, monkeypatch):
    """A locked keyring must not block the on-disk fallback."""
    import keyring.errors

    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    monkeypatch.setattr(s, "_extension_backends", lambda: [])

    def _boom(*_a: object, **_k: object) -> None:
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.get_password", _boom)
    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.set_password", _boom)
    monkeypatch.setattr("omnigent.onboarding.secrets.keyring.delete_password", _boom)
    s.reset_backends()

    s.store_secret("fallback", "file-value")
    assert s.load_secret("fallback") == "file-value"
    s.delete_secret("fallback")
    assert s.load_secret("fallback") is None


def test_extension_backends_returns_contributed_backends(monkeypatch) -> None:
    """A healthy extension seam contributes backends to the chain."""
    fake = _FakeBackend("contrib", store={"token": "remote"})
    monkeypatch.setattr(s, "_extension_backends", _REAL_EXTENSION_BACKENDS)
    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_secret_backends",
        lambda: [fake],
    )
    assert s._extension_backends() == [fake]


def test_extension_backends_call_error_returns_empty(monkeypatch) -> None:
    """A broken extension contributor must not break secret resolution."""
    monkeypatch.setattr(s, "_extension_backends", _REAL_EXTENSION_BACKENDS)
    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_secret_backends",
        lambda: (_ for _ in ()).throw(RuntimeError("extension seam unavailable")),
    )
    assert s._extension_backends() == []


def test_extension_backends_import_failure_is_ignored(tmp_path, monkeypatch):
    """A broken extension seam must not break local secret resolution."""
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_secret_backends",
        lambda: (_ for _ in ()).throw(RuntimeError("extension seam unavailable")),
    )
    s.reset_backends()
    s.store_secret("local", "ok")
    assert s.load_secret("local") == "ok"
    s.reset_backends()


def test_unavailable_extension_reports_false_when_availability_raises(
    file_home, monkeypatch
):
    """Backends that crash ``available()`` are skipped."""

    class _CrashingBackend:
        name = "crashing"

        def available(self) -> bool:
            raise RuntimeError("cannot probe")

        def load(self, name: str) -> str | None:
            return None

        def store(self, name: str, value: str) -> None:
            pass

        def delete(self, name: str) -> None:
            pass

    monkeypatch.setattr(s, "_extension_backends", lambda: [_CrashingBackend()])
    s.reset_backends()
    assert s.active_backend() == s.FILE_BACKEND


def test_explicit_override_selects_extension_backend(file_home, monkeypatch):
    """``OMNIGENT_SECRET_BACKEND=infisical`` pins the named extension backend."""
    fake = _FakeBackend("infisical", store={"pinned": "remote"})
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    monkeypatch.setenv("OMNIGENT_SECRET_BACKEND", "infisical")
    s.reset_backends()
    assert s.active_backend() == "infisical"
    assert s.load_secret("pinned") == "remote"


def test_explicit_override_unknown_falls_back_to_local(file_home, monkeypatch):
    fake = _FakeBackend("infisical", store={})
    monkeypatch.setattr(s, "_extension_backends", lambda: [fake])
    monkeypatch.setenv("OMNIGENT_SECRET_BACKEND", "nonexistent")
    s.reset_backends()
    assert s.active_backend() == s.FILE_BACKEND


def test_load_skips_flaky_backend_and_falls_through(file_home, monkeypatch):
    """A backend that raises on ``load`` must not block later backends."""

    class _FlakyBackend(_FakeBackend):
        def load(self, name: str) -> str | None:
            raise OSError("network down")

    flaky = _FlakyBackend("infisical", store={"k": "remote"})
    monkeypatch.setattr(s, "_extension_backends", lambda: [flaky])
    s.reset_backends()
    (file_home / "secrets.json").write_text('{"k": "local-copy"}')
    assert s.load_secret("k") == "local-copy"


def test_local_backend_available_is_always_true(file_home):
    """``LocalBackend.available`` is the unconditional local fallback hook."""
    assert s.LocalBackend().available() is True
