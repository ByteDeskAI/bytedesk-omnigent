"""Unit tests for omnigent.cli_sandbox helper functions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from omnigent.cli_sandbox import (
    _normalize_server_url,
    _omnigent_repo_root,
    _print_ready_banner,
    _require_cli_bootstrap,
    _resolve_repo_root,
)


def test_omnigent_repo_root_finds_checkout_from_cwd() -> None:
    root = _omnigent_repo_root()
    assert (root / "sdks" / "python-client").is_dir()
    assert (root / "omnigent").is_dir()


def test_omnigent_repo_root_raises_outside_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_file = tmp_path / "site" / "omnigent" / "cli_sandbox.py"
    fake_file.parent.mkdir(parents=True)
    monkeypatch.setattr("omnigent.cli_sandbox.__file__", str(fake_file))
    with pytest.raises(click.ClickException, match="Could not locate"):
        _omnigent_repo_root()


def test_resolve_repo_root_uses_explicit_path() -> None:
    root = _resolve_repo_root(_omnigent_repo_root())
    assert root == _omnigent_repo_root()


def test_resolve_repo_root_rejects_invalid_override(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="sdks/python-client"):
        _resolve_repo_root(tmp_path)


def test_require_cli_bootstrap_rejects_managed_only_provider() -> None:
    launcher = SimpleNamespace(provider="managed-only", supports_cli_bootstrap=False)
    with pytest.raises(click.ClickException, match="server-managed"):
        _require_cli_bootstrap(launcher)  # type: ignore[arg-type]


def test_normalize_server_url_strips_trailing_slash() -> None:
    assert _normalize_server_url("https://example.com/") == "https://example.com"


def test_normalize_server_url_rejects_missing_scheme() -> None:
    with pytest.raises(click.ClickException, match="full URL"):
        _normalize_server_url("//example.com")


def test_print_ready_banner_emits_connect_hint(capsys: pytest.CaptureFixture[str]) -> None:
    _print_ready_banner("lakebox", "sb-123", "https://example.com")
    out = capsys.readouterr().out
    assert "sb-123" in out
    assert "omnigent sandbox connect" in out
    assert "https://example.com" in out
