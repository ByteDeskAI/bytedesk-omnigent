"""Edge-case coverage for omnigent.runtime.credentials.databricks."""

from __future__ import annotations

import configparser
import logging
from pathlib import Path

import pytest

from omnigent.runtime.credentials.databricks import (
    DEFAULT_DATABRICKSCFG_PATH,
    _call_sdk_authenticate,
    _databrickscfg_path,
    _read_section,
    resolve_databricks_workspace,
)


@pytest.fixture(autouse=True)
def _disable_sdk_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests fast by short-circuiting real SDK authentication."""

    def _raise_value_error(*_args: object, **_kwargs: object) -> None:
        raise ValueError("SDK path disabled in tests by default")

    monkeypatch.setattr("databricks.sdk.config.Config", _raise_value_error)
    for var in ("DATABRICKS_CONFIG_FILE", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def test_databrickscfg_path_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DATABRICKS_CONFIG_FILE overrides the default ~/.databrickscfg path."""
    override = tmp_path / "custom.cfg"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(override))
    assert _databrickscfg_path() == override


def test_databrickscfg_path_falls_back_to_home_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset or empty DATABRICKS_CONFIG_FILE uses the home-directory default."""
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    assert _databrickscfg_path() == DEFAULT_DATABRICKSCFG_PATH

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "")
    assert _databrickscfg_path() == DEFAULT_DATABRICKSCFG_PATH


def test_read_section_returns_none_for_empty_default() -> None:
    """An empty [DEFAULT] section is treated as absent."""
    config = configparser.ConfigParser()
    assert _read_section(config, "DEFAULT") is None


def test_read_section_returns_none_for_incomplete_default() -> None:
    """[DEFAULT] missing host or token resolves to None rather than raising."""
    config = configparser.ConfigParser()
    config.read_string("[DEFAULT]\nhost = https://only-host.example.com\n")
    assert _read_section(config, "DEFAULT") is None


def test_resolve_raises_when_default_section_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An on-disk cfg with no usable [DEFAULT] values fails resolution."""
    cfg = tmp_path / "empty-default.cfg"
    cfg.write_text("; no sections with credentials\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    with pytest.raises(OSError, match="\\[DEFAULT\\]"):
        resolve_databricks_workspace(profile=None)


def test_call_sdk_authenticate_import_error_logs_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing databricks-sdk falls through to configparser with a warning."""
    real_import = __import__

    def _import_without_sdk_config(
        name: str,
        globals: object | None = None,
        locals: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "databricks.sdk.config":
            raise ImportError("databricks-sdk not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _import_without_sdk_config)

    with caplog.at_level(logging.WARNING, logger="omnigent.runtime.credentials.databricks"):
        assert _call_sdk_authenticate("prod") is None

    assert any(
        "databricks-sdk is not importable" in record.message
        for record in caplog.records
    )