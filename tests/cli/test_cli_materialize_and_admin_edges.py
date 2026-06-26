"""Edge tests for bundled example materialization and first-admin prompt."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import omnigent.cli as cli_mod
from omnigent.cli import (
    _INTERNAL_BETA_DEFAULT_AGENT_NAME,
    _create_artifact_store,
    _materialize_bundled_example,
    _materialize_internal_beta_agents,
    _maybe_prompt_first_admin,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore


class _FakeResource:
    """Minimal importlib.resources stand-in for bundled YAML."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, *, encoding: str) -> str:
        del encoding
        return self._text


def test_create_artifact_store_local_branch(tmp_path: Path) -> None:
    store = _create_artifact_store(str(tmp_path / "artifacts"))
    assert isinstance(store, LocalArtifactStore)


def test_create_artifact_store_nats_branch() -> None:
    from omnigent.stores.artifact_store.nats_object_store import (
        NatsObjectStoreArtifactStore,
    )

    store = _create_artifact_store("nats://omnigent-nats:4222/omnigent-artifacts")
    assert isinstance(store, NatsObjectStoreArtifactStore)


def test_create_artifact_store_delegates_to_shared_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    sentinel = object()

    def fake_create(location: str) -> object:
        captured["location"] = location
        return sentinel

    monkeypatch.setattr("omnigent.stores.factory._create_artifact_store", fake_create)

    assert _create_artifact_store("custom://artifact-store") is sentinel
    assert captured == {"location": "custom://artifact-store"}


def test_materialize_bundled_example_returns_existing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    existing = agents_dir / "demo_agent.yaml"
    existing.write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "_GLOBAL_AGENTS_DIR", agents_dir)

    path = _materialize_bundled_example("demo_agent.yaml")

    assert path == existing
    assert path.read_text() == "name: demo\n"


def test_materialize_bundled_example_copies_and_rewrites_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(cli_mod, "_GLOBAL_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cli_mod.sys, "executable", "/usr/bin/python3.12")

    bundled = 'python: "${OMNIGENT_HOME:-$PWD}/.venv/bin/python"\nalt: .venv/bin/python\n'

    class _Files:
        def joinpath(self, name: str) -> _FakeResource:
            assert name == "demo_agent.yaml"
            return _FakeResource(bundled)

    monkeypatch.setattr(cli_mod.resources, "files", lambda _pkg: _Files())

    path = _materialize_bundled_example("demo_agent.yaml")

    assert path == agents_dir / "demo_agent.yaml"
    text = path.read_text(encoding="utf-8")
    assert "/usr/bin/python3.12" in text
    assert ".venv/bin/python" not in text


def test_materialize_internal_beta_agents_returns_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(cli_mod, "_GLOBAL_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cli_mod.sys, "executable", "/usr/bin/python3.12")
    calls: list[str] = []

    def _fake_materialize(name: str) -> Path:
        calls.append(name)
        path = agents_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"name: {name}\n", encoding="utf-8")
        return path

    monkeypatch.setattr(cli_mod, "_materialize_bundled_example", _fake_materialize)

    default_path = _materialize_internal_beta_agents()

    assert default_path.name == _INTERNAL_BETA_DEFAULT_AGENT_NAME
    assert calls == list(cli_mod._INTERNAL_BETA_BUNDLED_AGENTS)


def test_maybe_prompt_first_admin_noops_without_account_store() -> None:
    _maybe_prompt_first_admin(None, MagicMock(), auto_open=False)


def test_maybe_prompt_first_admin_noops_when_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.list_users.return_value = []
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    _maybe_prompt_first_admin(store, MagicMock(), auto_open=False)
    store.create_user_with_password.assert_not_called()


def test_maybe_prompt_first_admin_noops_when_password_user_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.list_users.return_value = [SimpleNamespace(has_password=True)]
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    _maybe_prompt_first_admin(store, MagicMock(), auto_open=False)
    store.create_user_with_password.assert_not_called()


def test_maybe_prompt_first_admin_defers_to_browser_on_loopback_auto_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.auth import UnifiedAuthProvider

    store = MagicMock()
    store.list_users.return_value = []
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)

    cfg = SimpleNamespace(
        base_url="http://127.0.0.1:8000", cookie_secret="secret", session_ttl_hours=24
    )
    provider = MagicMock(spec=UnifiedAuthProvider)
    provider._accounts_config = cfg

    _maybe_prompt_first_admin(store, provider, auto_open=True)
    store.create_user_with_password.assert_not_called()


def test_maybe_prompt_first_admin_creates_admin_and_mints_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnigent.server.auth import UnifiedAuthProvider

    store = MagicMock()
    store.list_users.return_value = []
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)

    cfg = SimpleNamespace(
        base_url="http://127.0.0.1:8000",
        cookie_secret="cookie-secret",
        session_ttl_hours=12,
    )
    provider = MagicMock(spec=UnifiedAuthProvider)
    provider._accounts_config = cfg

    minted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "omnigent.server.accounts_bootstrap.resolve_admin_username",
        lambda: "admin",
    )
    monkeypatch.setattr(
        "omnigent.server.passwords.hash_password",
        lambda password: f"hash:{password}",
    )
    monkeypatch.setattr(
        "omnigent.server.accounts_bootstrap._mint_loopback_cli_token",
        lambda username, **kwargs: minted.append((username, kwargs["base_url"])),
    )

    def _prompt(label: str, **kwargs: object) -> str:
        if "Username" in label:
            return "admin"
        return "long-enough-password"

    monkeypatch.setattr(cli_mod.click, "prompt", _prompt)

    _maybe_prompt_first_admin(store, provider, auto_open=False)

    store.create_user_with_password.assert_called_once_with(
        "admin", "hash:long-enough-password", is_admin=True
    )
    assert minted == [("admin", "http://127.0.0.1:8000")]
    out = capsys.readouterr().out
    assert "Admin 'admin' created" in out


def test_maybe_prompt_first_admin_retries_short_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnigent.server.auth import UnifiedAuthProvider

    store = MagicMock()
    store.list_users.return_value = []
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    provider = MagicMock(spec=UnifiedAuthProvider)
    provider._accounts_config = None
    monkeypatch.setattr(
        "omnigent.server.accounts_bootstrap.resolve_admin_username",
        lambda: "admin",
    )
    monkeypatch.setattr(
        "omnigent.server.passwords.hash_password",
        lambda password: f"hash:{password}",
    )
    prompts: list[str] = []

    def _prompt(label: str, **kwargs: object) -> str:
        if "Username" in label:
            return "admin"
        prompts.append(label)
        return "short" if len(prompts) == 1 else "long-enough-password"

    monkeypatch.setattr(cli_mod.click, "prompt", _prompt)
    monkeypatch.setattr(
        "omnigent.server.routes.accounts_auth._MIN_PASSWORD_LENGTH",
        12,
    )

    _maybe_prompt_first_admin(store, provider, auto_open=False)

    assert len(prompts) == 2
    err = capsys.readouterr().err
    assert "at least 12 characters" in err
    store.create_user_with_password.assert_called_once_with(
        "admin",
        "hash:long-enough-password",
        is_admin=True,
    )


def test_maybe_prompt_first_admin_skips_on_create_race(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = MagicMock()
    store.list_users.return_value = []
    store.create_user_with_password.side_effect = ValueError("already exists")
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        "omnigent.server.accounts_bootstrap.resolve_admin_username",
        lambda: "admin",
    )
    monkeypatch.setattr(
        "omnigent.server.passwords.hash_password",
        lambda password: f"hash:{password}",
    )

    def _prompt(label: str, **kwargs: object) -> str:
        if "Username" in label:
            return "admin"
        return "long-enough-password"

    monkeypatch.setattr(cli_mod.click, "prompt", _prompt)

    _maybe_prompt_first_admin(store, MagicMock(), auto_open=False)

    err = capsys.readouterr().err
    assert "created elsewhere" in err
