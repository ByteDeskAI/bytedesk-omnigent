"""Tests for the shared provider error taxonomy (BDP-2345, sweep-3 item E)."""

from __future__ import annotations

from omnigent.kernel.pluggable import (
    ProviderError,
    ProviderNotRegistered,
    ProviderUnavailable,
    ProviderUnconfigured,
    RegistryConflict,
)


def test_all_subclasses_are_provider_errors() -> None:
    for cls in (
        ProviderNotRegistered,
        ProviderUnconfigured,
        ProviderUnavailable,
        RegistryConflict,
    ):
        assert issubclass(cls, ProviderError)


def test_not_registered_lists_known_names() -> None:
    err = ProviderNotRegistered("widget", "missing", known=["a", "b"])
    msg = str(err)
    assert "widget" in msg
    assert "missing" in msg
    assert "a" in msg and "b" in msg


def test_not_registered_without_known_names() -> None:
    err = ProviderNotRegistered("widget", "missing")
    msg = str(err)
    assert "widget" in msg and "missing" in msg


def test_registry_conflict_names_seam_and_name() -> None:
    err = RegistryConflict("widget", "dup")
    msg = str(err)
    assert "widget" in msg and "dup" in msg
